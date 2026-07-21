"""Domain models for the Transfer Note Packing List workflow (Build 1).

Deliberately separate from the invoice models: a Transfer Delivery Note is
not an invoice, and the two workflows must never share job state. Upload
order is a business rule - cartons are processed in the order the user
uploaded the files - so `sequence` is explicit, persistent, and never
derived from filenames.
"""

from dataclasses import dataclass, field

SCHEMA_VERSION = 1

JOB_TYPE_TRANSFER = "transfer_packing"
JOB_TYPE_INVOICE = "invoice_extraction"   # the implicit type of legacy jobs

# --- statuses ---------------------------------------------------------------------

FILE_UPLOADED = "UPLOADED"
FILE_VALIDATED = "VALIDATED"
FILE_INVALID = "INVALID"

JOB_READY_FOR_EXTRACTION = "READY_FOR_EXTRACTION"
JOB_CANCELLED = "CANCELLED"
JOB_FAILED = "FAILED"

JOB_STATUSES = (JOB_READY_FOR_EXTRACTION, JOB_CANCELLED, JOB_FAILED)

# --- machine-readable validation codes --------------------------------------------

NO_FILES = "NO_FILES"
UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
EMPTY_FILE = "EMPTY_FILE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
TOO_MANY_FILES = "TOO_MANY_FILES"
TOO_MANY_PAGES = "TOO_MANY_PAGES"
INVALID_PDF = "INVALID_PDF"
DUPLICATE_FILE = "DUPLICATE_FILE"

VALIDATION_CODES = (NO_FILES, UNSUPPORTED_FILE_TYPE, EMPTY_FILE,
                    FILE_TOO_LARGE, TOO_MANY_FILES, TOO_MANY_PAGES,
                    INVALID_PDF, DUPLICATE_FILE)


@dataclass(frozen=True)
class ValidationIssue:
    """One problem, with a stable machine code and a safe user message.
    `sequence` ties a per-file issue to its upload position (None for
    batch-level issues like TOO_MANY_FILES)."""
    code: str
    message: str
    sequence: int | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "sequence": self.sequence}


@dataclass
class TransferUploadFile:
    """One uploaded Transfer Delivery Note PDF, in explicit upload order.

    `sequence` is 1-based and mirrors the order returned by the uploader.
    `stored_name` is the sequence-prefixed sanitized filename used on disk
    (order-stable, collision-free); `original_name` is kept verbatim for
    display and audit only and is never used to build paths.
    """
    sequence: int
    original_name: str
    stored_name: str
    size_bytes: int
    mime: str = "application/pdf"
    sha256: str = ""
    page_count: int | None = None
    status: str = FILE_UPLOADED
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "original_name": self.original_name,
            "stored_name": self.stored_name,
            "size_bytes": self.size_bytes,
            "mime": self.mime,
            "sha256": self.sha256,
            "page_count": self.page_count,
            "status": self.status,
            "messages": list(self.messages),
        }

    @classmethod
    def from_dict(cls, row: dict) -> "TransferUploadFile":
        return cls(
            sequence=int(row["sequence"]),
            original_name=str(row.get("original_name", "")),
            stored_name=str(row.get("stored_name", "")),
            size_bytes=int(row.get("size_bytes", 0)),
            mime=str(row.get("mime", "application/pdf")),
            sha256=str(row.get("sha256", "")),
            page_count=row.get("page_count"),
            status=str(row.get("status", FILE_UPLOADED)),
            messages=[str(m) for m in row.get("messages", [])],
        )


@dataclass
class TransferPackingJob:
    """A Transfer Packing job: ordered uploads + validation summary. The
    `extraction` and `outputs` blocks are reserved extension points for the
    later builds (OCR extraction, To-Loc. grouping, per-destination Excel
    packing lists) and stay empty in Build 1."""
    job_id: str
    created_at: str
    status: str = JOB_READY_FOR_EXTRACTION
    files: list[TransferUploadFile] = field(default_factory=list)
    job_type: str = JOB_TYPE_TRANSFER
    schema_version: int = SCHEMA_VERSION
    extraction: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)

    @property
    def total_pages(self) -> int:
        return sum(f.page_count or 0 for f in self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "job_type": self.job_type,
            "created_at": self.created_at,
            "status": self.status,
            "summary": {
                "file_count": len(self.files),
                "total_pages": self.total_pages,
                "total_bytes": self.total_bytes,
            },
            "files": [f.as_dict() for f in self.files],
            "extraction": dict(self.extraction),
            "outputs": dict(self.outputs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TransferPackingJob":
        return cls(
            job_id=str(data["job_id"]),
            created_at=str(data.get("created_at", "")),
            status=str(data.get("status", JOB_READY_FOR_EXTRACTION)),
            files=[TransferUploadFile.from_dict(r)
                   for r in data.get("files", [])],
            job_type=str(data.get("job_type", JOB_TYPE_TRANSFER)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            extraction=dict(data.get("extraction", {})),
            outputs=dict(data.get("outputs", {})),
        )
