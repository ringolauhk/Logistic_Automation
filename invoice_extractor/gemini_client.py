"""Gemini calls: cheap text normalization and primary vision extraction."""

import warnings

# google-generativeai is deprecated in favor of google-genai but still works;
# silence its noisy import-time FutureWarning until we migrate.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from invoice_extractor.config import Config
from invoice_extractor.prompts import parse_json_response, text_extraction_prompt, vision_extraction_prompt
from invoice_extractor.schema import check_required, normalize_invoice

try:
    from google.api_core import exceptions as _gexc

    TRANSIENT_ERRORS: tuple = (
        _gexc.TooManyRequests,
        _gexc.ResourceExhausted,
        _gexc.ServiceUnavailable,
        _gexc.InternalServerError,
        _gexc.DeadlineExceeded,
        _gexc.GatewayTimeout,
        ConnectionError,
        TimeoutError,
    )
except ImportError:  # pragma: no cover
    TRANSIENT_ERRORS = (ConnectionError, TimeoutError)

_configured = False


def _get_model(cfg: Config) -> genai.GenerativeModel:
    global _configured
    if not cfg.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    if not _configured:
        genai.configure(api_key=cfg.gemini_api_key)
        _configured = True
    return genai.GenerativeModel(cfg.gemini_model)


def _call_with_retry(fn, max_retries: int) -> str:
    retryer = Retrying(
        retry=retry_if_exception_type(TRANSIENT_ERRORS),
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retryer(fn)


_GENERATION_CONFIG = {"response_mime_type": "application/json", "temperature": 0}


def extract_from_text(cfg: Config, invoice_text: str) -> dict:
    """Normalize already-extracted invoice text via a text-only Gemini call."""
    model = _get_model(cfg)
    prompt = text_extraction_prompt(invoice_text)

    def call() -> str:
        resp = model.generate_content(
            prompt,
            generation_config=_GENERATION_CONFIG,
            request_options={"timeout": 120},
        )
        return resp.text

    inv = normalize_invoice(parse_json_response(_call_with_retry(call, cfg.max_retries)))
    check_required(inv)
    return inv


def extract_from_images(cfg: Config, images: list[bytes]) -> dict:
    """Extract from rendered page PNGs via Gemini vision."""
    model = _get_model(cfg)
    parts: list = [vision_extraction_prompt(len(images))]
    parts.extend({"mime_type": "image/png", "data": png} for png in images)

    def call() -> str:
        resp = model.generate_content(
            parts,
            generation_config=_GENERATION_CONFIG,
            request_options={"timeout": 180},
        )
        return resp.text

    inv = normalize_invoice(parse_json_response(_call_with_retry(call, cfg.max_retries)))
    check_required(inv)
    return inv
