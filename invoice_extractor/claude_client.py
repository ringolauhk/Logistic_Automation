"""Claude (Sonnet) calls: fallback provider for vision and (optionally) text."""

import base64
import logging

import anthropic

from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.config import Config
from invoice_extractor.prompts import (
    parse_json_response,
    text_extraction_prompt,
    vision_extraction_prompt,
)
from invoice_extractor.retry import call_with_retry
from invoice_extractor.schema import Invoice, check_required, normalize_invoice, unknown_keys

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


def _request(cfg: Config, model: str, content) -> str:
    """Single Claude request. Test seam: mock this to avoid network."""
    client = _get_client(cfg)
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": content}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def _finalize(cfg: Config, raw_text: str, label: str, model: str) -> Invoice:
    """No repair retry here (unlike gemini_client) - Claude is already the
    final safety net; a still-unusable Claude response is meant to surface
    as the ordinary needs_review/failure outcome, not be retried further."""
    try:
        data = parse_json_response(raw_text)
    except ExtractionError as exc:
        save_debug_artifact(cfg, label, model=model, reason=str(exc), raw_text=raw_text)
        raise
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
    return _finalize(cfg, raw, label, cfg.claude_text_model)


def extract_from_images(cfg: Config, images: list[bytes], label: str = "claude_vision") -> Invoice:
    """Vision fallback when Gemini vision fails or returns unusable output."""
    content: list = [
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
    content.append({"type": "text", "text": vision_extraction_prompt(len(images))})
    raw, attempts = call_with_retry(
        lambda: _request(cfg, cfg.claude_vision_model, content),
        is_transient, cfg.max_retries, label,
    )
    logger.debug("%s: model=%s attempts=%d", label, cfg.claude_vision_model, attempts)
    return _finalize(cfg, raw, label, cfg.claude_vision_model)
