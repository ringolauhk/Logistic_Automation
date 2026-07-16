"""Extraction worker subprocess (M9).

Spawned by the Streamlit app in its own session/process group:

    python -m apps.web.worker

All context arrives through the (controlled, per-job) ENVIRONMENT - never
argv: WEB_JOB_ID, WEB_JOB_DIR, WEB_WORKER_TOKEN, WEB_JOB_ENABLE_LOG,
WEB_JOB_ENABLE_METADATA, plus any per-job numeric overrides such as
MAX_TEXT_PAGES. API keys are inherited exactly like the CLI - never printed,
never written to status/events.

The worker calls the SAME shared service as the CLI (run_extraction), writes
structured events to events.jsonl and atomic status.json updates, heartbeats
the global lock, and relies on the engine's proven SIGINT -> exit-130
partial-output behavior for cancellation. status.json never contains
exception text, review reasons, prompts, provider bodies, extracted values,
or secrets - failures surface as a fixed safe error_category.
"""

import os
import sys
import threading
from pathlib import Path

# Exit codes mirror the CLI contract.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130


def _flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def main() -> int:
    from apps.web import job_manager
    from apps.web.progress import (
        STATE_CANCELLED,
        STATE_COMPLETED,
        STATE_FAILED,
        STATE_NEEDS_REVIEW,
        STATE_RUNNING,
        EventWriter,
        build_status,
        read_status,
        write_status,
    )
    from invoice_extractor.config import load_config
    from invoice_extractor.logging_setup import exc_summary, setup_logging
    from invoice_extractor.pipeline import safe_review_categories
    from invoice_extractor.service import OutputWriteError, run_extraction

    job_id = os.environ.get("WEB_JOB_ID", "")
    token = os.environ.get("WEB_WORKER_TOKEN", "")
    try:
        job_dir = job_manager.job_dir_for(job_id)
    except job_manager.JobError:
        print("worker: invalid job id", file=sys.stderr)
        return EXIT_FAILED

    prior = read_status(job_dir) or {}
    created_at = prior.get("created_at")
    started_at = job_manager.utc_now()

    def set_status(state, *, exit_code=None, summary=None, files=None,
                   artifacts=None, error_category=None, finished=False):
        write_status(job_dir, build_status(
            job_id, state, created_at=created_at, started_at=started_at,
            finished_at=job_manager.utc_now() if finished else None,
            exit_code=exit_code, summary=summary, files=files,
            artifacts=artifacts, error_category=error_category))

    # Heartbeat: refresh the lock periodically so a live worker is never
    # treated as stale. Daemon thread; stops with the process.
    stop_beat = threading.Event()

    def _beat():
        while not stop_beat.wait(15):
            try:
                job_manager.heartbeat(job_id, token)
            except Exception:  # heartbeat must never kill the run
                pass

    threading.Thread(target=_beat, daemon=True).start()

    events = EventWriter(job_dir / "events.jsonl", job_id)
    log_path = (job_dir / "logs" / "run.log") if _flag("WEB_JOB_ENABLE_LOG") else None
    metadata_path = (job_dir / "output" / "results.run.json"
                     if _flag("WEB_JOB_ENABLE_METADATA") else None)

    try:
        try:
            cfg = load_config()
        except Exception as exc:
            set_status(STATE_FAILED, exit_code=EXIT_FAILED,
                       error_category="configuration", finished=True)
            print(f"worker: configuration error: {exc_summary(exc)}",
                  file=sys.stderr)
            return EXIT_FAILED

        logger = setup_logging(
            log_path, run_id=job_id[-12:],
            secrets=(cfg.gemini_api_key or "", cfg.anthropic_api_key or "",
                     cfg.openrouter_api_key or ""),
        )
        set_status(STATE_RUNNING)

        try:
            outcome = run_extraction(
                job_dir / "input", job_dir / "output" / "results.xlsx",
                cfg, logger, run_id=job_id[-12:],
                run_metadata_path=metadata_path, on_event=events,
            )
        except OutputWriteError:
            set_status(STATE_FAILED, exit_code=EXIT_FAILED,
                       error_category="output_write", finished=True)
            return EXIT_FAILED
        except Exception as exc:
            logger.error("worker: batch did not complete: %s", exc_summary(exc))
            set_status(STATE_FAILED, exit_code=EXIT_FAILED,
                       error_category="unexpected", finished=True)
            return EXIT_FAILED

        file_rows = [{
            "source_file": r.source_file,
            "extraction_method": r.extraction_method,
            "provider": r.provider, "model": r.model,
            "needs_review": r.needs_review, "error": r.error,
            "review_categories": list(safe_review_categories(r)),
        } for r in outcome.results]
        artifacts = [p.name for p in outcome.artifacts]

        if outcome.interrupted:
            set_status(STATE_CANCELLED, exit_code=EXIT_INTERRUPTED,
                       summary=outcome.summary_counts(), files=file_rows,
                       artifacts=artifacts, finished=True)
            return EXIT_INTERRUPTED

        any_review = any(r.needs_review or r.error for r in outcome.results)
        set_status(STATE_NEEDS_REVIEW if any_review else STATE_COMPLETED,
                   exit_code=EXIT_OK, summary=outcome.summary_counts(),
                   files=file_rows, artifacts=artifacts, finished=True)
        return EXIT_OK
    finally:
        stop_beat.set()
        events.close()
        job_manager.release_lock(job_id, token)


if __name__ == "__main__":
    sys.exit(main())
