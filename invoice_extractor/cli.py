"""CLI entry points.

  run       full pipeline: page-level classify -> LLM -> merge -> validate -> Excel
  classify  stage test: per-page classification report (no API calls)
  render    stage test: render image pages to PNGs on disk (no API calls)
  doctor    environment/config health check (no API calls unless --live)
"""

import importlib
import os
import sys
from pathlib import Path

import click

from invoice_extractor import pdf_utils
from invoice_extractor.config import describe_models, load_config, provider_key_status
from invoice_extractor.excel_export import export_workbook
from invoice_extractor.logging_setup import exc_summary, new_run_id, setup_logging
from invoice_extractor.pipeline import InvoiceResult, find_pdfs, process_directory
from invoice_extractor.usage import format_usage_summary, usage_csv_path, write_usage_csv


@click.group()
def cli():
    """Batch invoice PDF extraction to Excel."""


def print_summary(results: list[InvoiceResult], output_path: Path,
                  usage_records=None, interrupted: bool = False) -> None:
    """Operator-facing summary. Counts and a file path only - never secrets,
    provider config, or raw LLM prompts/responses."""
    processed = len(results)
    successful = sum(1 for r in results
                     if r.extraction_method in ("text", "vision", "mixed"))
    line_items = sum(len(r.invoice.line_items) for r in results)
    needs_review = sum(1 for r in results if r.needs_review)
    errors = sum(1 for r in results if r.error)
    elapsed = sum(r.elapsed_seconds for r in results)
    by_method: dict[str, int] = {}
    for r in results:
        by_method[r.extraction_method] = by_method.get(r.extraction_method, 0) + 1

    click.echo("")
    click.echo("=" * 52)
    if interrupted:
        click.echo("  ** INTERRUPTED (Ctrl+C) - partial results below **")
    click.echo(f"  Files processed:      {processed}")
    click.echo(f"  Invoices extracted:   {successful}")
    click.echo(f"  Line items extracted: {line_items}")
    click.echo(f"  Needs review:         {needs_review}")
    click.echo(f"  Failed/problem:       {errors}")
    for method, count in sorted(by_method.items()):
        click.echo(f"    - {method}: {count}")
    if usage_records:
        from decimal import Decimal
        reqs = len(usage_records)
        repair = sum(1 for u in usage_records if u.attempt_type == "repair")
        escalation = sum(1 for u in usage_records if u.attempt_type == "escalation")
        unknown = sum(1 for u in usage_records if u.cost_usd is None)
        cost = sum((u.cost_usd or Decimal("0") for u in usage_records), Decimal("0"))
        click.echo(f"  Provider requests:    {reqs} "
                   f"(repair={repair} escalation={escalation})")
        click.echo(f"  Reported cost (USD):  {cost}"
                   + (f" (incomplete: {unknown} unknown-cost request(s))" if unknown else ""))
    click.echo(f"  Total elapsed:        {elapsed:.1f}s")
    click.echo(f"  Output written:       {output_path}")
    click.echo("=" * 52)


@cli.command()
@click.option("--input", "input_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Folder containing invoice PDFs.")
@click.option("--output", "output_path", default="./output/results.xlsx",
              type=click.Path(dir_okay=False, path_type=Path), show_default=True,
              help="Excel workbook to write.")
@click.option("--log-file", default=None, type=click.Path(dir_okay=False, path_type=Path),
              help="Write a persistent run log to this path. Omit for console-only "
                   "logging (there is no automatic ./output/run.log anymore).")
@click.option("--run-metadata", "run_metadata_path", default=None,
              type=click.Path(dir_okay=False, path_type=Path),
              help="OPT-IN: write a small run-metadata JSON (run id, timestamps, status, "
                   "and per-file runtime/method/provider/model/needs_review/error/completed/"
                   "request_count/cost - NO invoice content). Omit for none.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Replace existing output artifacts. Without it, the run REFUSES (before "
                   "any provider call) if the workbook, its .usage.csv, or the run-metadata "
                   "JSON already exists.")
def run(input_dir: Path, output_path: Path, log_file: Path | None,
        run_metadata_path: Path | None, overwrite: bool):
    """Run the full extraction pipeline over a folder of PDFs.

    Exit-code policy (invoice-level review is NOT a program failure - see
    README "Review outcomes vs program failure"):

      0    batch completed and outputs were written - even if some or every
           invoice is needs_review; also 0 when no PDFs are found
      2    --input does not exist (enforced by click before this runs)
      130  interrupted by the operator (Ctrl+C): new calls/retries stop, a
           valid PARTIAL workbook/usage CSV is written if >=1 file completed
           (none if 0 completed), no traceback
      1    config/log/output error, output collision without --overwrite, an
           unwritable output location, the batch could not complete, or an
           output could not be written (fatal tool-level failures)
    """
    import json
    from datetime import datetime, timezone

    from invoice_extractor.atomic import StagedArtifacts
    from invoice_extractor.pipeline import BatchInterrupted

    try:
        cfg = load_config()
    except Exception as exc:
        raise SystemExit(f"FATAL: configuration error: {exc_summary(exc)}")

    run_id = new_run_id()
    try:
        logger = setup_logging(
            log_file, run_id=run_id,
            secrets=(cfg.gemini_api_key or "", cfg.anthropic_api_key or "",
                    cfg.openrouter_api_key or ""),
        )
    except Exception as exc:
        raise SystemExit(f"FATAL: cannot create log location: {exc_summary(exc)}")

    logger.info("run %s starting; %s", run_id, describe_models(cfg))

    key_status = provider_key_status(cfg)
    if cfg.llm_gateway == "direct":
        if not key_status["gemini"]:
            logger.warning("GEMINI_API_KEY not set - Gemini calls will fail")
        if not key_status["anthropic"]:
            logger.warning("ANTHROPIC_API_KEY not set - Claude fallback is unavailable")

    # No-safety-limits warning (OpenRouter only, once per run): a run with no
    # per-file/run attempt or cost cap can issue an unbounded number of paid
    # calls on a large/dense document. Non-blocking - just a safe pointer.
    if cfg.llm_gateway == "openrouter" and (
        cfg.max_model_attempts_per_file is None
        and cfg.max_cost_usd_per_file is None
        and cfg.max_cost_usd_per_run is None
    ):
        logger.warning(
            "no OpenRouter safety limits configured (MAX_MODEL_ATTEMPTS_PER_FILE, "
            "MAX_COST_USD_PER_FILE, MAX_COST_USD_PER_RUN all unset); paid-call count "
            "is bounded only by chunks x models x retries - see docs/OPERATIONS.md"
        )

    # Nothing to do: return BEFORE any output preflight so an empty input dir
    # never creates or probes the output location (keeps this path hermetic).
    if not find_pdfs(input_dir):
        click.echo(f"No PDFs found in {input_dir} - nothing to do.")
        return

    # --- Output preflight: writability + collision, BEFORE any provider call ---
    if output_path.exists() and output_path.is_dir():
        raise SystemExit(f"FATAL: --output {output_path} is a directory, not a file")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        probe = output_path.parent / f".preflight-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        raise SystemExit(
            f"FATAL: output location {output_path.parent} is not writable "
            f"({exc_summary(exc)})"
        )

    # Planned artifact set (one set - see StagedArtifacts): workbook always;
    # usage CSV only under OpenRouter; run metadata only when requested.
    usage_path = usage_csv_path(output_path) if cfg.llm_gateway == "openrouter" else None
    planned = [output_path]
    if usage_path is not None:
        planned.append(usage_path)
    if run_metadata_path is not None:
        planned.append(run_metadata_path)
    collisions = [p for p in planned if p.exists()]
    if collisions and not overwrite:
        listing = ", ".join(str(p) for p in collisions)
        raise SystemExit(
            f"FATAL: output already exists ({listing}); no provider calls were made. "
            "Re-run with --overwrite to replace, or choose a different --output."
        )

    started_at = datetime.now(timezone.utc).isoformat()
    interrupted = False
    try:
        results = process_directory(input_dir, cfg, logger, run_id=run_id)
    except BatchInterrupted as bi:
        results = bi.results
        interrupted = True
    except Exception as exc:
        logger.error("batch did not complete: %s", exc_summary(exc))
        raise SystemExit(f"FATAL: batch did not complete: {exc_summary(exc)}")
    finished_at = datetime.now(timezone.utc).isoformat()

    # Interrupted with zero recorded files: per policy, write no output.
    if interrupted and not results:
        click.echo("Interrupted before any file completed - no output written.")
        raise SystemExit(130)

    usage_records = [r for res in results for r in res.usage_records]

    def _write_metadata(dst: Path):
        meta = {
            "run_id": run_id, "started_at": started_at, "finished_at": finished_at,
            "interrupted": interrupted, "exit_code": 130 if interrupted else 0,
            "input_dir": str(input_dir),
            "output_artifacts": sorted(p.name for p in planned),
            "files": [_safe_run_metadata_row(r) for r in results],
        }
        dst.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    # Stage ALL temp artifacts, then replace finals together (atomic per file;
    # temps are all written before any final is touched - a failure at any
    # stage leaves every existing final untouched). See atomic.StagedArtifacts.
    try:
        with StagedArtifacts() as stage:
            stage.stage(output_path, lambda p: export_workbook(results, p))
            if usage_path is not None:
                stage.stage(usage_path, lambda p: write_usage_csv(usage_records, p))
            if run_metadata_path is not None:
                stage.stage(run_metadata_path, _write_metadata)
            stage.commit()
    except Exception as exc:
        logger.error("outputs could not be written: %s", exc_summary(exc))
        raise SystemExit(
            f"FATAL: outputs could not be written ({exc_summary(exc)}); "
            "any existing outputs were left unchanged"
        )

    if usage_path is not None:
        logger.info("Wrote %s", usage_path)
        click.echo(format_usage_summary(usage_records, len(results)))
    if run_metadata_path is not None:
        logger.info("Wrote %s", run_metadata_path)
    logger.info("Wrote %s%s", output_path,
                f" (log: {log_file})" if log_file else "")

    print_summary(results, output_path, usage_records=usage_records,
                  interrupted=interrupted)
    if interrupted:
        click.echo("Interrupted (Ctrl+C): wrote partial output for completed files.")
        raise SystemExit(130)


def _safe_run_metadata_row(r) -> dict:
    """One run-metadata file row: SAFE fields only - never review reasons,
    invoice values, text, prompts, responses, stack traces, or image data."""
    reqs = sum(1 for _ in r.usage_records)
    unknown = sum(1 for u in r.usage_records if u.cost_usd is None)
    from decimal import Decimal
    cost = sum((u.cost_usd or Decimal("0") for u in r.usage_records), Decimal("0"))
    completed = not r.error and "interrupted by operator" not in (r.review_reason or "")
    return {
        "source_file": r.source_file,
        "elapsed_seconds": round(r.elapsed_seconds, 3),
        "extraction_method": r.extraction_method,
        "provider": r.provider, "model": r.model,
        "needs_review": r.needs_review, "error": r.error,
        "completed": completed,
        "interrupted": "interrupted by operator" in (r.review_reason or ""),
        "request_count": reqs, "reported_cost": str(cost),
        "unknown_cost_count": unknown,
    }


# --- benchmark (M6): offline ground-truth scoring; makes NO provider calls ----

@cli.group()
def benchmark():
    """Offline ground-truth benchmark tooling (no network/provider calls)."""


@benchmark.command("score")
@click.option("--manifest", "manifest_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Benchmark manifest JSON.")
@click.option("--workbook", "workbook_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Extraction workbook to score (results.xlsx).")
@click.option("--usage", "usage_path", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Extraction usage CSV (results.usage.csv), if available.")
@click.option("--run-metadata", "run_metadata_path", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Optional run-metadata JSON for end-to-end runtime.")
@click.option("--thresholds", "thresholds_path", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Optional thresholds JSON; failures set a nonzero exit code.")
@click.option("--enable-fuzzy-line-matching", is_flag=True, default=False,
              help="Enable the optional fuzzy description matching tier (OFF by default).")
@click.option("--fuzzy-threshold", default="0.90", show_default=True,
              help="Fuzzy match ratio threshold (only used with fuzzy matching enabled).")
@click.option("--output", "output_path", default="./output/benchmark_report.xlsx",
              type=click.Path(dir_okay=False, path_type=Path), show_default=True,
              help="Report workbook path (a sibling .json summary is written too).")
@click.option("--overwrite", is_flag=True, default=False,
              help="Replace existing report outputs. Without it, scoring REFUSES if the "
                   "report workbook or its .json summary already exists.")
def benchmark_score(manifest_path, workbook_path, usage_path, run_metadata_path,
                    thresholds_path, enable_fuzzy_line_matching, fuzzy_threshold,
                    output_path, overwrite):
    """Score an extraction workbook against benchmark ground truth (offline).

    Exit codes:
      0  scoring completed and all supplied thresholds passed (or none supplied)
      1  one or more supplied thresholds failed
      2  invalid benchmark manifest / ground truth / config (nothing scored)
    """
    from decimal import Decimal, InvalidOperation

    from invoice_extractor.benchmark.dataset import (
        BenchmarkConfigError, load_manifest, load_thresholds,
    )
    from invoice_extractor.benchmark.report import write_report
    from invoice_extractor.benchmark.scoring import score_benchmark

    try:
        dataset = load_manifest(manifest_path)
        if thresholds_path is not None:
            dataset.thresholds = load_thresholds(thresholds_path)
        dataset.fuzzy_enabled = bool(enable_fuzzy_line_matching)
        try:
            dataset.fuzzy_threshold = Decimal(str(fuzzy_threshold))
        except InvalidOperation:
            raise BenchmarkConfigError(f"--fuzzy-threshold {fuzzy_threshold!r} is not a decimal")
    except BenchmarkConfigError as exc:
        # Exit code 2 for benchmark configuration / ground-truth errors
        # (distinct from 1 = threshold failure). SystemExit(str) would yield
        # exit 1, so echo the message and raise an explicit integer code.
        click.echo(f"BENCHMARK CONFIG ERROR: {exc}", err=True)
        raise SystemExit(2)

    output_path = Path(output_path)
    json_path = output_path.with_suffix(".json")
    collisions = [p for p in (output_path, json_path) if p.exists()]
    if collisions and not overwrite:
        listing = ", ".join(str(p) for p in collisions)
        raise SystemExit(
            f"FATAL: report output already exists ({listing}); re-run with --overwrite "
            "to replace, or choose a different --output."
        )

    report = score_benchmark(
        dataset, workbook_path, usage_path=usage_path,
        run_metadata_path=run_metadata_path,
    )

    # Both artifacts written as one staged set (temps first, then replace).
    write_report(report, output_path, json_path)

    a = report.aggregates
    click.echo(f"Benchmark: {a['num_cases']} case(s), "
               f"header micro acc={a['header_micro_accuracy']}, "
               f"line F1={a['line_f1']}, "
               f"review F1={a['review_f1']}")
    click.echo(f"Total reported cost: {a['total_reported_cost']}"
               + (f" (incomplete: {a['unknown_cost_requests']} unknown-cost request(s))"
                  if a["cost_incomplete"] else ""))
    if report.errors:
        click.echo(f"Errors sheet: {len(report.errors)} anomaly row(s) "
                   "(missing/duplicate/extra workbook cases)")
    for t in report.threshold_results:
        click.echo(f"  threshold {t['threshold']}: target={t['target']} "
                   f"actual={t['actual']} {'PASS' if t['passed'] else 'FAIL'}")
    click.echo(f"Wrote {output_path} and {json_path}")

    if report.threshold_results and not report.thresholds_passed:
        raise SystemExit(1)


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
              help="Make one minimal request per selected provider/route to confirm "
                   "the configured models are accepted. Sends a tiny generated probe, "
                   "NEVER an invoice. Costs a few tokens.")
@click.option("--provider", type=click.Choice(["gemini", "claude", "all"]),
              default="all", show_default=True,
              help="Restrict --live probes to one provider.")
@click.option("--route", type=click.Choice(["text", "vision", "all"]),
              default="all", show_default=True,
              help="Restrict --live probes to one route.")
def doctor(input_dir: Path, output_dir: Path, live: bool, provider: str, route: str):
    """Health check: environment, dependencies, paths, keys, models.

    Offline by default. With --live, makes the smallest practical provider
    request per configured model to confirm acceptance. --provider/--route
    narrow which of the four probes run (default: all four).
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
    key_status = provider_key_status(cfg)
    gem = key_status["gemini"]
    claude = key_status["anthropic"]
    _check(gem, "GEMINI_API_KEY", "set" if gem else "NOT SET")
    _check(claude, "ANTHROPIC_API_KEY", "set" if claude else "NOT SET")

    click.echo("Provider roles (fixed by design - not a user-selectable choice):")
    click.echo("  text route:   Gemini only, unless ENABLE_CLAUDE_TEXT_FALLBACK=true "
               "(then Claude is the fallback)")
    click.echo("  vision route: Gemini primary, Claude fallback (always on if its key is set)")

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

    # OpenRouter gateway readiness - offline, no paid calls, keys never printed.
    click.echo(f"Gateway: {cfg.llm_gateway}")
    if cfg.llm_gateway == "openrouter":
        click.echo("OpenRouter (offline checks; --live does not probe OpenRouter):")
        or_key = bool(cfg.openrouter_api_key)
        _check(or_key, "OPENROUTER_API_KEY", "set" if or_key else "NOT SET")
        _check(bool(cfg.openrouter_text_models), "OPENROUTER_TEXT_MODELS",
               ", ".join(cfg.openrouter_text_models) or "NOT SET")
        _check(bool(cfg.openrouter_vision_models), "OPENROUTER_VISION_MODELS",
               ", ".join(cfg.openrouter_vision_models) or "NOT SET (vision pages will fail)")
        click.echo(f"  structured_output = {cfg.openrouter_structured_output}")
        click.echo(f"  MAX_TEXT_PAGES={cfg.max_text_pages}  MAX_VISION_PAGES={cfg.max_vision_pages}"
                   f"  (dense scans: pilot MAX_VISION_PAGES=1-2 to avoid truncation)")
        click.echo(f"  MAX_RETRIES={cfg.max_retries}  "
                   f"REQUEST_TIMEOUT_SECONDS={cfg.request_timeout_seconds}")
        limits = []
        if cfg.max_model_attempts_per_file is not None:
            limits.append(f"MAX_MODEL_ATTEMPTS_PER_FILE={cfg.max_model_attempts_per_file}")
        if cfg.max_cost_usd_per_file is not None:
            limits.append(f"MAX_COST_USD_PER_FILE={cfg.max_cost_usd_per_file}")
        if cfg.max_cost_usd_per_run is not None:
            limits.append(f"MAX_COST_USD_PER_RUN={cfg.max_cost_usd_per_run}")
        if limits:
            click.echo("  safety limits: " + ", ".join(limits))
        else:
            click.echo("  [WARN] no safety limits configured (MAX_MODEL_ATTEMPTS_PER_FILE / "
                       "MAX_COST_USD_PER_FILE / MAX_COST_USD_PER_RUN all unset) - see "
                       "docs/OPERATIONS.md")
    click.echo("Output policy: existing outputs are NOT overwritten unless --overwrite "
               "is passed (run and benchmark score).")
    click.echo(f"Debug artifacts: {'ENABLED' if cfg.save_debug_artifacts else 'disabled'} "
               "(when enabled, failed responses may contain invoice content)")

    if live:
        run_gemini = provider in ("gemini", "all")
        run_claude = provider in ("claude", "all")
        run_text = route in ("text", "all")
        run_vision = route in ("vision", "all")
        click.echo(f"Live probes (tiny generated content, never an invoice; "
                   f"provider={provider} route={route}):")
        probe_prompt = "Reply with exactly: OK"
        if run_gemini and gem:
            from invoice_extractor import gemini_client
            candidates = []
            if run_text:
                candidates.append(("gemini text", cfg.gemini_text_model, [probe_prompt]))
            if run_vision:
                candidates.append(("gemini vision", cfg.gemini_vision_model, None))
            for label, model, contents in candidates:
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
        elif run_gemini:
            click.echo("  [skip] gemini probes - no key")
        if run_claude and claude:
            import base64
            from invoice_extractor import claude_client
            vision_content = [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": base64.standard_b64encode(_tiny_png()).decode()}},
                {"type": "text", "text": probe_prompt},
            ]
            candidates = []
            if run_text:
                candidates.append(("claude text", cfg.claude_text_model, probe_prompt))
            if run_vision:
                candidates.append(("claude vision", cfg.claude_vision_model, vision_content))
            for label, model, content in candidates:
                try:
                    claude_client._request(cfg, model, content)
                    ok &= _check(True, f"{label} ({model})", "model accepted")
                except Exception as exc:
                    ok &= _check(False, f"{label} ({model})", classify_probe_error(exc))
        elif run_claude:
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
