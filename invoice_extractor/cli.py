"""CLI entry points.

  run       full pipeline: page-level classify -> LLM -> merge -> validate -> Excel
  classify  stage test: per-page classification report (no API calls)
  render    stage test: render image pages to PNGs on disk (no API calls)
  doctor    environment/config health check (no API calls unless --live)
"""

import importlib
import sys
from pathlib import Path

import click

from invoice_extractor import pdf_utils
from invoice_extractor.config import describe_models, load_config
from invoice_extractor.excel_export import export_workbook
from invoice_extractor.logging_setup import new_run_id, setup_logging
from invoice_extractor.pipeline import InvoiceResult, find_pdfs, process_directory


@click.group()
def cli():
    """Batch invoice PDF extraction to Excel."""


def print_summary(results: list[InvoiceResult]) -> None:
    processed = len(results)
    needs_review = sum(1 for r in results if r.needs_review)
    errors = sum(1 for r in results if r.error)
    by_method: dict[str, int] = {}
    for r in results:
        by_method[r.extraction_method] = by_method.get(r.extraction_method, 0) + 1

    click.echo("")
    click.echo("=" * 52)
    click.echo(f"  Processed:     {processed}")
    click.echo(f"  Needs review:  {needs_review}")
    click.echo(f"  Errors:        {errors}")
    for method, count in sorted(by_method.items()):
        click.echo(f"    - {method}: {count}")
    click.echo("=" * 52)


@cli.command()
@click.option("--input", "input_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Folder containing invoice PDFs.")
@click.option("--output", "output_path", default="./output/results.xlsx",
              type=click.Path(dir_okay=False, path_type=Path), show_default=True,
              help="Excel workbook to write.")
@click.option("--log-file", default=None, type=click.Path(dir_okay=False, path_type=Path),
              help="Log file path (default: <output dir>/run.log).")
def run(input_dir: Path, output_path: Path, log_file: Path | None):
    """Run the full extraction pipeline over a folder of PDFs."""
    cfg = load_config()
    run_id = new_run_id()
    log_path = log_file or output_path.parent / "run.log"
    logger = setup_logging(
        log_path, run_id=run_id,
        secrets=(cfg.gemini_api_key or "", cfg.anthropic_api_key or ""),
    )
    logger.info("run %s starting; %s", run_id, describe_models(cfg))

    if not cfg.gemini_api_key:
        logger.warning("GEMINI_API_KEY not set - Gemini calls will fail")
    if not cfg.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not set - Claude fallback is unavailable")

    results = process_directory(input_dir, cfg, logger)
    if not results:
        raise SystemExit(f"No PDFs found in {input_dir}")

    export_workbook(results, output_path)
    logger.info("Wrote %s (log: %s)", output_path, log_path)
    print_summary(results)


@cli.command()
@click.option("--input", "input_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
def classify(input_dir: Path):
    """Stage test: per-page classification report. Makes no API calls."""
    cfg = load_config()
    pdfs = find_pdfs(input_dir)
    if not pdfs:
        raise SystemExit(f"No PDFs found in {input_dir}")

    for path in pdfs:
        try:
            pages = pdf_utils.analyze_pages(str(path), cfg.text_quality_threshold)
        except Exception as exc:
            click.echo(f"{path.name}: ERROR unreadable ({type(exc).__name__})")
            continue
        doc_class = pdf_utils.classify_document(pages)
        click.echo(f"{path.name}: {len(pages)} page(s) -> {doc_class}")
        for p in pages:
            click.echo(f"    page {p.number}: {p.kind:<5} ({p.alnum_chars} alnum chars)")


@cli.command()
@click.option("--input", "input_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "output_dir", default="./output/pages",
              type=click.Path(file_okay=False, path_type=Path), show_default=True)
@click.option("--all", "render_all", is_flag=True,
              help="Render every page, not just image-classified ones.")
def render(input_dir: Path, output_dir: Path, render_all: bool):
    """Stage test: render image pages to PNGs (what vision would see). No API calls."""
    cfg = load_config()
    pdfs = find_pdfs(input_dir)
    if not pdfs:
        raise SystemExit(f"No PDFs found in {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for path in pdfs:
        try:
            pages = pdf_utils.analyze_pages(str(path), cfg.text_quality_threshold)
            wanted = [p.number for p in pages
                      if render_all or p.kind == pdf_utils.PAGE_IMAGE]
            wanted = wanted[: cfg.max_vision_pages]
            if not wanted:
                click.echo(f"{path.name}: no image pages, skipped (use --all to force)")
                continue
            images = pdf_utils.render_pages_png(str(path), wanted, dpi=cfg.render_dpi)
        except Exception as exc:
            click.echo(f"{path.name}: ERROR ({type(exc).__name__})")
            continue
        for number, png in zip(wanted, images):
            (output_dir / f"{path.stem}_p{number}.png").write_bytes(png)
        click.echo(f"{path.name}: rendered page(s) {wanted} at {cfg.render_dpi} DPI")
        total += 1
    click.echo(f"\nRendered {total} PDF(s) to {output_dir}")


_REQUIRED_PACKAGES = {
    "pymupdf": "fitz",
    "google-genai": "google.genai",
    "anthropic": "anthropic",
    "pandas": "pandas",
    "openpyxl": "openpyxl",
    "tenacity": "tenacity",
    "click": "click",
    "pydantic": "pydantic",
    "python-dotenv": "dotenv",
}


def _check(ok: bool, label: str, detail: str = "") -> bool:
    mark = "OK " if ok else "FAIL"
    click.echo(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def classify_probe_error(exc: BaseException) -> str:
    """Human-readable category for a doctor --live probe failure.

    Doctor probes call the client seams directly (no tenacity), so
    authentication and invalid-model errors are never retried.
    """
    import anthropic
    import httpx
    from google.genai import errors as genai_errors

    if isinstance(exc, RuntimeError) and "is not set" in str(exc):
        return "missing key"
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return "authentication failure"
        if code == 404:
            return "model not found or unavailable"
        if code == 429:
            return "rate limited"
        if isinstance(code, int) and code >= 500:
            return "provider server error"
        return f"request rejected (HTTP {code})"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "authentication failure"
    if isinstance(exc, anthropic.NotFoundError):
        return "model not found or unavailable"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited"
    if isinstance(exc, (anthropic.APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, (anthropic.APIConnectionError, httpx.TransportError, ConnectionError)):
        return "network failure"
    return f"error ({type(exc).__name__})"


def _tiny_png() -> bytes:
    """A generated 1-page blank PNG for live vision probes - never a real invoice."""
    import fitz

    doc = fitz.open()
    doc.new_page(width=64, height=64)
    png = doc[0].get_pixmap().tobytes("png")
    doc.close()
    return png


@cli.command()
@click.option("--input", "input_dir", default="./samples", show_default=True,
              type=click.Path(path_type=Path))
@click.option("--output", "output_dir", default="./output", show_default=True,
              type=click.Path(path_type=Path))
@click.option("--live", is_flag=True,
              help="Make one minimal request per provider/route to confirm the "
                   "configured models are accepted. Sends a tiny generated probe, "
                   "NEVER an invoice. Costs a few tokens.")
def doctor(input_dir: Path, output_dir: Path, live: bool):
    """Health check: environment, dependencies, paths, keys, models.

    Offline by default. With --live, makes the smallest practical provider
    request per configured model to confirm acceptance.
    """
    cfg = load_config()
    ok = True

    click.echo("Environment:")
    py = sys.version_info
    ok &= _check(py >= (3, 11), f"Python {py.major}.{py.minor}.{py.micro} (>= 3.11 required)")

    click.echo("Packages:")
    from importlib.metadata import PackageNotFoundError, version
    for pkg, module in _REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module)
            try:
                ver = version(pkg)
            except PackageNotFoundError:
                ver = "unknown"
            ok &= _check(True, f"{pkg}", ver)
        except Exception as exc:
            ok &= _check(False, f"{pkg}", f"import failed: {type(exc).__name__}")

    click.echo("Paths:")
    ok &= _check(input_dir.is_dir(),
                 f"input dir {input_dir}",
                 f"{len(find_pdfs(input_dir))} PDF(s)" if input_dir.is_dir() else "missing")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        ok &= _check(True, f"output dir {output_dir}", "writable")
    except Exception as exc:
        ok &= _check(False, f"output dir {output_dir}", f"not writable: {type(exc).__name__}")

    click.echo("API keys (values never printed):")
    gem = bool(cfg.gemini_api_key)
    claude = bool(cfg.anthropic_api_key)
    _check(gem, "GEMINI_API_KEY", "set" if gem else "NOT SET")
    _check(claude, "ANTHROPIC_API_KEY", "set" if claude else "NOT SET")

    click.echo("Models (configured names; only --live confirms acceptance):")
    click.echo(f"  gemini_text   = {cfg.gemini_text_model}")
    click.echo(f"  gemini_vision = {cfg.gemini_vision_model}")
    click.echo(f"  claude_text   = {cfg.claude_text_model}")
    click.echo(f"  claude_vision = {cfg.claude_vision_model}")
    click.echo(f"  claude_text_fallback = {'enabled' if cfg.enable_claude_text_fallback else 'disabled'}")

    click.echo("Capability summary:")
    _check(gem, "text extraction (Gemini primary)",
           "possible - not live-verified" if gem else "blocked: no GEMINI_API_KEY")
    _check(gem or claude, "vision extraction (Gemini + Claude fallback)",
           ("full chain" if (gem and claude) else "PARTIAL: one provider only")
           if (gem or claude) else "blocked: no keys")

    if live:
        click.echo("Live probes (tiny generated content, never an invoice):")
        probe_prompt = "Reply with exactly: OK"
        if gem:
            from invoice_extractor import gemini_client
            for label, model, contents in (
                ("gemini text", cfg.gemini_text_model, [probe_prompt]),
                ("gemini vision", cfg.gemini_vision_model, None),
            ):
                try:
                    if contents is None:
                        from google.genai import types as genai_types
                        contents = [probe_prompt,
                                    genai_types.Part.from_bytes(data=_tiny_png(),
                                                                mime_type="image/png")]
                    gemini_client._generate(cfg, model, contents)
                    ok &= _check(True, f"{label} ({model})", "model accepted")
                except Exception as exc:
                    ok &= _check(False, f"{label} ({model})", classify_probe_error(exc))
        else:
            click.echo("  [skip] gemini probes - no key")
        if claude:
            import base64
            from invoice_extractor import claude_client
            vision_content = [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": base64.standard_b64encode(_tiny_png()).decode()}},
                {"type": "text", "text": probe_prompt},
            ]
            for label, model, content in (
                ("claude text", cfg.claude_text_model, probe_prompt),
                ("claude vision", cfg.claude_vision_model, vision_content),
            ):
                try:
                    claude_client._request(cfg, model, content)
                    ok &= _check(True, f"{label} ({model})", "model accepted")
                except Exception as exc:
                    ok &= _check(False, f"{label} ({model})", classify_probe_error(exc))
        else:
            click.echo("  [skip] claude probes - no key")
    else:
        click.echo("(offline mode - pass --live for minimal provider probes)")

    if not ok:
        raise SystemExit(1)
    if gem and claude:
        click.echo("doctor: all checks passed - ready for live extraction")
    else:
        click.echo("doctor: environment OK - NOT ready for live extraction "
                   "(missing API key(s); offline commands still work)")


if __name__ == "__main__":
    cli()
