"""Structured, privacy-safe progress events for UI/automation consumers (M9).

The engine emits ProgressEvent objects through an OPTIONAL callback threaded
down the extraction call chain (`on_event=None` everywhere by default - the
CLI passes None and its behavior/logging is byte-for-byte unchanged).

Every field is metadata that is already safe to log under the project's
privacy rules: filenames, counts, routes, page ranges, attempt types, model
NAMES, elapsed times, costs, and the stable review CATEGORIES. Events can
never carry invoice text, extracted values, line descriptions, prompts,
provider responses, image bytes/base64, API keys, or stack traces - the
schema simply has no fields for them.

A failing callback must never break extraction: emit() swallows and safely
logs callback exceptions (type name only).

Transport concerns (schema_version envelope, monotonic sequence numbers, UTC
timestamps, job id, JSONL flushing) belong to the consumer that persists
events - see apps/web/progress.py - not to the engine-level event itself.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("invoice_extractor")

# Event type vocabulary (stable strings; consumers must tolerate unknown
# types for forward compatibility).
JOB_STARTED = "job_started"
FILE_STARTED = "file_started"
CLASSIFICATION_COMPLETE = "classification_complete"
CHUNK_STARTED = "chunk_started"
PROVIDER_REQUEST_STARTED = "provider_request_started"
PROVIDER_REQUEST_COMPLETED = "provider_request_completed"
FILE_COMPLETED = "file_completed"
FILE_NEEDS_REVIEW = "file_needs_review"
FILE_FAILED = "file_failed"
JOB_COMPLETED = "job_completed"
JOB_CANCELLED = "job_cancelled"


@dataclass(frozen=True)
class ProgressEvent:
    """One safe progress event. All context fields optional."""

    event: str
    source_file: str | None = None
    file_index: int | None = None       # 1-based
    file_total: int | None = None
    classification: str | None = None   # text-native | image-only | mixed | error
    route: str | None = None            # text | vision
    page_range: str | None = None
    chunk_index: int | None = None      # 1-based
    chunk_total: int | None = None
    attempt_type: str | None = None     # primary | repair | escalation
    ladder_index: int | None = None     # 0-based position in the model ladder
    model_count: int | None = None
    requested_model: str | None = None
    provider: str | None = None         # openrouter | gemini | claude
    accepted: bool | None = None
    elapsed_seconds: float | None = None
    request_count: int | None = None
    repair_count: int | None = None
    escalation_count: int | None = None
    needs_review: bool | None = None
    error: bool | None = None
    review_categories: tuple[str, ...] | None = None
    extraction_method: str | None = None
    model: str | None = None            # accepted model(s) / "multiple"
    reported_cost: str | None = None    # Decimal string; never a float
    unknown_cost_count: int | None = None
    files_processed: int | None = None
    interrupted: bool | None = None


OnEvent = Optional[Callable[[ProgressEvent], None]]


def emit(on_event: OnEvent, event: ProgressEvent) -> None:
    """Deliver one event; a broken consumer can never break extraction."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception as exc:  # noqa: BLE001 - deliberate isolation boundary
        logger.warning("progress callback failed (%s); extraction continues",
                       type(exc).__name__)


class RequestNotifier:
    """Bundles static request context so the direct Gemini/Claude clients can
    emit provider events without learning the pipeline's vocabulary.

    Built by the pipeline (which knows source_file/route/page_range/chunk),
    passed to the client as an optional argument; the client only reports
    attempt lifecycle (started/completed) with its model name.
    """

    def __init__(self, on_event: OnEvent, *, source_file: str, route: str,
                 page_range: str, provider: str, requested_model: str,
                 chunk_index: int | None = None, chunk_total: int | None = None):
        self._on_event = on_event
        self._ctx = dict(source_file=source_file, route=route,
                         page_range=page_range, provider=provider,
                         requested_model=requested_model,
                         chunk_index=chunk_index, chunk_total=chunk_total)

    def started(self, attempt_type: str) -> None:
        emit(self._on_event, ProgressEvent(
            event=PROVIDER_REQUEST_STARTED, attempt_type=attempt_type, **self._ctx))

    def completed(self, attempt_type: str, accepted: bool) -> None:
        emit(self._on_event, ProgressEvent(
            event=PROVIDER_REQUEST_COMPLETED, attempt_type=attempt_type,
            accepted=accepted, **self._ctx))
