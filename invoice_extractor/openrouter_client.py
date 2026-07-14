"""OpenRouter gateway boundary and text-route model ladder.

Direct httpx against the OpenRouter chat-completions API. `extract_from_text`
(M2) is a single-model extraction, kept as-is for its existing tests;
`extract_from_text_ladder` (M3) tries an ordered list of models, escalating
on any unusable result and stopping at the first accepted extraction. Makes
no live calls in the test suite (the network-blocking autouse fixture
guards; tests mock `_chat_completion`).

Privacy: the API key lives only in the HTTP client's headers, never on a
returned object and never in a log line or exception. Provider error bodies
are untrusted and are never surfaced - only the HTTP status code and a fixed
category are retained (see ProviderError).
"""

import base64
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.config import Config
from invoice_extractor.prompts import (
    JSON_SCHEMA_BLOCK,
    parse_json_response,
    text_extraction_prompt,
)
from invoice_extractor.provider import (
    ATTEMPT_ESCALATION,
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
from invoice_extractor.logging_setup import exc_summary
from invoice_extractor.usage import (
    LadderExhaustedError,
    RunBudget,
    usage_record_for_failed_attempt,
    usage_record_from_result,
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


# Embedded-error-envelope numeric codes -> safe category (a 200-status HTTP
# response whose JSON body carries {"error": {...}} instead of "choices" -
# this is the shape a live pilot hit for an unsupported response_format).
# Only the numeric code/status is ever inspected - message/metadata are
# untrusted and never surfaced (see ProviderError's docstring).
_ERROR_CODE_CATEGORIES = {
    402: "payment_required",
    404: "model_unavailable",
    408: "timeout",
    429: "rate_limited",
}


def _categorize_error_envelope(error_obj: dict) -> tuple[str, int | None]:
    code = error_obj.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return "malformed_envelope", None
    if code in _ERROR_CODE_CATEGORIES:
        return _ERROR_CODE_CATEGORIES[code], code
    if code >= 500:
        return "server_error", code
    if code >= 400:
        return "client_error", code
    return "malformed_envelope", code


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
        error_obj = raw.get("error")
        if isinstance(error_obj, dict):
            category, http_status = _categorize_error_envelope(error_obj)
            raise ProviderError(
                "OpenRouter returned an embedded error response"
                + (f" (code {http_status})" if http_status is not None else ""),
                category=category, http_status=http_status,
            )
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


# --- Application-controlled model ladder (M3) --------------------------------

@dataclass
class _AttemptOutcome:
    """Internal: the result of trying ONE model (primary [+ one repair] call).

    usage_records covers every HTTP attempt made for this model (primary
    and, if it happened, repair) regardless of outcome - collected here so
    the ladder driver can accumulate them even when this model is ultimately
    rejected and escalation continues to the next one.
    """

    invoice: Invoice | None
    provider_result: ProviderResult | None
    usage_records: list
    failure_summary: str | None  # None only when invoice is not None
    budget_exhausted: bool = False  # True => ladder must stop, not escalate


def _attempt_model(
    cfg: Config, invoice_text: str, model: str, *, ladder_index: int,
    run_id: str, source_file: str, page_range: str, label: str,
    run_budget: RunBudget | None = None,
) -> _AttemptOutcome:
    """Try exactly one model: primary request, local cleanup, at most one
    repair, then schema + hard-required validation. Never raises - failures
    are reported via the returned _AttemptOutcome so the ladder can escalate.

    run_budget, when given, is the shared run-wide cost tracker: its live
    `spent` total is updated immediately after every usage record is built
    (never batched), and is checked immediately before the primary call and
    again before the repair call - the two HTTP call sites inside this
    function - so a run-wide crossing stops further calls at the earliest
    possible point, even mid-file.
    """
    usage_records = []
    mode = cfg.openrouter_structured_output
    response_format = build_response_format(mode)
    prompt = text_extraction_prompt(invoice_text)
    messages = build_text_messages(prompt)
    attempt_type = ATTEMPT_PRIMARY if ladder_index == 0 else ATTEMPT_ESCALATION

    def failed_record(attempt_type_, category, http_status=None):
        usage_records.append(usage_record_for_failed_attempt(
            run_id=run_id, source_file=source_file, route=ROUTE_TEXT,
            page_range=page_range, attempt_type=attempt_type_,
            ladder_index=ladder_index, requested_model=model,
            structured_mode=mode, rejection_category=category, http_status=http_status,
        ))
        if run_budget is not None:
            run_budget.add(None)  # failed attempts never carry a cost

    def outcome_record(result, *, accepted, category=None):
        usage_records.append(usage_record_from_result(
            result, run_id=run_id, source_file=source_file, page_range=page_range,
            ladder_index=ladder_index, accepted=accepted, rejection_category=category,
        ))
        if run_budget is not None:
            run_budget.add(result.cost_usd)

    if run_budget is not None and run_budget.exceeded():
        return _AttemptOutcome(
            None, None, usage_records,
            f"{model}: run-wide OpenRouter cost budget (${run_budget.limit}) "
            "reached before this model could be attempted",
            budget_exhausted=True,
        )

    # --- primary request ---
    try:
        result = _run_attempt(cfg, model, messages, response_format, mode,
                              attempt_type, 0, label)
    except ProviderError as exc:
        failed_record(attempt_type, exc.category, exc.http_status)
        return _AttemptOutcome(None, None, usage_records, f"{model}: {exc_summary(exc)}")
    except ExtractionError as exc:
        # Raised by _run_attempt itself for truncation or empty content -
        # before any JSON parsing, so there is nothing to repair.
        category = "truncated" if is_truncated_message(exc) else "empty"
        failed_record(attempt_type, category)
        return _AttemptOutcome(None, None, usage_records, f"{model}: {exc_summary(exc)}")

    # --- local cleanup, then at most one repair ---
    try:
        data = parse_json_response(result.text)
    except ExtractionError as parse_exc:
        outcome_record(result, accepted=False, category="malformed_json")
        if run_budget is not None and run_budget.exceeded():
            # The primary call's own cost (just recorded above) may have
            # crossed the run-wide budget - that overshoot is unavoidable
            # since cost is only known after the response, but no further
            # OpenRouter call (the repair retry) may now be issued.
            return _AttemptOutcome(
                None, None, usage_records,
                f"{model}: run-wide OpenRouter cost budget (${run_budget.limit}) "
                "reached; repair retry skipped",
                budget_exhausted=True,
            )
        logger.warning("%s: OpenRouter response from %s was not valid JSON (%s); "
                       "attempting one repair retry", label, model, parse_exc)
        try:
            repair = _run_attempt(
                cfg, model, _repair_messages(prompt, result.text), response_format,
                mode, ATTEMPT_REPAIR, 1, label,
            )
        except ProviderError as exc:
            failed_record(ATTEMPT_REPAIR, exc.category, exc.http_status)
            return _AttemptOutcome(
                None, None, usage_records, f"{model}: repair {exc_summary(exc)}"
            )
        except ExtractionError as exc:
            category = "truncated" if is_truncated_message(exc) else "empty"
            failed_record(ATTEMPT_REPAIR, category)
            return _AttemptOutcome(
                None, None, usage_records, f"{model}: repair {exc_summary(exc)}"
            )
        try:
            data = parse_json_response(repair.text)
        except ExtractionError as repair_parse_exc:
            outcome_record(repair, accepted=False, category="malformed_json")
            logger.warning("%s: JSON repair retry did not produce valid JSON; "
                           "escalating if another model is configured", label)
            return _AttemptOutcome(
                None, None, usage_records, f"{model}: {exc_summary(repair_parse_exc)}"
            )
        logger.info("%s: JSON repair retry recovered valid JSON", label)
        result = repair  # provenance reflects the successful (repair) attempt

    # --- schema + hard-required validation ---
    try:
        inv = _finalize(cfg, data, result.text, label, model)
    except ExtractionError as exc:
        outcome_record(result, accepted=False, category="missing_required_fields")
        return _AttemptOutcome(None, None, usage_records, f"{model}: {exc_summary(exc)}")

    outcome_record(result, accepted=True)
    return _AttemptOutcome(inv, result, usage_records, None)


def is_truncated_message(exc: ExtractionError) -> bool:
    """True for the specific ExtractionError _run_attempt raises on
    finish_reason=length (vs. the sibling empty-content case)."""
    return "truncated" in str(exc)


def extract_from_text_ladder(
    cfg: Config, invoice_text: str, *, run_id: str, source_file: str,
    page_range: str, label: str = "openrouter_text",
    run_budget: RunBudget | None = None,
) -> tuple[Invoice, ProviderResult, list]:
    """Try each configured OPENROUTER_TEXT_MODELS entry in order (1-4 models);
    the first accepted extraction stops the ladder. Escalates on any unusable
    result (transport/HTTP failure, embedded error envelope, empty content,
    truncation, malformed JSON after one repair, or missing hard-required
    fields) - never on soft validation outcomes, since validate_invoice() is
    only called later in pipeline.py, after a model has already been accepted.

    run_budget, when given, is the shared run-wide cost tracker (see
    usage.RunBudget) - checked here before every model attempt (covering both
    the first/"primary" model and later "escalation" models uniformly, since
    this loop drives both) and, one level deeper, inside _attempt_model before
    its repair call. Either checkpoint tripping stops this ladder immediately
    (budget_exhausted=True) rather than trying the next model, since a
    run-wide crossing blocks every model equally.

    Returns (Invoice, accepted ProviderResult, usage_records for every
    attempt across the whole ladder). Raises LadderExhaustedError (carrying
    all usage_records collected so far) if every model fails or a per-file
    budget/attempt cap (or the run-wide budget) stops escalation first.
    """
    models = cfg.openrouter_text_models
    usage_records: list = []
    failures: list[str] = []
    spent = Decimal("0")
    attempts_used = 0

    for ladder_index, model in enumerate(models):
        if (
            cfg.max_model_attempts_per_file is not None
            and attempts_used >= cfg.max_model_attempts_per_file
        ):
            failures.append(
                f"model-attempt cap ({cfg.max_model_attempts_per_file}) reached"
            )
            break
        if (
            cfg.max_cost_usd_per_file is not None
            and spent >= cfg.max_cost_usd_per_file
        ):
            failures.append(
                f"file cost budget (${cfg.max_cost_usd_per_file}) reached"
            )
            break
        if run_budget is not None and run_budget.exceeded():
            failures.append(
                f"run-wide OpenRouter cost budget (${run_budget.limit}) reached"
            )
            break
        attempts_used += 1

        outcome = _attempt_model(
            cfg, invoice_text, model, ladder_index=ladder_index, run_id=run_id,
            source_file=source_file, page_range=page_range,
            label=f"{label}_m{ladder_index}", run_budget=run_budget,
        )
        usage_records.extend(outcome.usage_records)
        spent += sum((r.cost_usd or Decimal("0")) for r in outcome.usage_records)

        if outcome.invoice is not None:
            return outcome.invoice, outcome.provider_result, usage_records
        failures.append(outcome.failure_summary)
        if outcome.budget_exhausted:
            break

    raise LadderExhaustedError(
        "; ".join(failures) if failures else "no OpenRouter text models configured",
        usage_records,
    )
