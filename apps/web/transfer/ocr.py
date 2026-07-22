"""Local OCR adapter for the Transfer Note workflow (Build 2).

The invoice pipeline's OCR-capable route is a CLOUD vision model, which the
Transfer workflow must not use. Instead OCR is a small local interface with
one production implementation (RapidOCR / onnxruntime, fully offline) that
is an OPTIONAL dependency: install with

    pip install -r requirements-ocr.txt        # or: pip install .[transfer-ocr]

When the library is absent, get_default_adapter() returns None and scanned
pages are reported as issues instead of crashing. Automated tests use
deterministic fake adapters - no cloud provider is ever called from here,
and no rendered page image is ever written to disk (bytes stay in memory).
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OcrToken:
    """One recognized text fragment with page-pixel geometry."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 0.0


class OcrError(Exception):
    """OCR failed for a page (kept per-page; never aborts the document)."""


class OcrAdapter(Protocol):
    def recognize_page(self, png: bytes) -> list[OcrToken]:
        """All tokens on a rendered page image."""
        ...

    def recognize_cell(self, png: bytes,
                       box: tuple[float, float, float, float]) -> tuple[str, float]:
        """Recognition-only pass on one cropped cell (x0, y0, x1, y1).
        Used to rescue short values (e.g. single-letter sizes) that page
        detection misses. Returns ("", 0.0) when nothing is readable."""
        ...


class RapidOcrAdapter:
    """Offline OCR via rapidocr-onnxruntime. Lazy-imported so the web app
    works without the optional dependency installed."""

    def __init__(self) -> None:
        try:
            import cv2          # noqa: F401 - rapidocr hard dependency
            import numpy        # noqa: F401
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:      # pragma: no cover - env-specific
            raise OcrError(
                "Local OCR is not installed. Install the optional OCR "
                "dependency (requirements-ocr.txt) to process scanned "
                "Transfer Delivery Notes.") from exc
        self._engine = RapidOCR()

    def _decode(self, png: bytes):
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise OcrError("Page image could not be decoded.")
        return img

    def recognize_page(self, png: bytes) -> list[OcrToken]:
        result, _elapse = self._engine(self._decode(png))
        tokens: list[OcrToken] = []
        for item in result or []:
            box, text, score = item[0], item[1], item[2]
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            try:
                confidence = float(score)
            except (TypeError, ValueError):
                confidence = 0.0
            if str(text).strip():
                tokens.append(OcrToken(text=str(text), x0=min(xs), y0=min(ys),
                                       x1=max(xs), y1=max(ys),
                                       confidence=confidence))
        return tokens

    def recognize_cell(self, png: bytes,
                       box: tuple[float, float, float, float]) -> tuple[str, float]:
        img = self._decode(png)
        h, w = img.shape[:2]
        x0, y0, x1, y1 = box
        x0, x1 = max(0, int(x0)), min(w, int(x1))
        y0, y1 = max(0, int(y0)), min(h, int(y1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return "", 0.0
        crop = img[y0:y1, x0:x1]
        out, _elapse = self._engine.text_recognizer([crop])
        if not out:
            return "", 0.0
        text, score = out[0][0], out[0][1]
        return str(text).strip(), float(score)


def get_default_adapter() -> OcrAdapter | None:
    """The production adapter when the optional dependency is installed,
    else None (callers then record OCR_UNAVAILABLE issues per page)."""
    try:
        return RapidOcrAdapter()
    except OcrError:
        return None
