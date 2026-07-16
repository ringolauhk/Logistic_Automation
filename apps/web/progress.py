"""Progress/status file protocol between the worker subprocess and the UI (M9).

events.jsonl - append-only, one COMPLETE JSON object per line, flushed after
every write. Envelope (versioned):

  {"schema_version": 1, "seq": <monotonic int>, "ts": "<UTC ISO>",
   "job_id": "...", "event": "...", ...approved safe fields...}

The reader tolerates a partial final line (a write in progress), skips and
COUNTS malformed non-final lines, and ignores duplicate sequence numbers.

status.json - small fixed schema, replaced atomically (same-directory temp +
os.replace). Never contains exception text, review reasons, prompts,
extracted values, provider responses, or secrets - only the fields written
by build_status()/write_status() below.
"""

import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from invoice_extractor.events import ProgressEvent

SCHEMA_VERSION = 1

# Job state machine (adjustment 6):
#   created -> prepared -> running -> completed|needs_review|failed|cancelled
#   -> (expired/deleted: the job directory is removed by cleanup)
STATE_CREATED = "created"
STATE_PREPARED = "prepared"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_NEEDS_REVIEW = "needs_review"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
TERMINAL_STATES = (STATE_COMPLETED, STATE_NEEDS_REVIEW, STATE_FAILED, STATE_CANCELLED)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventWriter:
    """Appends envelope-wrapped ProgressEvents to events.jsonl (worker side)."""

    def __init__(self, path: Path, job_id: str):
        self.path = Path(path)
        self.job_id = job_id
        self._seq = 0
        self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - long-lived

    def __call__(self, event: ProgressEvent) -> None:
        """Usable directly as the engine's on_event callback."""
        self._seq += 1
        payload = {"schema_version": SCHEMA_VERSION, "seq": self._seq,
                   "ts": _utc_now(), "job_id": self.job_id}
        for key, value in dataclasses.asdict(event).items():
            if value is not None:
                payload[key] = list(value) if isinstance(value, tuple) else value
        self._fh.write(json.dumps(payload) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def read_events(path: Path) -> tuple[list[dict], int]:
    """Read all valid events from events.jsonl (UI side).

    Returns (events ordered by seq, malformed_line_count). A malformed FINAL
    line is treated as an in-progress partial write and not counted; any
    other malformed line is skipped and counted. Duplicate seq values keep
    the first occurrence only.
    """
    path = Path(path)
    if not path.exists():
        return [], 0
    try:
        raw_lines = path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return [], 0
    events: dict[int, dict] = {}
    malformed = 0
    lines = [ln for ln in raw_lines if ln.strip()]
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
            seq = obj["seq"]
            if not isinstance(seq, int):
                raise ValueError("bad seq")
        except (ValueError, KeyError, TypeError):
            if i == len(lines) - 1:
                continue  # partial final line - a write may be in progress
            malformed += 1
            continue
        if seq not in events:  # ignore duplicate sequence numbers
            events[seq] = obj
    return [events[k] for k in sorted(events)], malformed


def build_status(job_id: str, state: str, *, created_at: str | None = None,
                 started_at: str | None = None, finished_at: str | None = None,
                 exit_code: int | None = None, summary: dict | None = None,
                 files: list[dict] | None = None, artifacts: list[str] | None = None,
                 error_category: str | None = None) -> dict:
    """The fixed, safe status.json schema. `files` rows may carry only the
    safe per-file fields (source_file/display name, method, provider, model,
    needs_review, error, review CATEGORIES) - callers must not add others."""
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "state": state,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "summary": summary or {},
        "files": files or [],
        "artifacts": sorted(artifacts or []),   # basenames only, never paths
        "error_category": error_category,
        "updated_at": _utc_now(),
    }


def write_status(job_dir: Path, status: dict) -> None:
    """Atomically replace status.json (same-directory temp + os.replace)."""
    job_dir = Path(job_dir)
    final = job_dir / "status.json"
    tmp = job_dir / f"status.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, final)


def read_status(job_dir: Path) -> dict | None:
    path = Path(job_dir) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
