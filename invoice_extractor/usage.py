"""OpenRouter per-request usage/cost accounting: records, CSV sidecar, and the
end-of-run summary (M3).

Metadata only - never invoice text, prompts, images, raw model output, raw
provider responses, or API keys. Every field on UsageRecord is either a
count/id/category or a token/cost number; nothing here can carry confidential
document content (see UsageRecord's docstring for the exact fields).
"""

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from invoice_extractor.provider import (
    ATTEMPT_PRIMARY,
    ATTEMPT_REPAIR,
    ProviderResult,
)
from invoice_extractor.schema import ExtractionError

# Deterministic column order for the .usage.csv sidecar - do not reorder
# without a documented reason; downstream tooling may rely on position.
USAGE_CSV_COLUMNS = [
    "run_id", "source_file", "route", "page_range", "attempt_type",
    "ladder_index", "requested_model", "actual_model", "structured_mode",
    "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
    "cost_usd", "finish_reason", "native_finish_reason", "generation_id",
    "latency_ms", "accepted", "rejection_category", "http_status",
]


@dataclass(frozen=True)
class UsageRecord:
    """One OpenRouter HTTP attempt's metadata - never response/request content.

    accepted=True marks the single attempt (per file, per route) whose
    Invoice was ultimately used; every other attempt (rejected primary/
    repair/escalation calls) is accepted=False with a rejection_category.
    cost_usd is None when OpenRouter did not report a cost for this specific
    attempt - see write_usage_csv's docstring for the resulting policy
    (never blocks progress; serialized as a blank cell, not "0", so it stays
    visibly distinct from a genuine zero-cost attempt).
    """

    run_id: str
    source_file: str
    route: str
    page_range: str
    attempt_type: str
    ladder_index: int
    requested_model: str
    actual_model: str | None
    structured_mode: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cost_usd: Decimal | None
    finish_reason: str | None
    native_finish_reason: str | None
    generation_id: str | None
    latency_ms: float | None
    accepted: bool
    rejection_category: str | None
    http_status: int | None


def usage_record_from_result(
    result: ProviderResult, *, run_id: str, source_file: str, page_range: str,
    ladder_index: int, accepted: bool, rejection_category: str | None = None,
) -> UsageRecord:
    """Build a usage record from a normalized ProviderResult - the HTTP call
    succeeded and produced a parseable envelope, whether or not the extracted
    content was ultimately accepted."""
    return UsageRecord(
        run_id=run_id, source_file=source_file, route=result.route,
        page_range=page_range, attempt_type=result.attempt_type,
        ladder_index=ladder_index, requested_model=result.requested_model,
        actual_model=result.actual_model, structured_mode=result.structured_mode,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        reasoning_tokens=result.reasoning_tokens, total_tokens=result.total_tokens,
        cost_usd=result.cost_usd, finish_reason=result.finish_reason,
        native_finish_reason=result.native_finish_reason,
        generation_id=result.generation_id, latency_ms=result.latency_ms,
        accepted=accepted, rejection_category=rejection_category, http_status=None,
    )


def usage_record_for_failed_attempt(
    *, run_id: str, source_file: str, route: str, page_range: str,
    attempt_type: str, ladder_index: int, requested_model: str,
    structured_mode: str, rejection_category: str, http_status: int | None = None,
) -> UsageRecord:
    """Build a usage record for an attempt that failed before producing a
    normalized ProviderResult (transport failure, HTTP error status, or an
    embedded OpenRouter error envelope). All token/cost/finish/generation
    fields are None since none were available - this is a deliberate
    simplification: we do not guess at partial usage from an error envelope's
    body, since OpenRouter does not document that as reliably present."""
    return UsageRecord(
        run_id=run_id, source_file=source_file, route=route, page_range=page_range,
        attempt_type=attempt_type, ladder_index=ladder_index,
        requested_model=requested_model, actual_model=None,
        structured_mode=structured_mode, input_tokens=None, output_tokens=None,
        reasoning_tokens=None, total_tokens=None, cost_usd=None,
        finish_reason=None, native_finish_reason=None, generation_id=None,
        latency_ms=None, accepted=False, rejection_category=rejection_category,
        http_status=http_status,
    )


class LadderExhaustedError(ExtractionError):
    """Every configured model in the ladder failed (or the file's model-
    attempt/cost cap was reached before any model succeeded).

    Carries the usage records for every attempt made across the whole ladder
    for this file/route, so the caller (pipeline.process_file) can still
    account for them even though extraction ultimately failed - the same
    getattr(exc, "usage_records", []) pattern already used for the direct-
    gateway dual-cause ExtractionError recovers these.
    """

    def __init__(self, message: str, usage_records: list[UsageRecord]):
        super().__init__(message)
        self.usage_records = usage_records


@dataclass
class RunBudget:
    """Shared, mutable run-wide OpenRouter cost tracker.

    Exactly ONE instance is created per process_directory() call and threaded
    by reference through process_file -> _run_text_route ->
    _run_openrouter_text_route -> extract_from_text_ladder -> _attempt_model,
    so every checkpoint (each primary attempt, each repair, each escalation,
    each new file) consults the same live `spent` total - never a stale
    per-function copy. `spent` is mutated in exactly one place
    (_attempt_model, immediately after each usage record is built), so
    process_directory must read it, never add to it, to avoid double
    counting.

    limit=None means no run-wide budget is configured; exceeded() is then
    always False. A cost_usd=None attempt (unknown cost) contributes
    Decimal("0") here, the same non-blocking policy used everywhere else in
    this module - budget enforcement is therefore incomplete whenever unknown
    costs exist, which is why format_usage_summary separately surfaces an
    unknown-cost count for the human reader.
    """

    limit: Decimal | None
    spent: Decimal = field(default_factory=lambda: Decimal("0"))

    def exceeded(self) -> bool:
        return self.limit is not None and self.spent >= self.limit

    def add(self, cost: Decimal | None) -> None:
        self.spent += cost or Decimal("0")


def _cost_cell(cost: Decimal | None) -> str:
    return "" if cost is None else str(cost)


def _cell(value) -> str:
    return "" if value is None else str(value)


def usage_csv_path(output_path: str | Path) -> Path:
    """<output-workbook-stem>.usage.csv, written next to the workbook."""
    output_path = Path(output_path)
    return output_path.with_name(output_path.stem + ".usage.csv")


def write_usage_csv(records: list[UsageRecord], path: str | Path) -> Path:
    """Write the usage sidecar CSV: UTF-8, deterministic column order, one
    row per request attempt.

    Always writes the file when called, even with an empty `records` list
    (header row only) - a zero-attempt OpenRouter run still produces the
    sidecar, so its mere presence/absence tells you which gateway a run used.
    The CALLER decides whether to call this at all: cli.py only calls it for
    LLM_GATEWAY=openrouter - the direct gateway never produces usage records
    and gets no sidecar file at all, since there is nothing to report.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(USAGE_CSV_COLUMNS)
        for r in records:
            writer.writerow([
                r.run_id, r.source_file, r.route, r.page_range, r.attempt_type,
                r.ladder_index, r.requested_model, _cell(r.actual_model),
                r.structured_mode,
                _cell(r.input_tokens), _cell(r.output_tokens),
                _cell(r.reasoning_tokens), _cell(r.total_tokens),
                _cost_cell(r.cost_usd),
                _cell(r.finish_reason), _cell(r.native_finish_reason),
                _cell(r.generation_id), _cell(r.latency_ms),
                r.accepted, _cell(r.rejection_category), _cell(r.http_status),
            ])
    return path


@dataclass
class UsageSummary:
    total_requests: int
    primary_requests: int
    repair_requests: int
    escalation_requests: int
    accepted_requests: int
    rejected_requests: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_cost_usd: Decimal
    cost_by_model: dict  # requested_model -> Decimal
    accepted_by_model: dict  # requested_model -> int
    unknown_cost_count: int


def summarize_usage(records: list[UsageRecord]) -> UsageSummary:
    """Aggregate usage records. Missing (None) cost contributes Decimal("0")
    to every running total here - unknown cost never blocks accounting, it
    only affects the per-attempt CSV cell (see write_usage_csv). Records with
    cost_usd=None are additionally counted in unknown_cost_count so
    format_usage_summary can label the total as incomplete rather than
    silently presenting it as exact."""
    cost_by_model: dict[str, Decimal] = {}
    accepted_by_model: dict[str, int] = {}
    input_tokens = output_tokens = reasoning_tokens = 0
    total_cost = Decimal("0")
    primary = repair = escalation = accepted = rejected = unknown_cost_count = 0

    for r in records:
        if r.attempt_type == ATTEMPT_PRIMARY:
            primary += 1
        elif r.attempt_type == ATTEMPT_REPAIR:
            repair += 1
        else:
            escalation += 1
        if r.accepted:
            accepted += 1
            accepted_by_model[r.requested_model] = (
                accepted_by_model.get(r.requested_model, 0) + 1
            )
        else:
            rejected += 1
        input_tokens += r.input_tokens or 0
        output_tokens += r.output_tokens or 0
        reasoning_tokens += r.reasoning_tokens or 0
        if r.cost_usd is None:
            unknown_cost_count += 1
        cost = r.cost_usd or Decimal("0")
        total_cost += cost
        cost_by_model[r.requested_model] = cost_by_model.get(r.requested_model, Decimal("0")) + cost

    return UsageSummary(
        total_requests=len(records), primary_requests=primary, repair_requests=repair,
        escalation_requests=escalation, accepted_requests=accepted,
        rejected_requests=rejected, input_tokens=input_tokens,
        output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
        total_cost_usd=total_cost, cost_by_model=cost_by_model,
        accepted_by_model=accepted_by_model, unknown_cost_count=unknown_cost_count,
    )


def format_usage_summary(records: list[UsageRecord], processed_count: int) -> str:
    """Concise end-of-run OpenRouter summary for the CLI - counts and costs
    only, never invoice content or raw provider errors."""
    s = summarize_usage(records)
    avg_cost = (s.total_cost_usd / processed_count) if processed_count else Decimal("0")
    lines = [
        "-" * 52,
        "  OpenRouter usage",
        "-" * 52,
        f"  Requests:             {s.total_requests}",
        f"    - primary:          {s.primary_requests}",
        f"    - repair:           {s.repair_requests}",
        f"    - escalation:       {s.escalation_requests}",
        f"  Accepted attempts:    {s.accepted_requests}",
        f"  Rejected attempts:    {s.rejected_requests}",
        f"  Input tokens:         {s.input_tokens}",
        f"  Output tokens:        {s.output_tokens}",
        f"  Reasoning tokens:     {s.reasoning_tokens}",
    ]
    if s.unknown_cost_count:
        # Non-blocking policy unchanged (unknown cost still counts as $0 for
        # every total and for run/file budget enforcement) - but the total is
        # explicitly labeled incomplete so a human reader never mistakes it
        # for an exact figure.
        plural = "" if s.unknown_cost_count == 1 else "s"
        lines.append(f"  Requests with unknown cost: {s.unknown_cost_count}")
        lines.append(
            f"  Total reported cost: ${s.total_cost_usd} "
            f"(incomplete: {s.unknown_cost_count} request cost{plural} unavailable)"
        )
    else:
        lines.append(f"  Total cost (USD):     {s.total_cost_usd}")
    lines.append(f"  Avg cost / PDF (USD): {avg_cost}")
    if s.accepted_by_model:
        lines.append("  Accepted extractions by model:")
        for model, count in sorted(s.accepted_by_model.items()):
            lines.append(f"    - {model}: {count}")
    if s.cost_by_model:
        lines.append("  Cost by requested model (USD):")
        for model, cost in sorted(s.cost_by_model.items()):
            lines.append(f"    - {model}: {cost}")
    lines.append("-" * 52)
    return "\n".join(lines)
