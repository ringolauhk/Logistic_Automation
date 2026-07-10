"""PDF text extraction, text/image classification, and page rendering."""

import fitz  # PyMuPDF

METHOD_TEXT = "text"
METHOD_IMAGE = "image"


def extract_pages_text(path: str) -> list[str]:
    """Return the extracted text of every page (empty string if none)."""
    with fitz.open(path) as doc:
        return [page.get_text("text") or "" for page in doc]


def alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def avg_alnum_per_page(pages_text: list[str]) -> float:
    if not pages_text:
        return 0.0
    return sum(alnum_count(t) for t in pages_text) / len(pages_text)


def classify_pages(pages_text: list[str], threshold: int) -> str:
    """'text' when the average alphanumeric chars per page exceeds threshold,
    else 'image' (scanned / no usable text layer)."""
    return METHOD_TEXT if avg_alnum_per_page(pages_text) > threshold else METHOD_IMAGE


def render_pages_png(path: str, dpi: int = 200, max_pages: int | None = None) -> list[bytes]:
    """Render each page to PNG bytes at the given DPI."""
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    images: list[bytes] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if max_pages is not None and i >= max_pages:
                break
            pix = page.get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
    return images
