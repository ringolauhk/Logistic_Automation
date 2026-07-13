"""Manual generator for the synthetic invoice validation pack's PDFs.

Explicit, human-invoked command only - never runs during import or normal
pytest collection. Writes PDFs for manual visual/structural review; does NOT
call Gemini/Claude, does NOT call the invoice_extractor pipeline, and does
NOT produce Excel output. Requires no .env.

Usage:
    python scripts/generate_synthetic_fixtures.py --output ./output/synthetic_fixture_review
    python scripts/generate_synthetic_fixtures.py --output ./output/synthetic_fixture_review --fixture fixture_04_eur_european_number_format
    python scripts/generate_synthetic_fixtures.py --output ./output/synthetic_fixture_review --force
"""

import argparse
import hashlib
import sys
from pathlib import Path

# Allow running as `python scripts/generate_synthetic_fixtures.py` from the
# project root without installing the package - Python only puts this
# script's own directory (scripts/) on sys.path by default.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import fitz  # noqa: E402  (after sys.path setup)

from tests.synthetic_fixtures import scenarios as sc  # noqa: E402


def _page_kind(page: fitz.Page) -> str:
    """Structural inspection only - mirrors the pack's own vocabulary
    (text/image/blank), not a call into invoice_extractor.pdf_utils."""
    text_len = len(page.get_text("text").strip())
    if text_len > 20:
        return "text"
    if page.get_images(full=True) or page.get_drawings():
        return "image"
    if text_len > 0:
        return "image"
    return "blank"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect(path: Path) -> dict:
    doc = fitz.open(str(path))
    kinds = [_page_kind(doc[i]) for i in range(doc.page_count)]
    page_count = doc.page_count
    doc.close()
    return {
        "page_count": page_count,
        "page_kinds": kinds,
        "file_size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="Directory to write generated PDFs into.")
    parser.add_argument("--fixture", default=None,
                        help="Generate only this fixture_id (default: all ten).")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing PDFs. Without this flag, an "
                             "existing file refuses to be overwritten.")
    args = parser.parse_args(argv)

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.fixture is not None:
        try:
            spec = sc.get_scenario(args.fixture)
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        specs = [spec]
    else:
        specs = list(sc.list_scenarios())

    for spec in specs:
        target = output_dir / spec.filename
        if target.exists() and not args.force:
            print(f"ERROR: {target} already exists (use --force to overwrite)",
                  file=sys.stderr)
            return 1

    print(f"{'fixture_id':<45} {'filename':<38} {'pages':>5}  page kinds")
    print("-" * 120)
    for spec in specs:
        target = output_dir / spec.filename
        sc.build_scenario(spec.fixture_id, target)
        info = _inspect(target)
        kinds_str = ",".join(info["page_kinds"])
        print(f"{spec.fixture_id:<45} {spec.filename:<38} {info['page_count']:>5}  {kinds_str}")
        print(f"{'':<45} {'':<38} {'':>5}  size={info['file_size']}B sha256={info['sha256']}")

    print(f"\nWrote {len(specs)} PDF(s) to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
