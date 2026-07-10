"""Logging: run-ID tagging, secret redaction, sanitized exception summaries.

Normal logs never contain API keys, raw provider responses, extracted invoice
text, or image data. Exception messages are truncated summaries; anything
matching a configured secret is redacted defensively.
"""

import logging
import uuid
from pathlib import Path


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def exc_summary(exc: BaseException, limit: int = 240) -> str:
    """Sanitized one-line exception category + message for logs."""
    msg = " ".join(str(exc).split())
    if len(msg) > limit:
        msg = msg[:limit] + "...[truncated]"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


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
