"""Basic smoke test: run the pipeline against whatever PDFs are in samples/
and print a summary. Usage:

    python test_pipeline.py [samples_dir]

Requires .env with API keys (see README.md). Exits non-zero if any file
errored outright.
"""

import sys
from pathlib import Path

from invoice_extractor.config import load_config
from invoice_extractor.excel_export import export_workbook
from invoice_extractor.logging_setup import setup_logging
from invoice_extractor.pipeline import process_directory


def main() -> int:
    samples_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./samples")
    if not samples_dir.is_dir():
        print(f"Samples folder not found: {samples_dir}")
        return 2

    cfg = load_config()
    logger = setup_logging("./output/run.log")
    results = process_directory(samples_dir, cfg, logger)

    if not results:
        print(f"No PDFs in {samples_dir} - drop some sample invoices there and rerun.")
        return 2

    output_path = export_workbook(results, "./output/results.xlsx")

    processed = len(results)
    needs_review = sum(1 for r in results if r.needs_review)
    errors = sum(1 for r in results if r.error)

    print("\n--- test_pipeline summary ---")
    print(f"processed:    {processed}")
    print(f"needs_review: {needs_review}")
    print(f"errors:       {errors}")
    for r in results:
        status = "ERROR" if r.error else ("REVIEW" if r.needs_review else "ok")
        print(f"  [{status:<6}] {r.source_file}  method={r.extraction_method or '-'} "
              f"provider={r.provider or '-'}  {r.elapsed_seconds:.1f}s"
              + (f"  ({r.review_reason})" if r.review_reason else ""))
    print(f"\nworkbook: {output_path}")
    print("log:      output/run.log")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
