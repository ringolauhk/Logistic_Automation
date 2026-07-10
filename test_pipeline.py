"""Basic smoke script: run the pipeline against whatever PDFs are in samples/
and print a summary. Usage:

    python test_pipeline.py [samples_dir]

Exit-code policy (invoice-level review is NOT a program failure):

  0  batch completed and outputs were written - even if some or every
     invoice is needs_review (e.g. missing API keys, provider failures)
  0  no PDFs found (clear message, nothing to do)
  2  input path cannot be accessed (fatal tool failure)
  1  workbook cannot be written, or an uncaught orchestration failure
     prevents the batch from completing (fatal tool failure)

For the offline test suite, run `pytest` instead.
"""

import sys
from pathlib import Path

from invoice_extractor.config import load_config
from invoice_extractor.excel_export import export_workbook
from invoice_extractor.logging_setup import exc_summary, new_run_id, setup_logging
from invoice_extractor.pipeline import find_pdfs, process_directory


def run_smoke(
    samples_dir: Path,
    output_path: Path = Path("./output/results.xlsx"),
    log_path: Path = Path("./output/run.log"),
) -> int:
    if not samples_dir.is_dir():
        print(f"FATAL: samples folder not found or not accessible: {samples_dir}")
        return 2

    cfg = load_config()
    try:
        logger = setup_logging(
            log_path, run_id=new_run_id(),
            secrets=(cfg.gemini_api_key or "", cfg.anthropic_api_key or ""),
        )
    except Exception as exc:
        print(f"FATAL: cannot create log/output location: {exc_summary(exc)}")
        return 1

    discovered = len(find_pdfs(samples_dir))
    if discovered == 0:
        print(f"No PDFs found in {samples_dir} - nothing to do. "
              "Drop sample invoice PDFs there and rerun.")
        return 0

    try:
        results = process_directory(samples_dir, cfg, logger)
    except Exception as exc:
        print(f"FATAL: batch did not complete: {exc_summary(exc)}")
        return 1

    try:
        written = export_workbook(results, output_path)
    except Exception as exc:
        print(f"FATAL: workbook could not be written to {output_path}: {exc_summary(exc)}")
        return 1

    processed = len(results)
    successful = sum(1 for r in results
                     if r.extraction_method in ("text", "vision", "mixed"))
    needs_review = sum(1 for r in results if r.needs_review)
    provider_failures = sum(1 for r in results if r.error)

    print("\n--- test_pipeline summary ---")
    print(f"PDFs discovered:                  {discovered}")
    print(f"PDFs processed:                   {processed}")
    print(f"successful structured extractions:{successful:>4}")
    print(f"needs-review invoices:            {needs_review}")
    print(f"provider/config failures:         {provider_failures}")
    print("unexpected fatal errors:          0")
    for r in results:
        status = "FAILED" if r.error else ("REVIEW" if r.needs_review else "ok")
        print(f"  [{status:<6}] {r.source_file}  class={r.document_classification} "
              f"method={r.extraction_method} provider={r.provider} "
              f"model={r.model or '-'} chunks={r.vision_chunk_count}  "
              f"{r.elapsed_seconds:.1f}s")
    print(f"\nworkbook: {written}")
    print(f"log:      {log_path}")
    if provider_failures:
        print("\nNote: provider/config failures above are invoice-level review "
              "outcomes (e.g. missing API keys), not a tool failure - exit 0.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    samples_dir = Path(args[0]) if args else Path("./samples")
    return run_smoke(samples_dir)


if __name__ == "__main__":
    raise SystemExit(main())
