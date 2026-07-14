"""Logging: run-ID tagging, secret redaction, sanitized exception summaries.

Normal logs never contain API keys, raw provider responses, extracted invoice
text, or image data. Exception messages are truncated summaries; anything
matching a configured secret is redacted defensively.
"""

import logging
import re
import uuid
from pathlib import Path

from invoice_extractor.config import ConfigurationError
from invoice_extractor.schema import ExtractionError

# Exception types whose str() is authored to be safe by THIS codebase and may
# therefore be logged verbatim (flattened + truncated). ExtractionError's
# message is guaranteed free of response/invoice content by construction (see
# its docstring; the raw payload rides on .detail, which is never logged).
# ConfigurationError carries only operator-facing config messages (never
# secrets/response content). RuntimeError is raised here only for missing-key
# / config guards with fixed, safe strings. Every OTHER type - all provider
# SDK errors, httpx, and any third-party/stdlib exception - is treated as
# UNTRUSTED: its str()/message may echo request payloads (invoice
# text/images), response bodies (model output), or raw JSON error bodies, so
# only safe STRUCTURED fields are emitted for those (class name + HTTP status
# + canonical status label). ProviderError subclasses ExtractionError, so its
# sanitized message is covered by the ExtractionError entry.
_TRUSTED_MESSAGE_TYPES = (ExtractionError, ConfigurationError, RuntimeError)

# A canonical provider status token like RESOURCE_EXHAUSTED / UNAVAILABLE:
# uppercase letters and underscores only. This shape cannot smuggle a response
# body or invoice text through the `.status` attribute.
_CANONICAL_STATUS = re.compile(r"^[A-Z][A-Z_]{1,39}$")


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _flatten_truncate(text: str, limit: int) -> str:
    msg = " ".join(text.split())
    if len(msg) > limit:
        msg = msg[:limit] + "...[truncated]"
    return msg


def exc_summary(exc: BaseException, limit: int = 240) -> str:
    """Sanitized one-line exception summary safe for logs and review reasons.

    Provider SDK exception strings are UNTRUSTED (they can echo invoice
    content, model output, or raw error bodies). Only our own exception types
    (see _TRUSTED_MESSAGE_TYPES) have their message included; for everything
    else, only the class name plus safe structured descriptors (HTTP status
    and canonical status label, duck-typed off the object) are emitted -
    never the raw message. Truncation is a secondary defense on the trusted
    path, not the primary one.
    """
    name = type(exc).__name__

    if isinstance(exc, _TRUSTED_MESSAGE_TYPES):
        msg = _flatten_truncate(str(exc), limit)
        return f"{name}: {msg}" if msg else name

    # Untrusted: build from structured fields only, never str(exc)/.message.
    descriptors: list[str] = []
    code = getattr(exc, "code", None)
    if not isinstance(code, int) or isinstance(code, bool):
        code = getattr(exc, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        descriptors.append(f"HTTP {code}")
    status = getattr(exc, "status", None)
    if isinstance(status, str) and _CANONICAL_STATUS.match(status):
        descriptors.append(status)

    return f"{name}: {' '.join(descriptors)}" if descriptors else name


class _ContextFilter(logging.Filter):
    """Injects the run ID into every record and redacts configured secrets."""

    def __init__(self, run_id: str, secrets: tuple[str, ...]):
        super().__init__()
        self.run_id = run_id
        self.secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        if self.secrets:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            redacted = msg
            for secret in self.secrets:
                if secret in redacted:
                    redacted = redacted.replace(secret, "***REDACTED***")
            if redacted != msg:
                record.msg = redacted
                record.args = None
        return True


def setup_logging(
    log_path: str | Path,
    run_id: str | None = None,
    secrets: tuple[str, ...] = (),
    verbose: bool = True,
) -> logging.Logger:
    """Log to the run log file (full detail) and the console."""
    run_id = run_id or new_run_id()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("invoice_extractor")
    logger.setLevel(logging.DEBUG)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    context = _ContextFilter(run_id, secrets)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(run_id)s] %(message)s")
    )
    file_handler.addFilter(context)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)-7s [%(run_id)s] %(message)s"))
    console.addFilter(context)
    logger.addHandler(console)

    return logger
