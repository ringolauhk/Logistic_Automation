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


_REPAIR_INSTRUCTION = (
    "Your previous response was not valid JSON. Return the same extraction "
    "as strict JSON only, matching this exact schema. Do not include "
    "markdown fences, comments, or explanation.\n\n" + JSON_SCHEMA_BLOCK
)


def _ensure_parsed_json(
    cfg: Config, model: str, raw_text: str, label: str,
    image_parts: list | None = None, notifier=None,
) -> tuple[dict, str]:
    """Parse a Gemini response, tolerating markdown fences / surrounding
    prose via parse_json_response's own local cleanup. If that still fails,
    try exactly ONE repair retry against the same model before giving up:
    the repair prompt echoes the model's own (invalid) response - and, for
    the vision route, the same images again - so it has full context to
    correct itself, not just a bare instruction.

    Returns (parsed_dict, raw_text_that_produced_it) - the latter is needed
    by _finalize so a later check_required failure saves the text that
    actually parsed (original or repaired), not a mismatched one.

    Raises the ORIGINAL ExtractionError (not a repair-specific one) if the
    repair attempt doesn't produce valid JSON either, so callers' existing
    failure/fallback handling is unaffected by this step's mere presence -
    it can only turn a would-be failure into a success, never change what a
    still-uncovered failure looks like upstream.
    """
    try:
        return parse_json_response(raw_text), raw_text
    except ExtractionError as exc:
        logger.warning("%s: response from %s was not valid JSON (%s); "
                       "attempting one repair retry", label, model, exc)
        repair_text = f"Your previous response was:\n\n{raw_text}\n\n{_REPAIR_INSTRUCTION}"
        repair_contents: list = [repair_text, *(image_parts or [])]
        if notifier is not None:
            notifier.started("repair")
        try:
            repaired, attempts = call_with_retry(
                lambda: _generate(cfg, model, repair_contents),
                is_transient, cfg.max_retries, f"{label}_repair",
            )
            logger.debug("%s_repair: model=%s attempts=%d", label, model, attempts)
            data = parse_json_response(repaired)
        except Exception as repair_exc:
            if notifier is not None:
                notifier.completed("repair", accepted=False)
            logger.warning("%s: JSON repair retry did not produce valid JSON; "
                           "continuing with existing failure/fallback behavior", label)
            save_debug_artifact(cfg, label, model=model, reason=str(exc), raw_text=raw_text)
            raise exc from repair_exc
        if notifier is not None:
            notifier.completed("repair", accepted=True)
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


def extract_from_text(cfg: Config, invoice_text: str, label: str = "gemini_text",
                      notifier=None) -> Invoice:
    """Normalize already-extracted invoice text via a TEXT-ONLY Gemini call.

    `notifier` (optional, M9): an events.RequestNotifier that reports
    provider-request start/completion for UI progress. None (the default,
    used by the CLI) changes nothing.
    """
    if notifier is not None:
        notifier.started("primary")
    try:
        raw, attempts = call_with_retry(
            lambda: _generate(cfg, cfg.gemini_text_model, [text_extraction_prompt(invoice_text)]),
            is_transient, cfg.max_retries, label,
        )
    except Exception:
        if notifier is not None:
            notifier.completed("primary", accepted=False)
        raise
    if notifier is not None:
        notifier.completed("primary", accepted=True)
    logger.debug("%s: model=%s attempts=%d", label, cfg.gemini_text_model, attempts)
    data, raw_used = _ensure_parsed_json(cfg, cfg.gemini_text_model, raw, label,
                                         notifier=notifier)
    return _finalize(cfg, data, raw_used, label, cfg.gemini_text_model)


def extract_from_images(cfg: Config, images: list[bytes], label: str = "gemini_vision",
                        notifier=None) -> Invoice:
    """Extract from rendered page PNGs via Gemini vision. `notifier` as in
    extract_from_text (optional M9 progress hook; None changes nothing)."""
    image_parts = [
        genai_types.Part.from_bytes(data=png, mime_type="image/png") for png in images
    ]
    contents: list = [vision_extraction_prompt(len(images)), *image_parts]
    if notifier is not None:
        notifier.started("primary")
    try:
        raw, attempts = call_with_retry(
            lambda: _generate(cfg, cfg.gemini_vision_model, contents),
            is_transient, cfg.max_retries, label,
        )
    except Exception:
        if notifier is not None:
            notifier.completed("primary", accepted=False)
        raise
    if notifier is not None:
        notifier.completed("primary", accepted=True)
    logger.debug("%s: model=%s attempts=%d", label, cfg.gemini_vision_model, attempts)
    data, raw_used = _ensure_parsed_json(
        cfg, cfg.gemini_vision_model, raw, label, image_parts=image_parts,
        notifier=notifier,
    )
    return _finalize(cfg, data, raw_used, label, cfg.gemini_vision_model)
