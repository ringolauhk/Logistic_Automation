"""Per-file orchestration: classify -> extract -> validate."""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from invoice_extractor import claude_client, gemini_client, pdf_utils
from invoice_extractor.config import Config
from invoice_extractor.schema import empty_invoice, validate_invoice


@dataclass
class InvoiceResult:
    source_file: str
    data: dict = field(default_factory=empty_invoice)
    page_count: int = 0
    # "text" | "gemini_vision" | "claude_vision" | None (total failure)
    extraction_method: str | None = None
    # Provider that actually produced the result, for the log/summary.
    provider: str | None = None
    needs_review: bool = False
    review_reason: str | None = None
    error: bool = False
    elapsed_seconds: float = 0.0


def _extract_text_path(cfg: Config, logger: logging.Logger, name: str, full_text: str) -> tuple[dict, str]:
    """Gemini text normalization, with Claude text (never vision) as fallback."""
    try:
        inv = gemini_client.extract_from_text(cfg, full_text)
        return inv, "gemini_text"
    except Exception as exc:
        logger.warning("%s: Gemini text normalization failed (%s: %s); trying Claude text fallback",
                       name, type(exc).__name__, exc)
        inv = claude_client.extract_from_text(cfg, full_text)
        return inv, "claude_text"


def _extract_vision_path(cfg: Config, logger: logging.Logger, name: str, images: list[bytes]) -> tuple[dict, str, str]:
    """Gemini vision first; on error/invalid output, retry the same images on Claude."""
    try:
        inv = gemini_client.extract_from_images(cfg, images)
        return inv, "gemini_vision", "gemini_vision"
    except Exception as exc:
        logger.warning("%s: Gemini vision failed (%s: %s); falling back to Claude vision",
                       name, type(exc).__name__, exc)
        inv = claude_client.extract_from_images(cfg, images)
        return inv, "claude_vision", "claude_vision"


def process_file(path: Path, cfg: Config, logger: logging.Logger) -> InvoiceResult:
    started = time.perf_counter()
    result = InvoiceResult(source_file=path.name)

    # Stage 1-2: direct text extraction + classification
    try:
        pages_text = pdf_utils.extract_pages_text(str(path))
    except Exception as exc:
        logger.error("%s: failed to open/parse PDF: %s", path.name, exc)
        result.needs_review = True
        result.error = True
        result.review_reason = f"unreadable PDF: {exc}"
        result.elapsed_seconds = time.perf_counter() - started
        return result

    result.page_count = len(pages_text)
    avg_chars = pdf_utils.avg_alnum_per_page(pages_text)
    classification = pdf_utils.classify_pages(pages_text, cfg.text_quality_threshold)
    logger.info("%s: %d page(s), avg %.0f alphanumeric chars/page -> %s",
                path.name, result.page_count, avg_chars,
                "text-native" if classification == pdf_utils.METHOD_TEXT else "image-only")

    # Stage 3/4: LLM extraction with provider fallback
    try:
        if classification == pdf_utils.METHOD_TEXT:
            full_text = "\n\n--- PAGE BREAK ---\n\n".join(pages_text)
            inv, provider = _extract_text_path(cfg, logger, path.name, full_text)
            method = "text"
        else:
            images = pdf_utils.render_pages_png(str(path), dpi=cfg.render_dpi,
                                                max_pages=cfg.max_vision_pages)
            if result.page_count > cfg.max_vision_pages:
                logger.warning("%s: %d pages, only first %d sent to vision API",
                               path.name, result.page_count, cfg.max_vision_pages)
            inv, method, provider = _extract_vision_path(cfg, logger, path.name, images)
        result.data = inv
        result.extraction_method = method
        result.provider = provider
    except Exception as exc:
        # Both providers failed: emit a null row instead of crashing the batch.
        logger.error("%s: all providers failed (%s: %s); emitting null row for review",
                     path.name, type(exc).__name__, exc)
        result.needs_review = True
        result.error = True
        result.review_reason = f"extraction failed on all providers: {exc}"
        result.elapsed_seconds = time.perf_counter() - started
        return result

    # Stage 6: validation
    reason = validate_invoice(result.data)
    if reason:
        result.needs_review = True
        result.review_reason = reason

    result.elapsed_seconds = time.perf_counter() - started
    logger.info("%s: done in %.1fs (method=%s, provider=%s, needs_review=%s%s)",
                path.name, result.elapsed_seconds, result.extraction_method,
                result.provider, result.needs_review,
                f", reason={result.review_reason}" if result.review_reason else "")
    return result


def find_pdfs(input_dir: Path) -> list[Path]:
    return sorted(p for p in input_dir.iterdir()
                  if p.is_file() and p.suffix.lower() == ".pdf")


def process_directory(input_dir: Path, cfg: Config, logger: logging.Logger) -> list[InvoiceResult]:
    pdfs = find_pdfs(input_dir)
    if not pdfs:
        logger.warning("No PDF files found in %s", input_dir)
        return []
    logger.info("Processing %d PDF(s) from %s", len(pdfs), input_dir)
    return [process_file(path, cfg, logger) for path in pdfs]
