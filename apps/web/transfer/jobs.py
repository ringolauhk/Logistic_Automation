"""Transfer Packing job storage and validation (Build 1).

Isolation from the invoice workflow is structural, not conventional:

  * transfer jobs live under their OWN root (TRANSFER_JOBS_DIR, default
    ./web-data/transfer-jobs) - the invoice recovery scan and retention
    cleanup never see them;
  * transfer job ids use a distinct shape (tjob-...) that the invoice
    loader's regex rejects, and vice versa;
  * metadata lives in transfer_job.json with an explicit job_type field.

No OCR, AI, API-gateway, or Excel logic exists here - validation uses only
deterministic PyMuPDF inspection (page count / readability).
"""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF - already a core project dependency

from apps.web.job_manager import JobError, sanitize_filename, utc_now
from apps.web.transfer.models import (
    DUPLICATE_FILE,
    EMPTY_FILE,
    FILE_INVALID,
    FILE_TOO_LARGE,
    FILE_VALIDATED,
    INVALID_PDF,
    JOB_READY_FOR_EXTRACTION,
    NO_FILES,
    TOO_MANY_FILES,
    TOO_MANY_PAGES,
    UNSUPPORTED_FILE_TYPE,
    TransferPackingJob,
    TransferUploadFile,
    ValidationIssue,
)

TJOB_ID_RE = re.compile(r"^tjob-\d{8}T\d{6}-[0-9a-f]{12}$")
METADATA_NAME = "transfer_job.json"


# --- configuration ----------------------------------------------------------------

def workflow_enabled() -> bool:
    """TRANSFER_WORKFLOW_ENABLED feature flag (default OFF)."""
    from invoice_extractor.config import _env_bool   # established bool helper
    return _env_bool("TRANSFER_WORKFLOW_ENABLED", False)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def transfer_limits() -> dict:
    return {
        "max_files": _env_int("TRANSFER_MAX_FILES", 50),
        "max_file_mb": _env_int("TRANSFER_MAX_FILE_MB", 50),
        "max_pages": _env_int("TRANSFER_MAX_PAGES", 500),
    }


def transfer_jobs_root() -> Path:
    return Path(os.environ.get("TRANSFER_JOBS_DIR",
                               "./web-data/transfer-jobs")).resolve()


# --- validation -------------------------------------------------------------------

def _page_count(data: bytes) -> int:
    """Deterministic page count via PyMuPDF; raises on unreadable PDFs.
    No OCR, no rendering, no provider calls."""
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("password-protected")
        return doc.page_count


def validate_transfer_uploads(
        files: list[tuple[str, bytes]],
) -> tuple[list[TransferUploadFile], list[ValidationIssue]]:
    """Validate (original_name, content) pairs in upload order.

    Collects ALL problems (nothing fails fast, nothing is silently
    skipped): every file gets a VALIDATED/INVALID status plus per-file
    messages, and batch-level issues carry sequence=None. A job may be
    created only when the issue list is empty.
    """
    issues: list[ValidationIssue] = []
    limits = transfer_limits()
    if not files:
        return [], [ValidationIssue(NO_FILES,
                                    "No files selected - add at least one "
                                    "Transfer Delivery Note PDF.")]
    if len(files) > limits["max_files"]:
        issues.append(ValidationIssue(
            TOO_MANY_FILES,
            f"{len(files)} files selected; the limit is "
            f"{limits['max_files']} per job."))

    max_file_bytes = limits["max_file_mb"] * 1024 * 1024
    seen_checksums: dict[str, int] = {}
    results: list[TransferUploadFile] = []
    total_pages = 0

    for index, (original_name, data) in enumerate(files, start=1):
        safe = sanitize_filename(original_name)
        upload = TransferUploadFile(
            sequence=index,
            original_name=original_name,
            stored_name=f"{index:03d}-{safe}",
            size_bytes=len(data),
        )

        def problem(code: str, message: str) -> None:
            issues.append(ValidationIssue(code, message, sequence=index))
            upload.status = FILE_INVALID
            upload.messages.append(message)

        if not original_name.lower().endswith(".pdf"):
            problem(UNSUPPORTED_FILE_TYPE,
                    f"File {index} ('{safe}'): only PDF files are accepted.")
        elif not data:
            problem(EMPTY_FILE, f"File {index} ('{safe}') is empty.")
        elif not data.startswith(b"%PDF-"):
            problem(UNSUPPORTED_FILE_TYPE,
                    f"File {index} ('{safe}') is not a PDF (content does "
                    "not match the PDF format).")
        else:
            if len(data) > max_file_bytes:
                problem(FILE_TOO_LARGE,
                        f"File {index} ('{safe}') is {len(data) / 1e6:.1f} MB;"
                        f" the per-file limit is {limits['max_file_mb']} MB.")
            upload.sha256 = hashlib.sha256(data).hexdigest()
            first = seen_checksums.setdefault(upload.sha256, index)
            if first != index:
                problem(DUPLICATE_FILE,
                        f"File {index} ('{safe}') has identical content to "
                        f"file {first} - remove one copy.")
            try:
                upload.page_count = _page_count(data)
                total_pages += upload.page_count
            except Exception:
                problem(INVALID_PDF,
                        f"File {index} ('{safe}') could not be read as a "
                        "PDF (corrupt or password-protected).")

        if upload.status != FILE_INVALID:
            upload.status = FILE_VALIDATED
        results.append(upload)

    if total_pages > limits["max_pages"]:
        issues.append(ValidationIssue(
            TOO_MANY_PAGES,
            f"Selected files total {total_pages} pages; the limit is "
            f"{limits['max_pages']} pages per job."))
    return results, issues


# --- job persistence --------------------------------------------------------------

def new_transfer_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"tjob-{stamp}-{secrets.token_hex(6)}"


def transfer_job_dir_for(job_id: str) -> Path:
    """Resolve a transfer-job directory from an app-format id ONLY. Rejects
    every other shape - including invoice job-... ids - so no
    user-controlled value can address a path, and the invoice and transfer
    loaders can never read each other's jobs."""
    if not TJOB_ID_RE.match(job_id or ""):
        raise JobError("Unknown transfer job id.")
    root = transfer_jobs_root()
    path = (root / job_id).resolve()
    if path.parent != root:
        raise JobError("Unknown transfer job id.")
    return path


def _write_metadata(job_dir: Path, job: TransferPackingJob) -> None:
    """Atomic same-directory replace, matching the project's established
    tmp + os.replace pattern for job state files."""
    final = job_dir / METADATA_NAME
    tmp = job_dir / f"{METADATA_NAME}.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(job.as_dict(), indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, final)


def create_transfer_job(files: list[tuple[str, bytes]],
                        validated: list[TransferUploadFile]) -> str:
    """Persist a validated selection as a new Transfer Packing job.

    Callers must pass the SAME ordered list that validate_transfer_uploads
    saw, with zero issues. Uploads are stored under input/ using the
    sequence-prefixed stored_name (order-stable; collisions impossible
    within and across jobs because every job has its own directory).
    Status: READY_FOR_EXTRACTION - no extraction starts in Build 1.
    """
    if not validated or len(files) != len(validated):
        raise JobError("Validation results do not match the upload set.")
    if any(f.status != FILE_VALIDATED for f in validated):
        raise JobError("Cannot create a job from an invalid selection.")
    root = transfer_jobs_root()
    root.mkdir(parents=True, exist_ok=True)
    job_id = new_transfer_job_id()
    job_dir = root / job_id
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True)
    input_dir = input_dir.resolve()
    for (_, data), upload in zip(files, validated):
        dest = (input_dir / upload.stored_name).resolve()
        if dest.parent != input_dir:   # defense in depth after sanitizing
            raise JobError("Unknown transfer job id.")
        dest.write_bytes(data)
    job = TransferPackingJob(job_id=job_id, created_at=utc_now(),
                             status=JOB_READY_FOR_EXTRACTION,
                             files=list(validated))
    _write_metadata(job_dir, job)
    return job_id


def load_transfer_job(job_id: str) -> TransferPackingJob | None:
    """Load a transfer job by id; None when absent/unreadable. Refuses
    metadata whose job_type is not transfer_packing (a foreign or tampered
    file must not be presented as a transfer job)."""
    try:
        job_dir = transfer_job_dir_for(job_id)
    except JobError:
        return None
    path = job_dir / METADATA_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("job_type") != "transfer_packing":
        return None
    try:
        return TransferPackingJob.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def newest_transfer_job_id() -> str | None:
    """Most recent transfer job (for refresh recovery). Scans ONLY the
    transfer root for tjob-pattern directories; symlinks are skipped."""
    root = transfer_jobs_root()
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir(), reverse=True):
        if (TJOB_ID_RE.match(entry.name) and entry.is_dir()
                and not entry.is_symlink()
                and (entry / METADATA_NAME).is_file()):
            return entry.name
    return None
