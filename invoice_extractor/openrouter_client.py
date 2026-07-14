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

import httpx

from invoice_extractor.config import Config
from invoice_extractor.provider import (
    MODE_JSON_OBJECT,
    MODE_JSON_SCHEMA,
    ATTEMPT_PRIMARY,
    MODE_PROMPT_ONLY,
    ProviderError,
    ProviderResult,
    coerce_cost,
)
from invoice_extractor.schema import Invoice

logger = logging.getLogger("invoice_extractor")

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
