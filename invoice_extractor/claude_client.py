"""Claude (Sonnet) calls: fallback provider for vision and (optionally) text."""

import base64
import logging

import anthropic

from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.config import Config
from invoice_extractor.prompts import (
    JSON_SCHEMA_BLOCK,
    parse_json_response,
    text_extraction_prompt,
    vision_extraction_prompt,
)
from invoice_extractor.retry import call_with_retry
from invoice_extractor.schema import (
    ExtractionError,
    Invoice,
    check_required,
    normalize_invoice,
    unknown_keys,
)

logger = logging.getLogger("invoice_extractor")

# Retryable: rate limits (429), server errors incl. overloaded (5xx/529),
# and network/timeout failures (APITimeoutError subclasses APIConnectionError).
# AuthenticationError (401), NotFoundError (404, e.g. invalid model), and
# other 4xx are NOT in this tuple and raise immediately.
TRANSIENT_ERRORS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)


_client: anthropic.Anthropic | None = None
_client_key: str | None = None


def _get_client(cfg: Config) -> anthropic.Anthropic:
    global _client, _client_key
    if not cfg.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    if _client is None or _client_key != cfg.anthropic_api_key:
        # max_retries=0: tenacity owns the retry policy (single backoff layer).
        _client = anthropic.Anthropic(
            api_key=cfg.anthropic_api_key,
            max_retries=0,
            timeout=float(cfg.request_timeout_seconds),
        )
        _client_key = cfg.anthropic_api_key
    return _client


# Claude's output cap. A single value (not per-route) mirroring the original
# design; truncation is DETECTED via stop_reason rather than papered over by
# blindly raising this - see _request. Kept at 8192 in this milestone.
_MAX_TOKENS = 8192


def _request(cfg: Config, model: str, content) -> str:
    """Single Claude request. Test seam: mock this to avoid network.

    Raises ExtractionError with a clear, sanitized reason when the model
    stopped because it hit the output-token cap (stop_reason == "max_tokens"),
    so a truncated response is never mislabeled downstream as a random JSON
    syntax error. This is non-transient, so it is not retried (an identical
    re-request would truncate the same way).
    """
    client = _get_client(cfg)
    resp = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": content}],
    )
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ExtractionError(
            "Claude response truncated before valid JSON completed "
            "(stop_reason=max_tokens)"
        )
    return "".join(block.text for block in resp.content if block.type == "text")


# Mirrors gemini_client's repair instruction (kept local rather than shared to
# avoid a client-to-client import; both point at the same JSON_SCHEMA_BLOCK).
_REPAIR_INSTRUCTION = (
    "Your previous response was not valid JSON. Return the same extraction "
    "as strict JSON only, matching this exact schema. Do not include "
    "markdown fences, comments, or explanation.\n\n" + JSON_SCHEMA_BLOCK
)


def _repair_text(raw_text: str) -> str:
    return f"Your previous response was:\n\n{raw_text}\n\n{_REPAIR_INSTRUCTION}"


def _ensure_parsed_json(
    cfg: Config, model: str, raw_text: str, label: str, repair_content,
) -> tuple[dict, str]:
    """Parse a Claude response, tolerating markdown fences / surrounding prose
    via parse_json_response's own local cleanup. If that still fails, make
    exactly ONE repair request to the same model (echoing Claude's own invalid
    response back and asking for strict JSON) before giving up - symmetric
    with gemini_client._ensure_parsed_json.

    Returns (parsed_dict, raw_text_that_produced_it). Raises the ORIGINAL
    ExtractionError if the repair does not yield valid JSON either, so the
    pipeline's existing failure/needs_review handling is unchanged in outcome.
    Truncation (stop_reason=max_tokens) never reaches here: _request raises it
    directly, short-circuiting before any repair.
    """
    try:
        return parse_json_response(raw_text), raw_text
    except ExtractionError as exc:
        logger.warning("%s: response from %s was not valid JSON (%s); "
                       "attempting one repair retry", label, model, exc)
        try:
            repaired, attempts = call_with_retry(
                lambda: _request(cfg, model, repair_content),
                is_transient, cfg.max_retries, f"{label}_repair",
            )
            logger.debug("%s_repair: model=%s attempts=%d", label, model, attempts)
            data = parse_json_response(repaired)
        except Exception as repair_exc:
            logger.warning("%s: JSON repair retry did not produce valid JSON; "
                           "surfacing failure for review", label)
            save_debug_artifact(cfg, label, model=model, reason=str(exc), raw_text=raw_text)
            raise exc from repair_exc
        logger.info("%s: JSON repair retry recovered valid JSON", label)
        return data, repaired


def _finalize(cfg: Config, data: dict, raw_text: str, label: str, model: str) -> Invoice:
    extras = unknown_keys(data)
    if extras:
        logger.debug("%s: dropped %d unexpected key(s): %s",
                     label, len(extras), ", ".join(extras[:8]))
    inv = normalize_invoice(data)
    try:
        check_required(inv)
    except ExtractionError as exc:
        save_debug_artifact(cfg, label, model=model, reason=str(exc), raw_text=raw_text)
        raise
    return inv


def extract_from_text(cfg: Config, invoice_text: str, label: str = "claude_text") -> Invoice:
    """Text-only fallback (used only when ENABLE_CLAUDE_TEXT_FALLBACK=true)."""
    raw, attempts = call_with_retry(
        lambda: _request(cfg, cfg.claude_text_model, text_extraction_prompt(invoice_text)),
        is_transient, cfg.max_retries, label,
    )
    logger.debug("%s: model=%s attempts=%d", label, cfg.claude_text_model, attempts)
    data, raw_used = _ensure_parsed_json(
        cfg, cfg.claude_text_model, raw, label, _repair_text(raw),
    )
    return _finalize(cfg, data, raw_used, label, cfg.claude_text_model)


def extract_from_images(cfg: Config, images: list[bytes], label: str = "claude_vision") -> Invoice:
    """Vision fallback when Gemini vision fails or returns unusable output."""
    image_blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode("utf-8"),
            },
        }
        for png in images
    ]
    content: list = [*image_blocks, {"type": "text", "text": vision_extraction_prompt(len(images))}]
    raw, attempts = call_with_retry(
        lambda: _request(cfg, cfg.claude_vision_model, content),
        is_transient, cfg.max_retries, label,
    )
    logger.debug("%s: model=%s attempts=%d", label, cfg.claude_vision_model, attempts)
    # Repair keeps the same list shape (images + repair text) so the vision
    # route stays classified as vision, matching gemini's image-resending repair.
    repair_content: list = [*image_blocks, {"type": "text", "text": _repair_text(raw)}]
    data, raw_used = _ensure_parsed_json(
        cfg, cfg.claude_vision_model, raw, label, repair_content,
    )
    return _finalize(cfg, data, raw_used, label, cfg.claude_vision_model)
