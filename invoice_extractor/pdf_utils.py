"""Page-level PDF analysis: per-page classification and selective rendering.

Every page is classified independently:

  text  - alphanumeric chars on the page exceed the quality threshold;
          its extracted text goes down the text route.
  image - below the threshold but the page carries images or vector drawings
          (typical scan), or has a little text not worth trusting; the page
          is rendered and goes down the vision route.
  blank - no alphanumeric text, no images, no drawings (includes
          punctuation-only pages). Skipped but recorded.

A mixed PDF therefore uses text extraction for some pages and vision for
others; the whole file is never forced down one route.
"""

from dataclasses import dataclass

import fitz  # PyMuPDF

PAGE_TEXT = "text"
PAGE_IMAGE = "image"
PAGE_BLANK = "blank"

DOC_TEXT_NATIVE = "text-native"
DOC_IMAGE_ONLY = "image-only"
DOC_MIXED = "mixed"
DOC_ERROR = "error"


@dataclass(frozen=True)
class PageInfo:
    number: int  # 1-based
    kind: str  # PAGE_TEXT | PAGE_IMAGE | PAGE_BLANK
    alnum_chars: int
    text: str


def alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def _classify_page(page: fitz.Page, text: str, threshold: int) -> str:
    chars = alnum_count(text)
    if chars > threshold:
        return PAGE_TEXT
    # Below threshold: a scanned page carries an embedded image (or vector
    # drawings); a page with neither and no alnum text is blank.
    if page.get_images(full=True) or page.get_drawings():
        return PAGE_IMAGE
    if chars > 0:
        return PAGE_IMAGE  # a little text, not enough to trust: render it
    return PAGE_BLANK


def analyze_pages(path: str, threshold: int) -> list[PageInfo]:
    """Classify every page. Raises on unreadable/corrupt PDFs."""
    pages: list[PageInfo] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            kind = _classify_page(page, text, threshold)
            pages.append(
                PageInfo(number=i, kind=kind, alnum_chars=alnum_count(text), text=text)
            )
    return pages


def classify_document(pages: list[PageInfo]) -> str:
    kinds = {p.kind for p in pages if p.kind != PAGE_BLANK}
    if not kinds:
        return DOC_ERROR  # nothing meaningful (all pages blank, or zero pages)
    if kinds == {PAGE_TEXT}:
        return DOC_TEXT_NATIVE
    if kinds == {PAGE_IMAGE}:
        return DOC_IMAGE_ONLY
    return DOC_MIXED


def format_page_ranges(numbers: list[int]) -> str:
    """Stable human-readable page list: [1,2,5,6,7] -> '1-2,5-7'.

    This is the documented representation used in Excel provenance columns,
    review reasons, and logs (never Python list syntax).
    """
    if not numbers:
        return ""
    nums = sorted(set(numbers))
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = n
    parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(parts)


def render_pages_png(path: str, page_numbers: list[int], dpi: int = 200) -> list[bytes]:
    """Render the given 1-based pages to PNG bytes, in the order given."""
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    wanted = set(page_numbers)
    rendered: dict[int, bytes] = {}
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            if i in wanted:
                rendered[i] = page.get_pixmap(matrix=matrix).tobytes("png")
    return [rendered[n] for n in page_numbers if n in rendered]
