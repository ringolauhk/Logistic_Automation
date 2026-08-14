"""Transfer Note extraction execution + persistence (Build 2).

Synchronous, deterministic, fully local: classify pages, read embedded text,
OCR scanned pages with the optional local engine, parse, validate totals,
and persist ONE atomic artifact:

    <job dir>/extraction/result.json      (schema-versioned)

Build 1 metadata (transfer_job.json) is never rewritten except for its
status field, via the validated state machine. A retry atomically replaces
result.json - results are never duplicated - and one document's failure
never discards another document's results.
"""

import json
import os
from pathlib import Path

from apps.web.job_manager import JobError, utc_now
from apps.web.transfer import jobs
from apps.web.transfer.extraction_models import (
    DOCUMENT_EXTRACTION_FAILED,
    SEV_ERROR,
    TransferDocumentExtraction,
    TransferExtractionIssue,
    TransferExtractionResult,
)
from apps.web.transfer.models import (
    EXTRACTABLE_STATUSES,
    JOB_EXTRACTED,
    JOB_EXTRACTED_WITH_ISSUES,
    JOB_EXTRACTING,
    JOB_FAILED,
    TransferPackingJob,
)
from apps.web.transfer.ocr import OcrAdapter, get_default_adapter
from apps.web.transfer.pagetext import extract_page_texts
from apps.web.transfer.parser import parse_document

RESULT_NAME = "result.json"


def result_path(job_id: str) -> Path:
    return jobs.transfer_job_dir_for(job_id) / "extraction" / RESULT_NAME


def load_result(job_id: str) -> TransferExtractionResult | None:
    """Reload the persisted extraction result (refresh recovery)."""
    try:
        path = result_path(job_id)
    except JobError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return TransferExtractionResult.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _write_result(job_id: str, result: TransferExtractionResult) -> None:
    """Atomic same-directory replace - a retry can never duplicate or
    half-overwrite a previous result."""
    path = result_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{RESULT_NAME}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(result.as_dict(), indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def run_extraction(job_id: str, *,
                   adapter: OcrAdapter | None = None,
                   use_default_adapter: bool = True,
                   on_progress=None) -> TransferExtractionResult:
    """Extract every uploaded file of a READY (or retryable) transfer job.

    Files are processed in upload sequence; page order is preserved inside
    each file. A document that blows up entirely is recorded as a
    DOCUMENT_EXTRACTION_FAILED issue while every other document's results
    are kept. No cloud provider and no internal API is ever called.
    """
    job = jobs.load_transfer_job(job_id)
    if job is None:
        raise JobError("Unknown transfer job id.")
    if job.status not in EXTRACTABLE_STATUSES:
        raise JobError(f"Job in state {job.status} cannot be extracted.")
    jobs.update_job_status(job_id, JOB_EXTRACTING)

    if adapter is None and use_default_adapter:
        adapter = get_default_adapter()

    result = TransferExtractionResult(job_id=job_id, started_at=utc_now())
    try:
        job_dir = jobs.transfer_job_dir_for(job_id)
        for upload in sorted(job.files, key=lambda f: f.sequence):
            if on_progress is not None:
                try:
                    on_progress(upload.sequence, len(job.files),
                                upload.original_name)
                except Exception:
                    pass
            pdf_path = job_dir / "input" / upload.stored_name
            try:
                pages = extract_page_texts(str(pdf_path), ocr_adapter=adapter)
                doc = parse_document(
                    source_file=upload.original_name,
                    upload_sequence=upload.sequence,
                    pages=pages, adapter=adapter)
            except Exception as exc:
                doc = TransferDocumentExtraction(
                    source_file=upload.original_name,
                    upload_sequence=upload.sequence)
                doc.issues.append(TransferExtractionIssue(
                    code=DOCUMENT_EXTRACTION_FAILED, severity=SEV_ERROR,
                    message=("This file could not be processed "
                             f"({type(exc).__name__}); other files are "
                             "unaffected."),
                    source_file=upload.original_name))
            result.documents.append(doc)
        result.finished_at = utc_now()
        _write_result(job_id, result)
    except Exception:
        jobs.update_job_status(job_id, JOB_FAILED)
        raise
    new_status = (JOB_EXTRACTED_WITH_ISSUES if result.error_count()
                  else JOB_EXTRACTED)
    jobs.update_job_status(job_id, new_status)
    return result


def refresh_job(job_id: str) -> TransferPackingJob | None:
    return jobs.load_transfer_job(job_id)
