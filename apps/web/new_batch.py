"""Start-New-Batch workflow (M9.1): terminal-state detection, session reset,
and the uploader-generation mechanism that visually clears Streamlit's
file_uploader (its state persists until its widget key changes).

Session state is treated as a plain MutableMapping so everything here is
testable offline without a Streamlit runtime. Resetting NEVER spawns a
worker and NEVER touches a running job - deletion goes through
cleanup.delete_job, which refuses the active job, foreign ids, and symlinks.
"""

from typing import Callable, MutableMapping

from apps.web import cleanup
from apps.web.progress import TERMINAL_STATES

UPLOADER_GEN_KEY = "uploader_gen"
# Session keys that reference the previous job's uploads, classification,
# estimate inputs, progress, and results. Dropping them (and detaching
# job_id) clears every derived display: plans drive the classification
# table + attempt estimate, job_id drives progress/summary/downloads.
SESSION_KEYS_TO_CLEAR = ("plans",)

DELETED_MSG = "Previous job files deleted. Ready for a new batch."
KEPT_MSG = ("Previous job files kept; they will be removed by normal "
            "retention cleanup. Ready for a new batch.")
DELETE_REFUSED_MSG = ("Previous job files could not be deleted right now; "
                      "they will be removed by retention cleanup. Ready for "
                      "a new batch.")


def can_start_new_batch(state: str | None, active) -> bool:
    """Start New Batch is offered only for a terminal job with no extraction
    running anywhere (an active lock always wins, even mid-cancellation:
    the worker must exit and release the lock first)."""
    return active is None and state in TERMINAL_STATES


def uploader_key(session: MutableMapping) -> str:
    """Widget key for st.file_uploader. Changing the generation makes
    Streamlit build a fresh uploader, so previously selected files
    disappear visually (assigning None to the value does NOT do that)."""
    return f"uploader-{session.get(UPLOADER_GEN_KEY, 0)}"


def start_new_batch(session: MutableMapping, job_id: str | None, *,
                    delete_files: bool,
                    delete_job: Callable[[str], bool] = cleanup.delete_job,
                    ) -> str:
    """Reset the session to the initial upload state. Returns a safe
    confirmation message (no paths, ids kept out of the UI text).

    - optionally deletes the finished job via the existing safe deleter;
    - detaches the session from the job (job_id key kept, set to None, so
      the page does NOT re-adopt the newest terminal job on rerun);
    - clears classification/estimate/progress/summary/download state;
    - bumps the uploader generation to visually reset the file picker;
    - never starts a worker.
    """
    message = KEPT_MSG
    if delete_files and job_id:
        message = DELETED_MSG if delete_job(job_id) else DELETE_REFUSED_MSG
    for key in SESSION_KEYS_TO_CLEAR:
        session.pop(key, None)
    session["job_id"] = None
    session[UPLOADER_GEN_KEY] = session.get(UPLOADER_GEN_KEY, 0) + 1
    return message
