"""Per-file orchestration: page-level classify -> per-route extract -> merge -> validate.

Vision pages are processed in ordered chunks of MAX_VISION_PAGES pages per
request - every meaningful page is processed, none are silently dropped.
Each chunk gets the full Gemini-first / Claude-fallback treatment; a chunk
failing both providers is recorded (failed_pages + review reason with the
page range) while later chunks still run.

Assumes one invoice per PDF (documented PoC limitation). Likely multi-invoice
PDFs are detected via conflicting invoice numbers and flagged for review
rather than merged silently.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from invoice_extractor import claude_client, gemini_client, openrouter_client, pdf_utils
from invoice_extractor.aggregation import RouteResult, aggregate
from invoice_extractor.config import (
    Config,
    ConfigurationError,
    describe_models,
    validate_openrouter_config,
)
from invoice_extractor.logging_setup import exc_summary
from invoice_extractor.pdf_utils import (
    DOC_ERROR,
    PAGE_BLANK,
    PAGE_IMAGE,
    PAGE_TEXT,
    PageInfo,
    format_page_ranges,
)
from invoice_extractor.schema import ExtractionError, Invoice, empty_invoice, validate_invoice


@dataclass
class InvoiceResult:
    source_file: str
    invoice: Invoice = field(default_factory=empty_invoice)
    page_count: int = 0
    document_classification: str = DOC_ERROR  # text-native | image-only | mixed | error
    extraction_method: str = "failed"  # text | vision | mixed | failed
    provider: str = "none"  # gemini | claude | openrouter | mixed | none
    model: str | None = None  # actual model id(s) that produced the result
    text_pages: list[int] = field(default_factory=list)
    image_pages: list[int] = field(default_factory=list)
    blank_pages: list[int] = field(default_factory=list)
    failed_pages: list[int] = field(default_factory=list)  # pages whose route/chunk failed
    vision_chunk_count: int = 0  # vision requests attempted (successful + failed)
    needs_review: bool = False
    review_reason: str | None = None
    error: bool = False  # hard failure: no structured result at all
    elapsed_seconds: float = 0.0


def _chunked(items: list, size: int) -> list[list]:
    # Defense in depth: Config.__post_init__ already rejects a non-positive
    # max_vision_pages before any PDF/provider work starts, but this function
    # is small and directly callable/importable on its own, so it must not
    # silently misbehave if ever invoked with a bad size some other way.
    # size=0 previously raised a confusing bare `ValueError: range() arg 3
    # must not be zero`; size<0 previously returned [] silently (range with
    # a negative step is empty when items is non-empty) - both now raise the
    # same clear, explicit error. Positive-size behavior is unchanged.
    if size <= 0:
        raise ValueError(f"chunk size must be a positive integer, got {size!r}")
    return [items[i : i + size] for i in range(0, len(items), size)]


def _run_text_route(cfg: Config, logger: logging.Logger, name: str,
                    text_pages: list[PageInfo]) -> RouteResult:
    """Gemini text normalization; Claude TEXT fallback only when enabled.

    With ENABLE_CLAUDE_TEXT_FALLBACK=false (the default, matching the original
    cost/routing design) a Gemini failure propagates - Claude is never called.
    """
    pages = [p.number for p in text_pages]
    combined = "\n\n".join(f"--- PAGE {p.number} ---\n{p.text}" for p in text_pages)
    started = time.perf_counter()
    if cfg.llm_gateway == "openrouter":
        return _run_openrouter_text_route(cfg, logger, name, pages, combined, started)
    try:
        inv = gemini_client.extract_from_text(cfg, combined, label=f"{name}_gemini_text")
        provider, model = "gemini", cfg.gemini_text_model
    except Exception as gemini_exc:
        if not cfg.enable_claude_text_fallback:
            logger.warning("%s: Gemini text failed (%s); Claude text fallback is disabled",
                           name, exc_summary(gemini_exc))
            raise
        logger.warning("%s: Gemini text failed (%s); trying Claude text fallback",
                       name, exc_summary(gemini_exc))
        try:
            inv = claude_client.extract_from_text(cfg, combined, label=f"{name}_claude_text")
        except Exception as claude_exc:
            # Both providers' sanitized reasons are preserved here - without
            # this, Claude's exception alone would propagate and Gemini's
            # original cause (e.g. a 429) would be silently lost.
            logger.warning("%s: Claude text fallback also failed (%s)",
                           name, exc_summary(claude_exc))
            raise ExtractionError(
                f"Gemini: {exc_summary(gemini_exc)}; Claude: {exc_summary(claude_exc)}"
            ) from claude_exc
        provider, model = "claude", cfg.claude_text_model
    logger.info("%s: text route (pages %s) ok provider=%s model=%s %.1fs",
                name, format_page_ranges(pages), provider, model,
                time.perf_counter() - started)
    return RouteResult("text", pages, inv, provider, model)


def _run_openrouter_text_route(
    cfg: Config, logger: logging.Logger, name: str,
    pages: list[int], combined: str, started: float,
) -> RouteResult:
    """Text extraction via the single configured OpenRouter text model
    (LLM_GATEWAY=openrouter, M2). No ladder/escalation - one model, at most
    one JSON-repair retry (handled inside openrouter_client.extract_from_text).

    validate_openrouter_config runs here (immediately before the live call),
    not at config-load time, so import/--help/classify/render/offline doctor
    stay key-free even when LLM_GATEWAY=openrouter is configured. A missing
    key or empty model list surfaces as the same clean per-route failure/
    needs_review outcome as any other provider failure - never a crash.
    """
    validate_openrouter_config(cfg, require_vision=False)
    label = f"{name}_openrouter_text"
    inv, provider_result = openrouter_client.extract_from_text(cfg, combined, label=label)
    model = provider_result.actual_model or provider_result.requested_model
    logger.info(
        "%s: text route (pages %s) ok provider=openrouter requested=%s actual=%s "
        "mode=%s finish=%s gen=%s %.1fs",
        name, format_page_ranges(pages), provider_result.requested_model, model,
        provider_result.structured_mode, provider_result.finish_reason,
        provider_result.generation_id, time.perf_counter() - started,
    )
    return RouteResult("text", pages, inv, "openrouter", model, provider_result)


def _run_vision_chunk(cfg: Config, logger: logging.Logger, name: str, path: Path,
                      chunk: list[PageInfo], index: int, total: int) -> RouteResult:
    """One vision request for one chunk: Gemini first, Claude on any failure.

    Per-chunk attempt counts are logged by the clients (same label prefix);
    normal logs never carry invoice text or response bodies.
    """
    pages = [p.number for p in chunk]
    page_range = format_page_ranges(pages)
    started = time.perf_counter()
    if cfg.llm_gateway == "openrouter":
        # OpenRouter vision is not implemented yet (a later milestone) -
        # fail clearly before rendering/spending anything, rather than
        # silently falling back to direct and mixing billing gateways.
        raise ConfigurationError(
            "OpenRouter vision route is not implemented yet (text-only so far); "
            "set LLM_GATEWAY=direct to process image pages"
        )
    images = pdf_utils.render_pages_png(str(path), pages, dpi=cfg.render_dpi)
    try:
        inv = gemini_client.extract_from_images(
            cfg, images, label=f"{name}_gemini_vision_c{index}")
        provider, model = "gemini", cfg.gemini_vision_model
    except Exception as gemini_exc:
        logger.warning("%s: vision chunk %d/%d (pages %s): Gemini failed (%s); "
                       "falling back to Claude vision",
                       name, index, total, page_range, exc_summary(gemini_exc))
        try:
            inv = claude_client.extract_from_images(
                cfg, images, label=f"{name}_claude_vision_c{index}")
        except Exception as claude_exc:
            # Both providers' sanitized reasons are preserved here - without
            # this, Claude's exception alone would propagate and Gemini's
            # original cause (e.g. a 429) would be silently lost.
            logger.warning("%s: vision chunk %d/%d (pages %s): Claude vision "
                           "fallback also failed (%s)",
                           name, index, total, page_range, exc_summary(claude_exc))
            raise ExtractionError(
                f"Gemini: {exc_summary(gemini_exc)}; Claude: {exc_summary(claude_exc)}"
            ) from claude_exc
        provider, model = "claude", cfg.claude_vision_model
    logger.info("%s: vision chunk %d/%d (pages %s) ok provider=%s model=%s %.1fs",
                name, index, total, page_range, provider, model,
                time.perf_counter() - started)
    return RouteResult("vision", pages, inv, provider, model)


def _reason_categories(result: InvoiceResult) -> str:
    """Loggable summary of review reasons WITHOUT invoice values."""
    if not result.review_reason:
        return "none"
    categories = []
    for part in result.review_reason.split("; "):
        categories.append(part.split(":", 1)[0].strip())
    return ", ".join(dict.fromkeys(categories))


def process_file(path: Path, cfg: Config, logger: logging.Logger) -> InvoiceResult:
    started = time.perf_counter()
    result = InvoiceResult(source_file=path.name)

    # Stage 1-2: per-page text extraction + classification
    try:
        pages = pdf_utils.analyze_pages(str(path), cfg.text_quality_threshold)
    except Exception as exc:
        logger.error("%s: unreadable PDF (%s)", path.name, exc_summary(exc))
        result.needs_review = True
        result.error = True
        result.review_reason = f"unreadable PDF: {exc_summary(exc)}"
        result.elapsed_seconds = time.perf_counter() - started
        return result

    result.page_count = len(pages)
    text_pages = [p for p in pages if p.kind == PAGE_TEXT]
    image_pages = [p for p in pages if p.kind == PAGE_IMAGE]
    blank_pages = [p for p in pages if p.kind == PAGE_BLANK]
    result.text_pages = [p.number for p in text_pages]
    result.image_pages = [p.number for p in image_pages]
    result.blank_pages = [p.number for p in blank_pages]
    result.document_classification = pdf_utils.classify_document(pages)

    for p in pages:
        logger.debug("%s: page %d -> %s (%d alnum chars)",
                     path.name, p.number, p.kind, p.alnum_chars)
    logger.info("%s: %d page(s) -> %s (text=%s image=%s blank=%s)",
                path.name, result.page_count, result.document_classification,
                format_page_ranges(result.text_pages) or "-",
                format_page_ranges(result.image_pages) or "-",
                format_page_ranges(result.blank_pages) or "-")

    # Stage 3-4: per-route extraction with provider fallback
    routes: list[RouteResult] = []
    route_failures: list[tuple[str, Exception]] = []

    if text_pages:
        try:
            routes.append(_run_text_route(cfg, logger, path.name, text_pages))
        except Exception as exc:
            label = f"text route (pages {format_page_ranges(result.text_pages)})"
            route_failures.append((label, exc))
            result.failed_pages.extend(result.text_pages)

    if image_pages:
        chunks = _chunked(image_pages, cfg.max_vision_pages)
        result.vision_chunk_count = len(chunks)
        if len(chunks) > 1:
            logger.info("%s: %d image page(s) split into %d vision chunk(s) of <= %d",
                        path.name, len(image_pages), len(chunks), cfg.max_vision_pages)
        for index, chunk in enumerate(chunks, start=1):
            try:
                routes.append(_run_vision_chunk(cfg, logger, path.name, path,
                                                chunk, index, len(chunks)))
            except Exception as exc:
                nums = [p.number for p in chunk]
                page_range = format_page_ranges(nums)
                label = f"vision route chunk {index}/{len(chunks)} (pages {page_range})"
                route_failures.append((label, exc))
                result.failed_pages.extend(nums)
                logger.warning("%s: vision chunk %d/%d (pages %s) FAILED on all "
                               "providers (%s); continuing with remaining chunks",
                               path.name, index, len(chunks), page_range,
                               exc_summary(exc))

    if not routes:
        # No structured result at all: emit a reviewable null row, never crash.
        result.needs_review = True
        result.error = True
        if result.document_classification == DOC_ERROR:
            result.review_reason = "no meaningful pages (document is blank)"
        else:
            result.review_reason = "; ".join(
                f"{label} failed on all providers: {exc_summary(exc)}"
                for label, exc in route_failures
            )
        result.elapsed_seconds = time.perf_counter() - started
        logger.error("%s: extraction failed (%s); emitting null row for review",
                     path.name, _reason_categories(result))
        return result

    # Stage 5: deterministic aggregation (chunks merge like any other routes)
    outcome = aggregate(routes)
    result.invoice = outcome.invoice

    routes_used = {r.route for r in routes}
    result.extraction_method = "mixed" if len(routes_used) > 1 else routes[0].route
    ordered_routes = sorted(routes, key=lambda r: min(r.pages))
    providers = list(dict.fromkeys(r.provider for r in ordered_routes))
    result.provider = providers[0] if len(providers) == 1 else "mixed"
    result.model = "+".join(dict.fromkeys(r.model for r in ordered_routes))

    # Stage 6: review flags - partial route/chunk failure, conflicts, validation
    reasons: list[str] = []
    covered = format_page_ranges(sorted(n for r in routes for n in r.pages))
    for label, exc in route_failures:
        reasons.append(
            f"partial extraction: {label} failed ({exc_summary(exc)}); "
            f"result covers pages {covered} only"
        )
    for fld, detail in outcome.conflicts:
        reasons.append(f"conflict in {fld}: {detail}")
    reasons.extend(outcome.notes)
    validation_reason = validate_invoice(
        result.invoice, cfg.total_abs_tolerance, cfg.total_rel_tolerance
    )
    if validation_reason:
        reasons.append(validation_reason)
    if reasons:
        result.needs_review = True
        result.review_reason = "; ".join(reasons)

    result.elapsed_seconds = time.perf_counter() - started
    logger.info(
        "%s: done in %.1fs class=%s method=%s provider=%s model=%s chunks=%d "
        "needs_review=%s (%s)",
        path.name, result.elapsed_seconds, result.document_classification,
        result.extraction_method, result.provider, result.model,
        result.vision_chunk_count, result.needs_review, _reason_categories(result),
    )
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
    logger.info(describe_models(cfg))
    results = []
    for path in pdfs:
        try:
            results.append(process_file(path, cfg, logger))
        except Exception as exc:  # belt and braces: one file must never stop the batch
            logger.error("%s: unexpected pipeline error (%s)", path.name, exc_summary(exc))
            failed = InvoiceResult(source_file=path.name, needs_review=True, error=True,
                                   review_reason=f"unexpected pipeline error: {exc_summary(exc)}")
            results.append(failed)
    return results
