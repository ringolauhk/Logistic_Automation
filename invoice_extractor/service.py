"""Shared extraction application service (M9).

ONE orchestration function - run_extraction() - used by BOTH the CLI `run`
command and the web UI's worker subprocess, so there is exactly one place
that: runs the batch, handles operator interruption, writes the artifact set
atomically (workbook + usage CSV + optional run metadata as one staged set),
and builds the privacy-safe run-metadata document.

The service is UI-agnostic: no click, no Streamlit, no printing. Callers own
presentation (console summary / progress page), exit codes, and preflight
messaging. Progress flows through the optional structured on_event callback
(see invoice_extractor.events); the CLI passes None and behaves exactly as
before.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from invoice_extractor.atomic import StagedArtifacts
from invoice_extractor.config import Config
from invoice_extractor.events import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_STARTED,
    OnEvent,
    ProgressEvent,
    emit,
)
from invoice_extractor.excel_export import export_workbook
from invoice_extractor.logging_setup import new_run_id
from invoice_extractor.pipeline import (
    BatchInterrupted,
    InvoiceResult,
    find_pdfs,
    process_directory,
)
from invoice_extractor.usage import usage_csv_path, write_usage_csv


class OutputWriteError(Exception):
    """Raised when the staged artifact set could not be written. Existing
    final artifacts are guaranteed untouched (see atomic.StagedArtifacts);
    the message is safe (exception summary only, no content)."""


@dataclass
class RunOutcome:
    """Everything a presentation layer needs after one extraction run."""

    run_id: str
    results: list[InvoiceResult] = field(default_factory=list)
    usage_records: list = field(default_factory=list)
    interrupted: bool = False
    artifacts: list[Path] = field(default_factory=list)  # finals actually written
    started_at: str = ""
    finished_at: str = ""

    @property
    def wrote_output(self) -> bool:
        return bool(self.artifacts)

    def summary_counts(self) -> dict:
        """Safe aggregate counts (no invoice values) for summaries/status."""
        results = self.results
        by_method: dict[str, int] = {}
        by_class: dict[str, int] = {}
        for r in results:
            by_method[r.extraction_method] = by_method.get(r.extraction_method, 0) + 1
            by_class[r.document_classification] = by_class.get(r.document_classification, 0) + 1
        cost = sum((u.cost_usd or Decimal("0") for u in self.usage_records), Decimal("0"))
        return {
            "files_processed": len(results),
            "extracted": sum(1 for r in results
                             if r.extraction_method in ("text", "vision", "mixed")),
            "needs_review": sum(1 for r in results if r.needs_review),
            "failed": sum(1 for r in results if r.error),
            "by_method": dict(sorted(by_method.items())),
            "by_classification": dict(sorted(by_class.items())),
            "requests": len(self.usage_records),
            "repairs": sum(1 for u in self.usage_records if u.attempt_type == "repair"),
            "escalations": sum(1 for u in self.usage_records
                               if u.attempt_type == "escalation"),
            "reported_cost": str(cost),
            "unknown_cost_requests": sum(1 for u in self.usage_records
                                         if u.cost_usd is None),
            "elapsed_seconds": round(sum(r.elapsed_seconds for r in results), 3),
            "interrupted": self.interrupted,
        }


def safe_run_metadata_row(r: InvoiceResult) -> dict:
    """One run-metadata file row: SAFE fields only - never review reasons,
    invoice values, text, prompts, responses, stack traces, or image data."""
    reqs = sum(1 for _ in r.usage_records)
    unknown = sum(1 for u in r.usage_records if u.cost_usd is None)
    cost = sum((u.cost_usd or Decimal("0") for u in r.usage_records), Decimal("0"))
    interrupted = "interrupted by operator" in (r.review_reason or "")
    return {
        "source_file": r.source_file,
        "elapsed_seconds": round(r.elapsed_seconds, 3),
        "extraction_method": r.extraction_method,
        "provider": r.provider, "model": r.model,
        "needs_review": r.needs_review, "error": r.error,
        "completed": not r.error and not interrupted,
        "interrupted": interrupted,
        "request_count": reqs, "reported_cost": str(cost),
        "unknown_cost_count": unknown,
    }


def build_run_metadata(outcome: RunOutcome, input_dir: Path,
                       planned_names: list[str]) -> dict:
    """The opt-in run-metadata document (fixed safe schema, M7 contract)."""
    return {
        "run_id": outcome.run_id,
        "started_at": outcome.started_at, "finished_at": outcome.finished_at,
        "interrupted": outcome.interrupted,
        "exit_code": 130 if outcome.interrupted else 0,
        "input_dir": str(input_dir),
        "output_artifacts": sorted(planned_names),
        "files": [safe_run_metadata_row(r) for r in outcome.results],
    }


def run_extraction(
    input_dir: Path, output_path: Path, cfg: Config, logger: logging.Logger, *,
    run_id: str | None = None, run_metadata_path: Path | None = None,
    on_event: OnEvent = None,
) -> RunOutcome:
    """Run the full extraction batch and write the artifact set atomically.

    Behavior contract (identical to the pre-M9 CLI internals):
      - operator interruption (Ctrl+C/SIGINT -> BatchInterrupted) stops new
        provider calls; with >=1 recorded file a valid PARTIAL artifact set is
        written; with 0 files NOTHING is written; outcome.interrupted is True;
      - all artifacts (workbook + usage CSV under OpenRouter + optional run
        metadata) are staged fully before any existing final is replaced;
      - a staging failure raises OutputWriteError and leaves existing outputs
        untouched;
      - unexpected pipeline errors propagate to the caller unchanged.

    Callers do preflight (collisions/writability) BEFORE calling this.
    """
    run_id = run_id or new_run_id()
    input_dir = Path(input_dir)
    output_path = Path(output_path)

    pdf_count = len(find_pdfs(input_dir))
    emit(on_event, ProgressEvent(event=JOB_STARTED, file_total=pdf_count))

    started_at = datetime.now(timezone.utc).isoformat()
    interrupted = False
    try:
        results = process_directory(input_dir, cfg, logger, run_id=run_id,
                                    on_event=on_event)
    except BatchInterrupted as bi:
        results = bi.results
        interrupted = True
    finished_at = datetime.now(timezone.utc).isoformat()

    outcome = RunOutcome(
        run_id=run_id, results=results,
        usage_records=[u for r in results for u in r.usage_records],
        interrupted=interrupted, started_at=started_at, finished_at=finished_at,
    )

    # Interrupted with zero recorded files: per policy, write no output.
    if interrupted and not results:
        emit(on_event, ProgressEvent(event=JOB_CANCELLED, files_processed=0,
                                     interrupted=True))
        return outcome

    usage_path = usage_csv_path(output_path) if cfg.llm_gateway == "openrouter" else None
    planned = [output_path] + ([usage_path] if usage_path else []) \
        + ([run_metadata_path] if run_metadata_path else [])

    def _write_metadata(dst: Path):
        meta = build_run_metadata(outcome, input_dir, [p.name for p in planned])
        dst.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    from invoice_extractor.logging_setup import exc_summary
    try:
        with StagedArtifacts() as stage:
            stage.stage(output_path, lambda p: export_workbook(results, p))
            if usage_path is not None:
                stage.stage(usage_path,
                            lambda p: write_usage_csv(outcome.usage_records, p))
            if run_metadata_path is not None:
                stage.stage(Path(run_metadata_path), _write_metadata)
            stage.commit()
    except Exception as exc:
        logger.error("outputs could not be written: %s", exc_summary(exc))
        raise OutputWriteError(exc_summary(exc)) from exc

    outcome.artifacts = list(planned)
    counts = outcome.summary_counts()
    emit(on_event, ProgressEvent(
        event=JOB_CANCELLED if interrupted else JOB_COMPLETED,
        files_processed=counts["files_processed"],
        request_count=counts["requests"], repair_count=counts["repairs"],
        escalation_count=counts["escalations"],
        reported_cost=counts["reported_cost"],
        unknown_cost_count=counts["unknown_cost_requests"],
        elapsed_seconds=counts["elapsed_seconds"], interrupted=interrupted))
    return outcome
