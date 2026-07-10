"""CLI entry points.

  run       full pipeline: extract -> LLM -> validate -> Excel
  classify  stage test: text extraction + text/image classification (no API calls)
  render    stage test: render image-only pages to PNGs on disk (no API calls)
"""

from pathlib import Path

import click

from invoice_extractor import pdf_utils
from invoice_extractor.config import load_config
from invoice_extractor.excel_export import export_workbook
from invoice_extractor.logging_setup import setup_logging
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
        key = r.extraction_method or "failed"
        by_method[key] = by_method.get(key, 0) + 1

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
    log_path = log_file or output_path.parent / "run.log"
    logger = setup_logging(log_path)

    if not cfg.gemini_api_key:
        logger.warning("GEMINI_API_KEY not set - Gemini calls will fail and fall back to Claude")
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
    """Stage test: show per-file text extraction quality and classification.

    Makes no API calls - use this to sanity-check the text/image split
    before spending tokens.
    """
    cfg = load_config()
    pdfs = find_pdfs(input_dir)
    if not pdfs:
        raise SystemExit(f"No PDFs found in {input_dir}")

    click.echo(f"{'file':<45} {'pages':>5} {'avg chars/page':>15}  classification")
    click.echo("-" * 85)
    for path in pdfs:
        try:
            pages_text = pdf_utils.extract_pages_text(str(path))
        except Exception as exc:
            click.echo(f"{path.name:<45} {'?':>5} {'?':>15}  ERROR: {exc}")
            continue
        avg = pdf_utils.avg_alnum_per_page(pages_text)
        cls = pdf_utils.classify_pages(pages_text, cfg.text_quality_threshold)
        label = "text-native" if cls == pdf_utils.METHOD_TEXT else "image-only"
        click.echo(f"{path.name:<45} {len(pages_text):>5} {avg:>15.0f}  {label}")


@cli.command()
@click.option("--input", "input_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "output_dir", default="./output/pages",
              type=click.Path(file_okay=False, path_type=Path), show_default=True)
@click.option("--all", "render_all", is_flag=True,
              help="Render every PDF, not just image-only ones.")
def render(input_dir: Path, output_dir: Path, render_all: bool):
    """Stage test: render pages to PNGs on disk (what the vision API would see).

    By default only renders PDFs classified as image-only. No API calls.
    """
    cfg = load_config()
    pdfs = find_pdfs(input_dir)
    if not pdfs:
        raise SystemExit(f"No PDFs found in {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = 0
    for path in pdfs:
        try:
            pages_text = pdf_utils.extract_pages_text(str(path))
            cls = pdf_utils.classify_pages(pages_text, cfg.text_quality_threshold)
            if cls == pdf_utils.METHOD_TEXT and not render_all:
                click.echo(f"{path.name}: text-native, skipped (use --all to force)")
                continue
            images = pdf_utils.render_pages_png(str(path), dpi=cfg.render_dpi,
                                                max_pages=cfg.max_vision_pages)
        except Exception as exc:
            click.echo(f"{path.name}: ERROR: {exc}")
            continue
        for i, png in enumerate(images, start=1):
            out = output_dir / f"{path.stem}_p{i}.png"
            out.write_bytes(png)
        click.echo(f"{path.name}: rendered {len(images)} page(s) at {cfg.render_dpi} DPI")
        rendered += 1
    click.echo(f"\nRendered {rendered} PDF(s) to {output_dir}")


if __name__ == "__main__":
    cli()
