"""SDK-free provider abstraction for the OpenRouter gateway (M1).

The rest of the pipeline must not depend on OpenRouter-, Gemini-, or
Anthropic-specific response objects. These small, frozen dataclasses are the
internal boundary types the gateway produces. Fields that carry confidential
content (the request messages - invoice text and/or base64 image data - and
the model's output text - i.e. extracted invoice content) are marked
`repr=False` so they can never leak through an object's repr in a log line,
traceback, or exception message. API keys, request headers, and raw provider
response dicts are never stored on these objects at all.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from invoice_extractor.schema import ExtractionError

GATEWAY_OPENROUTER = "openrouter"

ROUTE_TEXT = "text"
ROUTE_VISION = "vision"

# Structured-output mode actually used for an attempt (recorded per attempt so
# accuracy/cost can be attributed to it in a later milestone).
MODE_JSON_SCHEMA = "json_schema"
MODE_JSON_OBJECT = "json_object"
MODE_PROMPT_ONLY = "prompt_only"

# Attempt classification for usage accounting / escalation.
ATTEMPT_PRIMARY = "primary"
ATTEMPT_REPAIR = "repair"
ATTEMPT_ESCALATION = "escalation"  # the first call for a model AFTER model 0

# OpenRouter's normalized finish_reason for output that hit the token cap.
FINISH_LENGTH = "length"


class ProviderError(ExtractionError):
    """A sanitized provider/transport failure at the OpenRouter boundary.

    Subclasses ExtractionError so it flows through the pipeline's existing
    failure handling and is trusted (message-safe) by exc_summary. The message
    is authored to be safe - it never contains raw response bodies, provider
    error text, request payloads, or headers. Safe structured signals for a
    future model-escalation ladder ride on attributes, not in the message:

      category    - coarse, fixed class: "rate_limited" | "server_error" |
                    "client_error" | "timeout" | "transport" |
                    "malformed_envelope"
      http_status - the HTTP status code when one is known, else None
    """

    def __init__(
        self,
        message: str,
        *,
        category: str,
        http_status: int | None = None,
        detail: str | None = None,
    ):
        super().__init__(message, detail=detail)
        self.category = category
        self.http_status = http_status


@dataclass(frozen=True)
class ProviderRequest:
    """One extraction attempt's request, gateway-agnostic.

    `messages` holds the OpenRouter message payload, which contains invoice
    text and/or base64 image data - hence repr=False. No API key or headers
    are stored here; those live only on the HTTP client.
    """

    route: str
    requested_model: str
    structured_mode: str
    max_tokens: int
    attempt_type: str = ATTEMPT_PRIMARY
    attempt_index: int = 0
    messages: list = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class ProviderResult:
    """One extraction attempt's normalized outcome, gateway-agnostic.

    `text` is the model's output content (i.e. extracted invoice data) - hence
    repr=False. The raw OpenRouter response dict is intentionally NOT stored.
    """

    requested_model: str
    route: str
    gateway: str = GATEWAY_OPENROUTER
    actual_model: str | None = None
    attempt_type: str = ATTEMPT_PRIMARY
    attempt_index: int = 0
    structured_mode: str = MODE_PROMPT_ONLY
    text: str = field(default="", repr=False)
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: Decimal | None = None
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    generation_id: str | None = None
    latency_ms: float | None = None


def is_truncated(result: ProviderResult) -> bool:
    """True when the model stopped because it hit the output-token cap.

    OpenRouter normalizes this to finish_reason == "length" (the equivalent of
    Anthropic's stop_reason == "max_tokens"). A future milestone uses this to
    classify truncation explicitly rather than as a random syntax error.
    """
    return result.finish_reason == FINISH_LENGTH


def coerce_cost(value) -> Decimal | None:
    """Coerce OpenRouter's usage.cost (a JSON number) to Decimal, or None.

    Decimal (not float) keeps run-total cost summation exact in a later
    accounting milestone, consistent with the project's money-handling policy.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
