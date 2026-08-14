"""Page classification and normalized page text (Build 2).

Every page becomes a PageText: an ordered set of positioned text spans that
looks the same to the parser whether it came from embedded PDF text or from
local OCR. Rules:

  * embedded text first - OCR only when a page has no usable embedded text;
  * classification is PER PAGE, never for the whole PDF at once;
  * a failing page is recorded as unreadable and never discards the others;
  * rendered images live in memory only (never written to disk) and are
    kept on the PageText solely so the parser can run cell-rescue OCR.
"""

from dataclasses import dataclass, field

import fitz  # PyMuPDF - existing core dependency

from apps.web.transfer.extraction_models import (
    METHOD_EMBEDDED,
    METHOD_OCR,
    METHOD_UNREADABLE,
)
from apps.web.transfer.ocr import OcrAdapter, OcrError

# Same idea as the invoice classifier's text-quality threshold: below this
# many alphanumeric characters the embedded text layer is not usable.
USABLE_TEXT_MIN_ALNUM = 20

OCR_DPI = 200


@dataclass(frozen=True)
class TextSpan:
    """One positioned text fragment (a PDF word or an OCR token). Units are
    page points for embedded text and image pixels for OCR - consistent
    within a page, which is all the parser needs."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 1.0


@dataclass
class PageText:
    page_number: int                      # 1-based, PDF order
    method: str                           # embedded_text | ocr | unreadable
    spans: list[TextSpan] = field(default_factory=list)
    error: str | None = None
    image_png: bytes | None = None        # OCR pages only; memory-only


def _embedded_spans(page: fitz.Page) -> list[TextSpan]:
    spans = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if word.strip():
            spans.append(TextSpan(text=word, x0=x0, y0=y0, x1=x1, y1=y1))
    return spans


def _usable(spans: list[TextSpan]) -> bool:
    alnum = sum(1 for s in spans for ch in s.text if ch.isalnum())
    return alnum >= USABLE_TEXT_MIN_ALNUM


def extract_page_texts(pdf_path: str, *,
                       ocr_adapter: OcrAdapter | None,
                       dpi: int = OCR_DPI) -> list[PageText]:
    """Classify and read every page of one PDF, in page order."""
    pages: list[PageText] = []
    with fitz.open(pdf_path) as doc:
        for index in range(doc.page_count):
            number = index + 1
            try:
                spans = _embedded_spans(doc[index])
            except Exception:
                pages.append(PageText(number, METHOD_UNREADABLE,
                                      error="page could not be read"))
                continue
            if _usable(spans):
                pages.append(PageText(number, METHOD_EMBEDDED, spans=spans))
                continue
            pages.append(_ocr_page(doc, index, ocr_adapter, dpi))
    return pages


def _ocr_page(doc: fitz.Document, index: int,
              adapter: OcrAdapter | None, dpi: int) -> PageText:
    number = index + 1
    if adapter is None:
        return PageText(number, METHOD_UNREADABLE, error="ocr_unavailable")
    try:
        zoom = dpi / 72.0
        png = doc[index].get_pixmap(
            matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
    except Exception:
        return PageText(number, METHOD_UNREADABLE,
                        error="page could not be rendered")
    try:
        tokens = adapter.recognize_page(png)
    except OcrError as exc:
        return PageText(number, METHOD_UNREADABLE, error=str(exc))
    except Exception:
        return PageText(number, METHOD_UNREADABLE, error="ocr failed")
    if not tokens:
        return PageText(number, METHOD_UNREADABLE,
                        error="no text recognized", image_png=png)
    spans = [TextSpan(text=t.text, x0=t.x0, y0=t.y0, x1=t.x1, y1=t.y1,
                      confidence=t.confidence) for t in tokens]
    return PageText(number, METHOD_OCR, spans=spans, image_png=png)
