"""Job lifecycle, upload validation, the single-active-job lock, and the
worker subprocess (M9).

Security/identity model (approved adjustments 3/4/6):

  * every job lives under WEB_JOBS_DIR in an app-generated directory
    (job-<UTCstamp>-<12 hex>); user input NEVER contributes a path segment;
  * ONE global lock (jobs_root/active.lock) created O_CREAT|O_EXCL with
    owner-only permissions, containing schema_version/job_id/pid/
    worker_token/started_at/heartbeat_at; updated atomically;
  * cancellation fails CLOSED: SIGINT is sent to the worker's own process
    group only after the lock's job_id + token match the requesting job AND
    the PID is alive AND it leads its own process group AND its command line
    looks like our worker - a reused PID can never be signalled;
  * stale locks (dead PID, or heartbeat older than the stale window with a
    non-matching process) are reclaimed;
  * uploads are validated (extension, %PDF signature, size, count, dupes)
    and stored under <job>/input/ with sanitized basenames verified to
    resolve inside the job directory;
  * API keys and provider settings are NEVER passed via argv - the worker
    inherits a per-job environment COPY (the Streamlit server's own
    os.environ is never mutated).
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apps.web.progress import (
    STATE_CREATED,
    STATE_PREPARED,
    build_status,
    write_status,
)

SCHEMA_VERSION = 1
JOB_ID_RE = re.compile(r"^job-\d{8}T\d{6}-[0-9a-f]{12}$")
LOCK_NAME = "active.lock"
HEARTBEAT_STALE_SECONDS = 120  # no heartbeat for this long => candidate stale

# Fixed download allowlist: the ONLY files the UI may ever serve, all under
# <job>/output/ (plus the opt-in log under <job>/logs/). Uploaded PDFs are
# deliberately absent - source invoices are never downloadable.
ARTIFACT_ALLOWLIST = {
    "results.xlsx": ("output", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "results.usage.csv": ("output", "text/csv"),
    "results.run.json": ("output", "application/json"),
    "run.log": ("logs", "text/plain"),
}


class JobError(Exception):
    """Operator-facing job/upload problem. Message is safe to display."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jobs_root() -> Path:
    return Path(os.environ.get("WEB_JOBS_DIR", "./web-data/jobs")).resolve()


def upload_limits() -> dict:
    def _int(name, default):
        try:
            return max(1, int(os.environ.get(name, default)))
        except ValueError:
            return default
    return {
        "max_files": _int("WEB_MAX_FILES", 25),
        "max_file_mb": _int("WEB_MAX_FILE_MB", 25),
        "max_total_mb": _int("WEB_MAX_TOTAL_MB", 200),
    }


# --- Upload validation ----------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    """Basename-only, NFKC-normalized, safe-charset filename ending in .pdf.
    Path traversal is impossible: separators are stripped with everything
    else outside the safe charset, and callers re-verify containment."""
    base = unicodedata.normalize("NFKC", name).replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._") or "upload"
    return f"{stem[:80]}.pdf" if ext.lower() == "pdf" else f"{stem[:80]}_{ext}.pdf"


@dataclass
class ValidatedUpload:
    display_name: str   # sanitized name shown in the UI and used on disk
    data: bytes


def validate_uploads(files: list[tuple[str, bytes]]) -> list[ValidatedUpload]:
    """Validate (filename, content) pairs against the pilot limits.

    Raises JobError with a safe, actionable message on the first violation.
    Nothing is silently truncated or dropped.
    """
    limits = upload_limits()
    if not files:
        raise JobError("No files uploaded - add at least one PDF.")
    if len(files) > limits["max_files"]:
        raise JobError(f"Too many files: {len(files)} uploaded, "
                       f"limit is {limits['max_files']}.")
    max_file = limits["max_file_mb"] * 1024 * 1024
    max_total = limits["max_total_mb"] * 1024 * 1024
    total = 0
    seen: set[str] = set()
    validated: list[ValidatedUpload] = []
    for raw_name, data in files:
        name = sanitize_filename(raw_name)
        if not raw_name.lower().endswith(".pdf"):
            raise JobError(f"'{name}': only PDF files are accepted.")
        if not data:
            raise JobError(f"'{name}' is empty.")
        if not data.startswith(b"%PDF-"):
            raise JobError(f"'{name}' does not look like a PDF "
                           "(missing %PDF signature).")
        if len(data) > max_file:
            raise JobError(f"'{name}' is {len(data) / 1e6:.1f} MB; the per-file "
                           f"limit is {limits['max_file_mb']} MB.")
        total += len(data)
        if total > max_total:
            raise JobError(f"Combined upload exceeds the "
                           f"{limits['max_total_mb']} MB total limit.")
        if name in seen:
            raise JobError(f"Duplicate file name after sanitizing: '{name}'. "
                           "Rename one copy and re-upload.")
        seen.add(name)
        validated.append(ValidatedUpload(display_name=name, data=data))
    return validated


# --- Job directories --------------------------------------------------------------

def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"job-{stamp}-{secrets.token_hex(6)}"


def job_dir_for(job_id: str) -> Path:
    """Resolve a job directory from an app-format job id ONLY - rejects
    anything that isn't our own id shape, so no user-controlled path can
    ever be addressed."""
    if not JOB_ID_RE.match(job_id or ""):
        raise JobError("Unknown job id.")
    path = (jobs_root() / job_id).resolve()
    if path.parent != jobs_root():
        raise JobError("Unknown job id.")
    return path


def create_job(uploads: list[ValidatedUpload]) -> str:
    """Create a job directory, store validated uploads, mark it 'created'."""
    root = jobs_root()
    root.mkdir(parents=True, exist_ok=True)
    job_id = new_job_id()
    job_dir = root / job_id
    for sub in ("input", "output", "logs"):
        (job_dir / sub).mkdir(parents=True)
    input_dir = (job_dir / "input").resolve()
    for up in uploads:
        dest = (input_dir / up.display_name).resolve()
        if dest.parent != input_dir:  # defense in depth after sanitizing
            raise JobError("Unknown job id.")
        dest.write_bytes(up.data)
    write_status(job_dir, build_status(job_id, STATE_CREATED, created_at=utc_now()))
    return job_id


def mark_prepared(job_id: str, *, file_rows: list[dict]) -> None:
    """created -> prepared: uploads saved + classified, ready to start. No
    provider calls have been made; only 'running' owns the extraction lock."""
    job_dir = job_dir_for(job_id)
    from apps.web.progress import read_status
    prior = read_status(job_dir) or {}
    write_status(job_dir, build_status(
        job_id, STATE_PREPARED, created_at=prior.get("created_at") or utc_now(),
        files=file_rows))


# --- Single-active-job lock ---------------------------------------------------------

@dataclass
class LockInfo:
    job_id: str
    pid: int | None
    worker_token: str
    started_at: str
    heartbeat_at: str
    raw: dict = field(default_factory=dict)


def lock_path() -> Path:
    return jobs_root() / LOCK_NAME


def acquire_lock(job_id: str) -> str:
    """Create the global lock (O_CREAT|O_EXCL, owner-only permissions).
    Returns the random worker token recorded in it. Raises JobError if a
    live job already holds the lock; reclaims a stale one first."""
    root = jobs_root()
    root.mkdir(parents=True, exist_ok=True)
    reclaim_stale_lock()
    token = secrets.token_hex(16)
    payload = {
        "schema_version": SCHEMA_VERSION, "job_id": job_id, "pid": None,
        "worker_token": token, "started_at": utc_now(), "heartbeat_at": utc_now(),
    }
    try:
        fd = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise JobError("Another extraction is currently running.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return token


def read_lock() -> LockInfo | None:
    path = lock_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(job_id=raw.get("job_id", ""), pid=raw.get("pid"),
                        worker_token=raw.get("worker_token", ""),
                        started_at=raw.get("started_at", ""),
                        heartbeat_at=raw.get("heartbeat_at", ""), raw=raw)
    except (OSError, ValueError):
        # Unreadable/corrupt lock: treat as held (fail closed) until stale
        # recovery decides otherwise.
        return LockInfo(job_id="", pid=None, worker_token="", started_at="",
                        heartbeat_at="", raw={})


def update_lock(job_id: str, token: str, **fields) -> None:
    """Atomically update our OWN lock (verified by job_id+token)."""
    info = read_lock()
    if info is None or info.job_id != job_id or info.worker_token != token:
        return  # not ours (anymore) - never touch someone else's lock
    payload = dict(info.raw)
    payload.update(fields)
    tmp = lock_path().with_name(f"{LOCK_NAME}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, lock_path())


def heartbeat(job_id: str, token: str) -> None:
    update_lock(job_id, token, heartbeat_at=utc_now())


def release_lock(job_id: str, token: str) -> None:
    """Remove the lock only if it is OURS (job_id + token match)."""
    info = read_lock()
    if info is not None and info.job_id == job_id and info.worker_token == token:
        try:
            lock_path().unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours - definitely not our worker


def _looks_like_our_worker(pid: int) -> bool:
    """Verify PID identity before ever signalling it (adjustment 3):
    it must lead its own process group (we spawn with start_new_session) and
    its command line must reference our worker module. Fails CLOSED on any
    doubt. Uses /proc on Linux and `ps` on macOS."""
    try:
        if os.getpgid(pid) != pid:
            return False
    except OSError:
        return False
    cmdline = ""
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():  # Linux
        try:
            cmdline = proc_path.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace")
        except OSError:
            return False
    else:  # macOS and other BSDs
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)
            cmdline = out.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    return "apps.web.worker" in cmdline


def _heartbeat_expired(heartbeat_at: str) -> bool:
    try:
        beat = datetime.fromisoformat(heartbeat_at)
    except (ValueError, TypeError):
        return True
    age = (datetime.now(timezone.utc) - beat).total_seconds()
    return age > HEARTBEAT_STALE_SECONDS


def lock_is_stale(info: LockInfo) -> bool:
    """Stale = dead PID, or expired heartbeat AND the recorded PID is not
    verifiably our worker anymore. A live, verified worker is never stale."""
    if info.pid is None:
        # Spawn-phase lock with no PID yet: stale only when its heartbeat
        # (set at acquisition) has expired - covers a UI that crashed
        # between acquire and spawn.
        return _heartbeat_expired(info.heartbeat_at)
    if not _pid_alive(info.pid):
        return True
    if _heartbeat_expired(info.heartbeat_at) and not _looks_like_our_worker(info.pid):
        return True
    return False


def reclaim_stale_lock() -> bool:
    info = read_lock()
    if info is None:
        return False
    if lock_is_stale(info):
        try:
            lock_path().unlink()
        except OSError:
            pass
        return True
    return False


# --- Worker subprocess ---------------------------------------------------------------

def spawn_worker(job_id: str, token: str, *, settings_env: dict[str, str],
                 enable_log: bool, enable_metadata: bool) -> int:
    """Start the extraction worker in its OWN session/process group.

    The child inherits a CONTROLLED COPY of the current environment plus the
    per-job overrides - the Streamlit server's os.environ is never mutated
    and nothing secret goes through argv. stdout/stderr go to an INTERNAL
    console file (never in the download allowlist, never a PIPE - so no
    unread-pipe deadlock and no raw stderr in the browser).

    On spawn failure the lock is released (adjustment 3)."""
    job_dir = job_dir_for(job_id)
    env = dict(os.environ)
    env.update(settings_env)
    env["WEB_JOB_ID"] = job_id
    env["WEB_JOB_DIR"] = str(job_dir)
    env["WEB_WORKER_TOKEN"] = token
    env["WEB_JOB_ENABLE_LOG"] = "1" if enable_log else "0"
    env["WEB_JOB_ENABLE_METADATA"] = "1" if enable_metadata else "0"
    console = open(job_dir / "logs" / "worker-console.log", "a",  # noqa: SIM115
                   encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "apps.web.worker"],
            cwd=str(_repo_root()), env=env,
            stdout=console, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # own PGID => group-targeted SIGINT
        )
    except Exception:
        console.close()
        release_lock(job_id, token)
        raise
    console.close()  # child holds its own descriptor now
    update_lock(job_id, token, pid=proc.pid)
    return proc.pid


def _repo_root() -> Path:
    """Directory containing the `apps` package (works in the container at
    /app and in a source checkout)."""
    return Path(__file__).resolve().parent.parent.parent


def cancel_job(job_id: str, token: str) -> bool:
    """Send SIGINT to the VALIDATED worker process group. Fail closed: no
    signal unless the lock is ours (job_id + token), the PID is alive, and
    the process is verifiably our worker leading its own group."""
    info = read_lock()
    if (info is None or info.job_id != job_id
            or info.worker_token != token or not info.pid):
        return False
    if not _pid_alive(info.pid) or not _looks_like_our_worker(info.pid):
        return False
    try:
        os.killpg(info.pid, signal.SIGINT)
        return True
    except OSError:
        return False


def active_job() -> LockInfo | None:
    """The currently running job, if a live lock exists (used by every
    browser session/refresh to rediscover state - never session-local)."""
    reclaim_stale_lock()
    return read_lock()
