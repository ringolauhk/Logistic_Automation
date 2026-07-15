"""OpenRouter gateway boundary and the text/vision model ladders.

Direct httpx against the OpenRouter chat-completions API. `extract_from_text`
(M2) is a single-model extraction, kept as-is for its existing tests;
`extract_from_text_ladder` (M3) and `extract_from_vision_ladder` (M4) try an
ordered list of models (OPENROUTER_TEXT_MODELS / OPENROUTER_VISION_MODELS
respectively) via the shared `_run_ladder`, escalating on any unusable result
and stopping at the first accepted extraction. Vision requests are one
multimodal message (base64 PNG data-URL blocks + prompt); vision REPAIR
requests are text-only - images are never resent. Makes no live calls in the
test suite (the network-blocking autouse fixture guards; tests mock
`_chat_completion`).

Privacy: the API key lives only in the HTTP client's headers, never on a
returned object and never in a log line or exception. Provider error bodies
are untrusted and are never surfaced - only the HTTP status code and a fixed
category are retained (see ProviderError). Image bytes/base64 exist only in
the in-memory request messages: never logged, never stored on results or
exceptions, never written by save_debug_artifact (which persists response
TEXT only, and only when SAVE_DEBUG_ARTIFACTS is explicitly enabled).
"""

import base64
import logging
import time
from dataclasses import dataclass

import httpx

from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.config import Config
from invoice_extractor.prompts import (
    JSON_SCHEMA_BLOCK,
    openrouter_vision_prompt,
    parse_json_response,
    text_chunk_context,
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
    ROUTE_VISION,
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
    FileBudget,
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
    attempt_type: str, attempt_index: int, label: str, route: str = ROUTE_TEXT,
) -> ProviderResult:
    """One OpenRouter request (with bounded transport retry), normalized and
    checked for truncation/empty content BEFORE any JSON parsing.

    Truncation (finish_reason=length) and empty content raise a clear,
    sanitized ExtractionError - neither is retried and neither triggers the
    JSON-repair path (repair is only for malformed-but-complete JSON). The
    already-normalized ProviderResult (actual_model, tokens, cost,
    generation_id, finish_reason - all safe metadata) is attached to the
    raised exception as `.provider_result` so a caller that wants full usage
    accounting for a rejected-but-parseable response can recover it (see
    _attempt_model) without changing this function's raise/message contract -
    extract_from_text (M2) does not read the attribute, so it is unaffected.
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
        raw, requested_model=model, route=route,
        attempt_index=attempt_index, attempt_type=attempt_type,
        structured_mode=structured_mode, latency_ms=latency_ms,
    )
    if is_truncated(result):
        exc = ExtractionError(
            "OpenRouter response truncated before valid JSON completed "
            "(finish_reason=length)"
        )
        exc.provider_result = result
        raise exc
    if not result.text.strip():
        exc = ExtractionError("empty response from model")
        exc.provider_result = result
        raise exc
    return result


def _finalize(
    cfg: Config, data: dict, raw_text: str, label: str, model: str,
    *, require_hard_fields: bool = True,
) -> Invoice:
    """require_hard_fields=False (M3.1) is used ONLY when this call is one
    chunk of a multi-chunk text-native document: a chunk covering only
    line-item pages will legitimately lack invoice_date/currency/seller_name/
    total_amount, and rejecting it here would discard perfectly good line
    items for no reason. The full hard-required contract is NOT weakened -
    it is enforced exactly once, on the AGGREGATED invoice, in
    pipeline.process_file's Stage 6 (validate_invoice, unchanged). For every
    other caller (single-chunk ladder call, and extract_from_text's M2 path)
    this defaults to True, identical to pre-M3.1 behavior.
    """
    extras = unknown_keys(data)
    if extras:
        logger.debug("%s: dropped %d unexpected key(s): %s",
                     label, len(extras), ", ".join(extras[:8]))
    inv = normalize_invoice(data)
    if require_hard_fields:
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
    cfg: Config, model: str, *, messages: list, repair_prompt: str, route: str,
    ladder_index: int, run_id: str, source_file: str, page_range: str, label: str,
    run_budget: RunBudget | None = None, file_budget: FileBudget | None = None,
    require_hard_fields: bool = True,
) -> _AttemptOutcome:
    """Try exactly one model: primary request, local cleanup, at most one
    repair, then schema + (usually) hard-required validation. Never raises -
    failures are reported via the returned _AttemptOutcome so the ladder can
    escalate. Route-agnostic (M4): the caller supplies the prebuilt primary
    `messages` (text-only or multimodal) and the textual `repair_prompt`; the
    repair request is ALWAYS text-only (built by _repair_messages from
    repair_prompt + the malformed response), so a vision chunk's repair never
    resends its images.

    run_budget/file_budget, when given, are the shared run-wide/file-wide
    cost trackers: their live totals are updated immediately after every
    usage record is built (never batched), and checked immediately before
    the primary call and again before the repair call - the two HTTP call
    sites inside this function - so a crossing stops further calls at the
    earliest possible point, even mid-file/mid-chunk.

    require_hard_fields=False means this call is ONE chunk of a document
    whose OpenRouter extraction spans multiple requests: _finalize's
    hard-required check is skipped for THIS chunk (a line-item-only chunk
    legitimately lacks headers) - the full hard-required contract is still
    enforced once, on the aggregated invoice, by pipeline.process_file's
    Stage 6.
    """
    usage_records = []
    mode = cfg.openrouter_structured_output
    response_format = build_response_format(mode)
    attempt_type = ATTEMPT_PRIMARY if ladder_index == 0 else ATTEMPT_ESCALATION

    def failed_record(attempt_type_, category, http_status=None):
        usage_records.append(usage_record_for_failed_attempt(
            run_id=run_id, source_file=source_file, route=route,
            page_range=page_range, attempt_type=attempt_type_,
            ladder_index=ladder_index, requested_model=model,
            structured_mode=mode, rejection_category=category, http_status=http_status,
        ))
        if run_budget is not None:
            run_budget.add(None)  # failed attempts never carry a cost
        if file_budget is not None:
            file_budget.cost.add(None)

    def outcome_record(result, *, accepted, category=None):
        usage_records.append(usage_record_from_result(
            result, run_id=run_id, source_file=source_file, page_range=page_range,
            ladder_index=ladder_index, accepted=accepted, rejection_category=category,
        ))
        if run_budget is not None:
            run_budget.add(result.cost_usd)
        if file_budget is not None:
            file_budget.cost.add(result.cost_usd)

    def budget_exhausted_reason(context: str) -> str | None:
        # Note: both branches already end in "reached" - context must NOT
        # repeat it (e.g. "before the repair retry could be issued", never
        # "reached before...").
        if run_budget is not None and run_budget.exceeded():
            return f"run-wide OpenRouter cost budget (${run_budget.limit}) reached {context}"
        if file_budget is not None and file_budget.exceeded():
            return f"{file_budget.reason()} {context}"
        return None

    # No pre-check for the PRIMARY call here: the ladder loop (which drives
    # both the first/"primary" model and later "escalation" models) already
    # gates every model attempt on both budgets BEFORE calling this function
    # and BEFORE incrementing file_budget's attempt counter - re-checking
    # here would see the post-increment state and wrongly block the very
    # attempt the loop just approved. The repair call below has no such
    # outer gate, so it checks explicitly.

    # --- primary request ---
    try:
        result = _run_attempt(cfg, model, messages, response_format, mode,
                              attempt_type, 0, label, route)
    except ProviderError as exc:
        failed_record(attempt_type, exc.category, exc.http_status)
        return _AttemptOutcome(None, None, usage_records, f"{model}: {exc_summary(exc)}")
    except ExtractionError as exc:
        # Raised by _run_attempt itself for truncation or empty content -
        # before any JSON parsing, so there is nothing to repair. The
        # ProviderResult (if the response was a parseable envelope) rides on
        # the exception so full usage metadata is still preserved here.
        category = "truncated" if is_truncated_message(exc) else "empty"
        pr = getattr(exc, "provider_result", None)
        if pr is not None:
            outcome_record(pr, accepted=False, category=category)
        else:
            failed_record(attempt_type, category)
        return _AttemptOutcome(None, None, usage_records, f"{model}: {exc_summary(exc)}")

    # --- local cleanup, then at most one repair ---
    try:
        data = parse_json_response(result.text)
    except ExtractionError as parse_exc:
        outcome_record(result, accepted=False, category="malformed_json")
        repair_reason = budget_exhausted_reason("before the repair retry could be issued")
        if repair_reason is not None:
            # The primary call's own cost (just recorded above) may have
            # crossed the budget - that overshoot is unavoidable since cost
            # is only known after the response, but no further OpenRouter
            # call (the repair retry) may now be issued.
            return _AttemptOutcome(
                None, None, usage_records, f"{model}: {repair_reason}", budget_exhausted=True,
            )
        logger.warning("%s: OpenRouter response from %s was not valid JSON (%s); "
                       "attempting one repair retry", label, model, parse_exc)
        try:
            repair = _run_attempt(
                cfg, model, _repair_messages(repair_prompt, result.text),
                response_format, mode, ATTEMPT_REPAIR, 1, label, route,
            )
        except ProviderError as exc:
            failed_record(ATTEMPT_REPAIR, exc.category, exc.http_status)
            return _AttemptOutcome(
                None, None, usage_records, f"{model}: repair {exc_summary(exc)}"
            )
        except ExtractionError as exc:
            category = "truncated" if is_truncated_message(exc) else "empty"
            pr = getattr(exc, "provider_result", None)
            if pr is not None:
                outcome_record(pr, accepted=False, category=category)
            else:
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

    # --- schema + (usually) hard-required validation ---
    try:
        inv = _finalize(cfg, data, result.text, label, model,
                        require_hard_fields=require_hard_fields)
    except ExtractionError as exc:
        outcome_record(result, accepted=False, category="missing_required_fields")
        return _AttemptOutcome(None, None, usage_records, f"{model}: {exc_summary(exc)}")

    outcome_record(result, accepted=True)
    return _AttemptOutcome(inv, result, usage_records, None)


def is_truncated_message(exc: ExtractionError) -> bool:
    """True for the specific ExtractionError _run_attempt raises on
    finish_reason=length (vs. the sibling empty-content case)."""
    return "truncated" in str(exc)


def _run_ladder(
    cfg: Config, models: tuple, *, messages: list, repair_prompt: str, route: str,
    run_id: str, source_file: str, page_range: str, label: str,
    run_budget: RunBudget | None, file_budget: FileBudget | None,
    require_hard_fields: bool,
) -> tuple[Invoice, ProviderResult, list]:
    """Shared ladder driver for BOTH routes (M4): try each model in `models`
    in order; the first accepted extraction stops the ladder. Escalates on
    any unusable result (transport/HTTP failure, embedded error envelope,
    empty content, truncation, malformed JSON after one repair, or - when
    require_hard_fields - missing hard-required fields) - never on soft
    validation outcomes, since validate_invoice() is only called later in
    pipeline.py, after a model has already been accepted.

    run_budget, when given, is the shared run-wide cost tracker (see
    usage.RunBudget). file_budget, when given, is the shared per-file cost/
    attempt tracker (see usage.FileBudget) spanning every chunk of the SAME
    file across BOTH routes - when omitted, a fresh one is created here
    scoped to just this one call, so a caller that doesn't chunk gets one
    call == one file == one budget. Both are checked before every model
    attempt (covering both the first/"primary" model and later "escalation"
    models uniformly, since this loop drives both) and, one level deeper,
    inside _attempt_model before its repair call. Any checkpoint tripping
    stops this ladder immediately (budget_exhausted=True) rather than trying
    the next model, since a crossing blocks every model equally.

    Returns (Invoice, accepted ProviderResult, usage_records for every
    attempt across the whole ladder). Raises LadderExhaustedError (carrying
    all usage_records collected so far) if every model fails or a per-file
    budget/attempt cap (or the run-wide budget) stops escalation first.
    """
    usage_records: list = []
    failures: list[str] = []
    file_budget = file_budget or FileBudget(
        cfg.max_model_attempts_per_file, RunBudget(cfg.max_cost_usd_per_file)
    )

    for ladder_index, model in enumerate(models):
        if file_budget.attempts_exceeded():
            failures.append(f"model-attempt cap ({file_budget.max_attempts}) reached")
            break
        if file_budget.cost.exceeded():
            failures.append(f"file cost budget (${file_budget.cost.limit}) reached")
            break
        if run_budget is not None and run_budget.exceeded():
            failures.append(
                f"run-wide OpenRouter cost budget (${run_budget.limit}) reached"
            )
            break
        file_budget.record_attempt()

        outcome = _attempt_model(
            cfg, model, messages=messages, repair_prompt=repair_prompt, route=route,
            ladder_index=ladder_index, run_id=run_id, source_file=source_file,
            page_range=page_range, label=f"{label}_m{ladder_index}",
            run_budget=run_budget, file_budget=file_budget,
            require_hard_fields=require_hard_fields,
        )
        usage_records.extend(outcome.usage_records)

        if outcome.invoice is not None:
            return outcome.invoice, outcome.provider_result, usage_records
        failures.append(outcome.failure_summary)
        if outcome.budget_exhausted:
            break

    raise LadderExhaustedError(
        "; ".join(failures) if failures else f"no OpenRouter {route} models configured",
        usage_records,
    )


def extract_from_text_ladder(
    cfg: Config, invoice_text: str, *, run_id: str, source_file: str,
    page_range: str, label: str = "openrouter_text",
    run_budget: RunBudget | None = None, file_budget: FileBudget | None = None,
    is_chunked: bool = False,
) -> tuple[Invoice, ProviderResult, list]:
    """OPENROUTER_TEXT_MODELS ladder over one text chunk (or a whole
    text-native document when it fits in a single request). Thin wrapper:
    builds the text prompt/messages (with the partial-document chunk context
    when is_chunked) and delegates to the shared _run_ladder - see its
    docstring for escalation/budget semantics. is_chunked also relaxes the
    per-chunk hard-required check (see _attempt_model)."""
    chunk_context = text_chunk_context(page_range) if is_chunked else None
    prompt = text_extraction_prompt(invoice_text, chunk_context=chunk_context)
    return _run_ladder(
        cfg, cfg.openrouter_text_models, messages=build_text_messages(prompt),
        repair_prompt=prompt, route=ROUTE_TEXT, run_id=run_id,
        source_file=source_file, page_range=page_range, label=label,
        run_budget=run_budget, file_budget=file_budget,
        require_hard_fields=not is_chunked,
    )


def extract_from_vision_ladder(
    cfg: Config, images: list[bytes], *, run_id: str, source_file: str,
    page_range: str, label: str = "openrouter_vision",
    run_budget: RunBudget | None = None, file_budget: FileBudget | None = None,
    is_chunked: bool = False,
) -> tuple[Invoice, ProviderResult, list]:
    """OPENROUTER_VISION_MODELS ladder over one vision chunk's rendered PNG
    page images (M4). Builds ONE multimodal message (base64 PNG data-URL
    blocks in page order + the vision prompt, which always states the page
    range and adds the partial-document context when is_chunked) and
    delegates to the shared _run_ladder - identical escalation, repair,
    budget, and usage semantics to the text route, with route="vision" on
    every usage record.

    Repair requests are TEXT-ONLY (the images are never resent): the
    malformed JSON is already the model's reading of the images, so repair
    is purely a formatting fix - cheaper, and it keeps image bytes out of
    every retry path. Image bytes/base64 live only in the in-memory request
    messages: never logged, never stored on exceptions/dataclasses, never
    written to debug artifacts (save_debug_artifact receives response TEXT
    only, and only when SAVE_DEBUG_ARTIFACTS is explicitly enabled).
    """
    prompt = openrouter_vision_prompt(len(images), page_range, is_chunked=is_chunked)
    return _run_ladder(
        cfg, cfg.openrouter_vision_models, messages=build_vision_messages(prompt, images),
        repair_prompt=prompt, route=ROUTE_VISION, run_id=run_id,
        source_file=source_file, page_range=page_range, label=label,
        run_budget=run_budget, file_budget=file_budget,
        require_hard_fields=not is_chunked,
    )
