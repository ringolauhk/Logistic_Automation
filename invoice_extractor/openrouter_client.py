"""OpenRouter gateway boundary (M1: boundary + normalization only).

Direct httpx against the OpenRouter chat-completions API. This milestone adds
ONLY the request/response boundary and safe normalization - it does NOT run
extraction, JSON repair, schema validation, the model ladder, or any pipeline
wiring, and makes no live calls in the test suite (the network-blocking
autouse fixture guards; tests mock `_chat_completion`).

Privacy: the API key lives only in the HTTP client's headers, never on a
returned object and never in a log line or exception. Provider error bodies
are untrusted and are never surfaced - only the HTTP status code and a fixed
category are retained (see ProviderError).
"""

import base64
import logging
import time

import httpx

from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.config import Config
from invoice_extractor.prompts import (
    JSON_SCHEMA_BLOCK,
    parse_json_response,
    text_extraction_prompt,
)
from invoice_extractor.provider import (
    ATTEMPT_PRIMARY,
    ATTEMPT_REPAIR,
    MODE_JSON_OBJECT,
    MODE_JSON_SCHEMA,
    MODE_PROMPT_ONLY,
    ROUTE_TEXT,
    ProviderError,
    ProviderResult,
    coerce_cost,
    is_truncated,
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

# Output-token ceiling for OpenRouter requests. Fixed for M2 (mirrors the
# Claude client); per-model sizing from capability metadata is a later
# milestone. A response that hits this cap surfaces as finish_reason=length
# and is classified as a truncation, never a random JSON syntax error.
_MAX_TOKENS = 8192

# Transport-layer categories worth a bounded retry (mirrors the transient
# policy of the direct clients). Malformed envelopes and 4xx client errors are
# NOT transient - they raise immediately.
_TRANSIENT_CATEGORIES = ("rate_limited", "server_error", "timeout", "transport")

# Repair instruction (kept local to avoid a client-to-client import; mirrors
# gemini_client/claude_client). Points at the same shared JSON_SCHEMA_BLOCK.
_REPAIR_INSTRUCTION = (
    "Your previous response was not valid JSON. Return the same extraction "
    "as strict JSON only, matching this exact schema. Do not include "
    "markdown fences, comments, or explanation.\n\n" + JSON_SCHEMA_BLOCK
)

_client: httpx.Client | None = None
_client_key: str | None = None
_client_base: str | None = None


def _get_client(cfg: Config) -> httpx.Client:
    """Cached httpx client. Raises RuntimeError when the key is absent - the
    same missing-key guard idiom as gemini_client/claude_client (a trusted,
    key-free message). The key is placed in the Authorization header only."""
    global _client, _client_key, _client_base
    if not cfg.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    if (
        _client is None
        or _client_key != cfg.openrouter_api_key
        or _client_base != cfg.openrouter_base_url
    ):
        headers = {"Authorization": f"Bearer {cfg.openrouter_api_key}"}
        # Optional attribution headers (never credentials).
        if cfg.openrouter_app_name:
            headers["X-Title"] = cfg.openrouter_app_name
        if cfg.openrouter_site_url:
            headers["HTTP-Referer"] = cfg.openrouter_site_url
        _client = httpx.Client(base_url=cfg.openrouter_base_url, headers=headers)
        _client_key = cfg.openrouter_api_key
        _client_base = cfg.openrouter_base_url
    return _client


def _categorize(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "timeout"
    if status >= 500:
        return "server_error"
    return "client_error"


def _chat_completion(
    cfg: Config,
    *,
    model: str,
    messages: list,
    response_format=None,
    max_tokens: int,
    timeout: float | None = None,
) -> dict:
    """Single OpenRouter chat completion. Test seam: mock this to avoid the
    network. Returns the raw JSON dict on success.

    Raises a sanitized ProviderError on HTTP (>=400) or transport failure -
    the message carries only the HTTP status code / a fixed category, never
    the response body, headers, or key.
    """
    client = _get_client(cfg)
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format is not None:
        body["response_format"] = response_format
    try:
        resp = client.post(
            "/chat/completions",
            json=body,
            timeout=timeout if timeout is not None else float(cfg.request_timeout_seconds),
        )
    except httpx.RequestError as exc:
        # Do not surface exc's message (may contain URL/host detail); a fixed
        # category is enough for the caller.
        raise ProviderError("OpenRouter transport error", category="transport") from exc
    if resp.status_code >= 400:
        raise ProviderError(
            f"OpenRouter request failed (HTTP {resp.status_code})",
            category=_categorize(resp.status_code),
            http_status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(
            "OpenRouter returned a non-JSON response", category="malformed_envelope"
        ) from exc


def parse_completion(
    raw: dict,
    *,
    requested_model: str,
    route: str,
    attempt_index: int = 0,
    attempt_type: str = ATTEMPT_PRIMARY,
    structured_mode: str = MODE_PROMPT_ONLY,
    latency_ms: float | None = None,
) -> ProviderResult:
    """Normalize an OpenRouter chat-completion response into a ProviderResult.

    Extracts text content, the ACTUAL served model, generation id, usage token
    counts (incl. reasoning tokens), inline cost, and both the normalized and
    native finish reasons. Raises a sanitized ProviderError (category
    "malformed_envelope") when the envelope structure is unusable; the message
    never contains response content.
    """
    if not isinstance(raw, dict):
        raise ProviderError(
            "OpenRouter response was not a JSON object", category="malformed_envelope"
        )
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(
            "OpenRouter response has no choices", category="malformed_envelope"
        )
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError(
            "OpenRouter choice was malformed", category="malformed_envelope"
        )
    message = first.get("message") or {}
    content = message.get("content")
    if content is None:
        content = ""  # empty response is a normal (rejectable) outcome, not malformed
    if not isinstance(content, str):
        raise ProviderError(
            "OpenRouter message content was not text", category="malformed_envelope"
        )

    usage = raw.get("usage") or {}
    completion_details = usage.get("completion_tokens_details") or {}

    return ProviderResult(
        requested_model=requested_model,
        route=route,
        actual_model=raw.get("model"),
        attempt_type=attempt_type,
        attempt_index=attempt_index,
        structured_mode=structured_mode,
        text=content,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        reasoning_tokens=completion_details.get("reasoning_tokens"),
        total_tokens=usage.get("total_tokens"),
        cost_usd=coerce_cost(usage.get("cost")),
        finish_reason=first.get("finish_reason"),
        native_finish_reason=first.get("native_finish_reason"),
        generation_id=raw.get("id"),
        latency_ms=latency_ms,
    )


# --- Payload builders (pure; not wired to the pipeline in M1) ----------------

def build_text_messages(prompt: str) -> list:
    """OpenRouter messages for a text-route request."""
    return [{"role": "user", "content": prompt}]


def build_vision_messages(prompt: str, images: list[bytes]) -> list:
    """OpenRouter messages for a vision-route request: base64 image_url blocks
    (multiple images per request are supported) followed by the text prompt."""
    content: list = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.standard_b64encode(png).decode("utf-8")
            },
        }
        for png in images
    ]
    content.append({"type": "text", "text": prompt})
    return [{"role": "user", "content": content}]


def build_response_format(mode: str, schema: dict | None = None):
    """Map a structured-output mode to an OpenRouter response_format value.

    json_schema -> strict JSON-schema object; json_object -> {"type":
    "json_object"}; prompt_only -> None (parser/repair does the work). The
    json_schema strict-compatibility of the Invoice schema is refined when it
    is first used live (a later milestone); M1 only fixes the wrapper shape.
    """
    if mode == MODE_JSON_SCHEMA:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "invoice",
                "strict": True,
                "schema": schema if schema is not None else Invoice.model_json_schema(),
            },
        }
    if mode == MODE_JSON_OBJECT:
        return {"type": "json_object"}
    return None  # prompt_only


# --- Text extraction through one configured OpenRouter model (M2) ------------

def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, ProviderError) and exc.category in _TRANSIENT_CATEGORIES


def _run_attempt(
    cfg: Config, model: str, messages: list, response_format, structured_mode: str,
    attempt_type: str, attempt_index: int, label: str,
) -> ProviderResult:
    """One OpenRouter request (with bounded transport retry), normalized and
    checked for truncation/empty content BEFORE any JSON parsing.

    Truncation (finish_reason=length) and empty content raise a clear,
    sanitized ExtractionError - neither is retried and neither triggers the
    JSON-repair path (repair is only for malformed-but-complete JSON).
    """
    started = time.perf_counter()
    raw, _attempts = call_with_retry(
        lambda: _chat_completion(
            cfg, model=model, messages=messages,
            response_format=response_format, max_tokens=_MAX_TOKENS,
        ),
        _is_transient, cfg.max_retries, f"{label}_{attempt_type}",
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    result = parse_completion(
        raw, requested_model=model, route=ROUTE_TEXT,
        attempt_index=attempt_index, attempt_type=attempt_type,
        structured_mode=structured_mode, latency_ms=latency_ms,
    )
    if is_truncated(result):
        raise ExtractionError(
            "OpenRouter response truncated before valid JSON completed "
            "(finish_reason=length)"
        )
    if not result.text.strip():
        raise ExtractionError("empty response from model")
    return result


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


def _repair_messages(prompt: str, raw_text: str) -> list:
    return build_text_messages(
        f"{prompt}\n\nYour previous response was:\n\n{raw_text}\n\n{_REPAIR_INSTRUCTION}"
    )


def extract_from_text(
    cfg: Config, invoice_text: str, label: str = "openrouter_text",
) -> tuple[Invoice, ProviderResult]:
    """Extract an invoice from text via the single configured OpenRouter text
    model (index 0; the model ladder is a later milestone).

    Returns (Invoice, ProviderResult) - the result carries safe provenance/
    usage metadata for the caller. Applies local JSON cleanup, then exactly
    ONE repair request to the same model if still malformed, then schema +
    hard-required validation. Raw model output is never logged.
    """
    model = cfg.openrouter_text_models[0]
    mode = cfg.openrouter_structured_output
    prompt = text_extraction_prompt(invoice_text)
    messages = build_text_messages(prompt)
    response_format = build_response_format(mode)

    result = _run_attempt(
        cfg, model, messages, response_format, mode, ATTEMPT_PRIMARY, 0, label,
    )
    try:
        data = parse_json_response(result.text)
    except ExtractionError as exc:
        logger.warning("%s: OpenRouter response from %s was not valid JSON (%s); "
                       "attempting one repair retry", label, model, exc)
        repair = _run_attempt(
            cfg, model, _repair_messages(prompt, result.text), response_format, mode,
            ATTEMPT_REPAIR, 1, label,
        )
        try:
            data = parse_json_response(repair.text)
        except ExtractionError as repair_exc:
            logger.warning("%s: JSON repair retry did not produce valid JSON; "
                           "surfacing failure for review", label)
            save_debug_artifact(cfg, label, model=model, reason=str(exc), raw_text=result.text)
            raise exc from repair_exc
        logger.info("%s: JSON repair retry recovered valid JSON", label)
        result = repair  # provenance reflects the successful (repair) attempt

    inv = _finalize(cfg, data, result.text, label, model)
    return inv, result
