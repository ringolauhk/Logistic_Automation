"""Claude (Sonnet) calls: fallback provider for vision and text extraction."""

import base64

import anthropic
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from invoice_extractor.config import Config
from invoice_extractor.prompts import parse_json_response, text_extraction_prompt, vision_extraction_prompt
from invoice_extractor.schema import check_required, normalize_invoice

# Retryable: rate limits (429), server errors incl. overloaded (5xx/529),
# and network/timeout failures. 4xx request errors are not retried.
TRANSIENT_ERRORS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,  # includes APITimeoutError
)

_client: anthropic.Anthropic | None = None


def _get_client(cfg: Config) -> anthropic.Anthropic:
    global _client
    if not cfg.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    if _client is None:
        # max_retries=0: tenacity owns the retry policy (single backoff layer).
        _client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=0)
    return _client


def _call_with_retry(fn, max_retries: int) -> str:
    retryer = Retrying(
        retry=retry_if_exception_type(TRANSIENT_ERRORS),
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retryer(fn)


def _create_and_read(client: anthropic.Anthropic, cfg: Config, content) -> str:
    resp = client.messages.create(
        model=cfg.claude_model,
        max_tokens=8192,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": content}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def extract_from_text(cfg: Config, invoice_text: str) -> dict:
    """Text-only fallback when Gemini text normalization fails."""
    client = _get_client(cfg)

    def call() -> str:
        return _create_and_read(client, cfg, text_extraction_prompt(invoice_text))

    inv = normalize_invoice(parse_json_response(_call_with_retry(call, cfg.max_retries)))
    check_required(inv)
    return inv


def extract_from_images(cfg: Config, images: list[bytes]) -> dict:
    """Vision fallback when Gemini vision fails or returns unusable output."""
    client = _get_client(cfg)
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

    def call() -> str:
        return _create_and_read(client, cfg, content)

    inv = normalize_invoice(parse_json_response(_call_with_retry(call, cfg.max_retries)))
    check_required(inv)
    return inv
