"""Gemini calls via the google-genai SDK (migrated from google-generativeai).

Primary provider for both routes. Uses JSON mode (response_mime_type) as the
native structured-output control, with robust JSON extraction as a fallback.
"""

import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.config import Config
from invoice_extractor.prompts import (
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

_client: genai.Client | None = None
_client_key: str | None = None


def _get_client(cfg: Config) -> genai.Client:
    global _client, _client_key
    if not cfg.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    if _client is None or _client_key != cfg.gemini_api_key:
        _client = genai.Client(api_key=cfg.gemini_api_key)
        _client_key = cfg.gemini_api_key
    return _client


def is_transient(exc: BaseException) -> bool:
    """Retry on 429 and 5xx and network/timeout errors only.

    Auth (401/403), invalid model (404), and bad requests (400) are
    ClientErrors with codes outside this set - they raise immediately.
    """
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        return code == 429 or (isinstance(code, int) and code >= 500)
    return isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError))


def _generate(cfg: Config, model: str, contents: list) -> str:
    """Single Gemini request. Test seam: mock this to avoid network."""
    client = _get_client(cfg)
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            http_options=genai_types.HttpOptions(
                timeout=cfg.request_timeout_seconds * 1000  # milliseconds
            ),
        ),
    )
    if resp.text is None:
        raise ExtractionError("Gemini returned no text content")
    return resp.text


def _finalize(cfg: Config, raw_text: str, label: str) -> Invoice:
    save_debug_artifact(cfg, label, raw_text)
    data = parse_json_response(raw_text)
    extras = unknown_keys(data)
    if extras:
        logger.debug("%s: dropped %d unexpected key(s): %s",
                     label, len(extras), ", ".join(extras[:8]))
    inv = normalize_invoice(data)
    check_required(inv)
    return inv


def extract_from_text(cfg: Config, invoice_text: str, label: str = "gemini_text") -> Invoice:
    """Normalize already-extracted invoice text via a TEXT-ONLY Gemini call."""
    raw, attempts = call_with_retry(
        lambda: _generate(cfg, cfg.gemini_text_model, [text_extraction_prompt(invoice_text)]),
        is_transient, cfg.max_retries, label,
    )
    logger.debug("%s: model=%s attempts=%d", label, cfg.gemini_text_model, attempts)
    return _finalize(cfg, raw, label)


def extract_from_images(cfg: Config, images: list[bytes], label: str = "gemini_vision") -> Invoice:
    """Extract from rendered page PNGs via Gemini vision."""
    contents: list = [vision_extraction_prompt(len(images))]
    contents.extend(
        genai_types.Part.from_bytes(data=png, mime_type="image/png") for png in images
    )
    raw, attempts = call_with_retry(
        lambda: _generate(cfg, cfg.gemini_vision_model, contents),
        is_transient, cfg.max_retries, label,
    )
    logger.debug("%s: model=%s attempts=%d", label, cfg.gemini_vision_model, attempts)
    return _finalize(cfg, raw, label)
