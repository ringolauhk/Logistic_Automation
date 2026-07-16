"""Retention cleanup for web jobs (M9).

Deletes expired job directories (uploads AND outputs) after
WEB_JOB_RETENTION_HOURS (default 24). Runs at app startup, before each new
job, and via the manual "Delete job files now" button.

Safety rules:
  * only directories DIRECTLY under the jobs root whose name matches the
    app's own job-ID format are ever considered;
  * symlinks are never followed or deleted-through;
  * the active (locked) job is never removed;
  * prepared-but-abandoned jobs expire like any other;
  * failures are logged as safe counts and never crash the app.
"""

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from apps.web import job_manager
from apps.web.progress import read_status

logger = logging.getLogger("invoice_extractor.web")

import os


def retention_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("WEB_JOB_RETENTION_HOURS", "24")))
    except ValueError:
        return 24.0


def _job_age_hours(job_dir: Path) -> float:
    status = read_status(job_dir) or {}
    stamp = status.get("finished_at") or status.get("created_at")
    if stamp:
        try:
            created = datetime.fromisoformat(stamp)
            return (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
        except (ValueError, TypeError):
            pass
    try:  # fallback: directory mtime
        mtime = datetime.fromtimestamp(job_dir.stat().st_mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0
    except OSError:
        return 0.0


def delete_job(job_id: str) -> bool:
    """Delete ONE job directory (manual button / expiry). Refuses the active
    job and anything that is not a real directory matching our id format."""
    try:
        job_dir = job_manager.job_dir_for(job_id)
    except job_manager.JobError:
        return False
    active = job_manager.read_lock()
    if active is not None and active.job_id == job_id:
        return False  # never delete the active job
    if job_dir.is_symlink() or not job_dir.is_dir():
        return False
    try:
        shutil.rmtree(job_dir)
        return True
    except OSError as exc:
        logger.warning("cleanup: could not delete one job dir (%s)",
                       type(exc).__name__)
        return False


def cleanup_expired() -> dict:
    """Delete every expired job. Returns safe counts only."""
    root = job_manager.jobs_root()
    removed = kept = errors = ignored = 0
    if not root.is_dir():
        return {"removed": 0, "kept": 0, "errors": 0, "ignored": 0}
    limit = retention_hours()
    active = job_manager.read_lock()
    active_id = active.job_id if active else None
    for entry in sorted(root.iterdir()):
        if not job_manager.JOB_ID_RE.match(entry.name):
            ignored += 1          # unrelated file/dir (incl. the lock file)
            continue
        if entry.is_symlink() or not entry.is_dir():
            ignored += 1          # never follow symlinks
            continue
        if entry.name == active_id:
            kept += 1             # never delete the active job
            continue
        if _job_age_hours(entry) < limit:
            kept += 1
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError as exc:
            errors += 1
            logger.warning("cleanup: could not delete one expired job (%s)",
                           type(exc).__name__)
    if removed or errors:
        logger.info("web cleanup: removed=%d kept=%d errors=%d ignored=%d",
                    removed, kept, errors, ignored)
    return {"removed": removed, "kept": kept, "errors": errors, "ignored": ignored}
