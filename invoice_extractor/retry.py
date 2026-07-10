"""Shared bounded-exponential-backoff retry wrapper for provider calls."""

import logging
from typing import Callable

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger("invoice_extractor")

# Module-level so tests can swap in tenacity.wait_none() - tests must never
# sleep for real retry delays. Bounded: 1s -> 2s -> 4s ... capped at 30s.
WAIT_STRATEGY = wait_exponential(multiplier=1, min=1, max=30)


def call_with_retry(
    fn: Callable[[], str],
    is_transient: Callable[[BaseException], bool],
    max_attempts: int,
    label: str,
) -> tuple[str, int]:
    """Run fn with retries on transient errors only. Returns (result, attempts).

    Non-transient errors (auth failures, invalid model, bad requests,
    unusable output) raise immediately without retrying.
    """

    def _before_sleep(retry_state) -> None:
        exc = retry_state.outcome.exception()
        # Exception type only - messages could echo request/response content.
        logger.warning(
            "%s: transient %s, retrying (attempt %d/%d)",
            label, type(exc).__name__, retry_state.attempt_number, max_attempts,
        )

    retryer = Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_attempts),
        wait=WAIT_STRATEGY,
        before_sleep=_before_sleep,
        reraise=True,
    )
    result = retryer(fn)
    return result, retryer.statistics.get("attempt_number", 1)
