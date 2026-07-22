"""Extraction result models for the Transfer Note workflow (Build 2).

Everything is a plain serializable dataclass persisted as
extraction/result.json inside the transfer job directory. Raw source
values are always preserved next to their normalized forms for audit.
No API enrichment, carton resequencing, or Excel output exists here.
"""

from dataclasses import asdict, dataclass, field

EXTRACTION_SCHEMA_VERSION = 1

# --- issue severities -------------------------------------------------------------

SEV_ERROR = "error"       # blocks clean acceptance; document needs review
SEV_WARNING = "warning"   # kept for review; extraction still usable

# --- issue codes (stable, machine-readable) ---------------------------------------

UNRECOGNIZED_DOCUMENT = "UNRECOGNIZED_DOCUMENT"
UNREADABLE_PAGE = "UNREADABLE_PAGE"
OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
MISSING_DESTINATION = "MISSING_DESTINATION"
AMBIGUOUS_DESTINATION = "AMBIGUOUS_DESTINATION"
MISSING_DELIVERY_NOTE_NO = "MISSING_DELIVERY_NOTE_NO"
MISSING_CARTON_NO = "MISSING_CARTON_NO"
NO_ITEM_LINES = "NO_ITEM_LINES"
MISSING_ITEM_IDENTIFIER = "MISSING_ITEM_IDENTIFIER"
INVALID_EAN = "INVALID_EAN"
MISSING_COLOR = "MISSING_COLOR"
MISSING_SIZE = "MISSING_SIZE"
INVALID_QUANTITY = "INVALID_QUANTITY"
INVALID_RETAIL_PRICE = "INVALID_RETAIL_PRICE"
MALFORMED_ITEM_ROW = "MALFORMED_ITEM_ROW"
CARTON_TOTAL_MISMATCH = "CARTON_TOTAL_MISMATCH"
DOCUMENT_TOTAL_MISMATCH = "DOCUMENT_TOTAL_MISMATCH"
PRINTED_TOTAL_UNREADABLE = "PRINTED_TOTAL_UNREADABLE"
DOCUMENT_EXTRACTION_FAILED = "DOCUMENT_EXTRACTION_FAILED"

ISSUE_CODES = (
    UNRECOGNIZED_DOCUMENT, UNREADABLE_PAGE, OCR_UNAVAILABLE,
    MISSING_DESTINATION, AMBIGUOUS_DESTINATION, MISSING_DELIVERY_NOTE_NO,
    MISSING_CARTON_NO, NO_ITEM_LINES, MISSING_ITEM_IDENTIFIER, INVALID_EAN,
    MISSING_COLOR, MISSING_SIZE, INVALID_QUANTITY, INVALID_RETAIL_PRICE,
    MALFORMED_ITEM_ROW, CARTON_TOTAL_MISMATCH, DOCUMENT_TOTAL_MISMATCH,
    PRINTED_TOTAL_UNREADABLE, DOCUMENT_EXTRACTION_FAILED,
)

# page extraction methods
METHOD_EMBEDDED = "embedded_text"
METHOD_OCR = "ocr"
METHOD_UNREADABLE = "unreadable"


@dataclass
class TransferExtractionIssue:
    code: str
    severity: str
    message: str
    source_file: str = ""
    source_page: int | None = None
    carton: str | None = None
    line_ref: int | None = None       # source sequence number within carton
    field: str | None = None
    raw_value: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TransferExtractionIssue":
        return cls(**{k: d.get(k) for k in (
            "code", "severity", "message", "source_file", "source_page",
            "carton", "line_ref", "field", "raw_value")})


@dataclass
class TransferNoteLine:
    """One item row, in original order. String identity fields stay strings
    (leading zeros preserved); numbers are normalized alongside raw text.
    normalized_item_code/color/size deliberately support the future
    Item+Color+Size fallback lookup, which is NOT built here."""
    source_file: str
    upload_sequence: int
    source_page: int
    delivery_note_number: str | None
    original_carton_number: str | None
    source_sequence_number: int | None
    raw_item_code: str | None = None
    normalized_item_code: str | None = None
    raw_ean: str | None = None
    normalized_ean: str | None = None
    raw_description: str | None = None
    normalized_description: str | None = None
    raw_retail_price: str | None = None
    normalized_retail_price: str | None = None    # Decimal serialized as str
    raw_color_code: str | None = None
    normalized_color_code: str | None = None
    raw_size_code: str | None = None
    normalized_size_code: str | None = None
    raw_quantity: str | None = None
    normalized_quantity: int | None = None
    extraction_method: str = METHOD_EMBEDDED
    extraction_confidence: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TransferNoteLine":
        return cls(**d)


@dataclass
class TransferCarton:
    """One physical carton. Identity is the PRINTED original carton number
    (leading zeros preserved, never resequenced, never invented). A carton
    may span several consecutive pages; source_pages keeps them in order."""
    source_file: str
    upload_sequence: int
    source_page: int                        # first page of the carton
    delivery_note_number: str | None
    destination_code: str | None
    original_carton_number: str | None
    printed_carton_total_raw: str | None = None
    printed_carton_total: int | None = None
    calculated_carton_total: int = 0
    validation_status: str = "unvalidated"  # matched|mismatch|no_printed_total
    destination_inherited: bool = False
    source_pages: list[int] = field(default_factory=list)
    lines: list[TransferNoteLine] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["lines"] = [ln.as_dict() if isinstance(ln, TransferNoteLine) else ln
                      for ln in self.lines]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TransferCarton":
        d = dict(d)
        d["lines"] = [TransferNoteLine.from_dict(x) for x in d.get("lines", [])]
        return cls(**d)


@dataclass
class TransferNoteHeader:
    source_file: str
    upload_sequence: int
    document_title: str | None = None
    batch_reference: str | None = None
    from_location_raw: str | None = None
    from_location_code: str | None = None
    from_location_name: str | None = None
    to_location_raw: str | None = None
    to_location_code: str | None = None
    to_location_name: str | None = None
    pick_reference: str | None = None
    delivery_note_number: str | None = None
    delivery_date_raw: str | None = None
    delivery_date: str | None = None        # ISO YYYY-MM-DD when parseable
    declared_page_number: int | None = None
    declared_page_count: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TransferNoteHeader":
        return cls(**d)


@dataclass
class TransferDocumentExtraction:
    """Extraction of ONE uploaded PDF. A failure in another document never
    affects this one; a page failure never discards the other pages."""
    source_file: str
    upload_sequence: int
    recognized: bool = False
    header: TransferNoteHeader | None = None
    cartons: list[TransferCarton] = field(default_factory=list)
    issues: list[TransferExtractionIssue] = field(default_factory=list)
    page_count: int = 0
    pages_embedded_text: int = 0
    pages_ocr: int = 0
    pages_unreadable: int = 0
    page_methods: list[str] = field(default_factory=list)   # per page, in order
    printed_grand_total_raw: str | None = None
    printed_grand_total: int | None = None
    calculated_grand_total: int = 0

    def as_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "upload_sequence": self.upload_sequence,
            "recognized": self.recognized,
            "header": self.header.as_dict() if self.header else None,
            "cartons": [c.as_dict() for c in self.cartons],
            "issues": [i.as_dict() for i in self.issues],
            "page_count": self.page_count,
            "pages_embedded_text": self.pages_embedded_text,
            "pages_ocr": self.pages_ocr,
            "pages_unreadable": self.pages_unreadable,
            "page_methods": list(self.page_methods),
            "printed_grand_total_raw": self.printed_grand_total_raw,
            "printed_grand_total": self.printed_grand_total,
            "calculated_grand_total": self.calculated_grand_total,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TransferDocumentExtraction":
        return cls(
            source_file=d["source_file"],
            upload_sequence=d["upload_sequence"],
            recognized=d.get("recognized", False),
            header=(TransferNoteHeader.from_dict(d["header"])
                    if d.get("header") else None),
            cartons=[TransferCarton.from_dict(c) for c in d.get("cartons", [])],
            issues=[TransferExtractionIssue.from_dict(i)
                    for i in d.get("issues", [])],
            page_count=d.get("page_count", 0),
            pages_embedded_text=d.get("pages_embedded_text", 0),
            pages_ocr=d.get("pages_ocr", 0),
            pages_unreadable=d.get("pages_unreadable", 0),
            page_methods=list(d.get("page_methods", [])),
            printed_grand_total_raw=d.get("printed_grand_total_raw"),
            printed_grand_total=d.get("printed_grand_total"),
            calculated_grand_total=d.get("calculated_grand_total", 0),
        )


@dataclass
class TransferExtractionResult:
    """The persisted Build 2 artifact (extraction/result.json)."""
    job_id: str
    started_at: str
    finished_at: str = ""
    documents: list[TransferDocumentExtraction] = field(default_factory=list)
    schema_version: int = EXTRACTION_SCHEMA_VERSION

    # --- aggregates -------------------------------------------------------------

    def all_issues(self) -> list[TransferExtractionIssue]:
        return [i for doc in self.documents for i in doc.issues]

    def error_count(self) -> int:
        return sum(1 for i in self.all_issues() if i.severity == SEV_ERROR)

    def warning_count(self) -> int:
        return sum(1 for i in self.all_issues() if i.severity == SEV_WARNING)

    def summary(self) -> dict:
        docs = self.documents
        cartons = [c for d in docs for c in d.cartons]
        lines = [ln for c in cartons for ln in c.lines]
        destinations = sorted({c.destination_code for c in cartons
                               if c.destination_code})
        return {
            "uploaded_files": len(docs),
            "processed_files": sum(1 for d in docs if d.page_count > 0),
            "processed_pages": sum(d.page_count for d in docs),
            "pages_embedded_text": sum(d.pages_embedded_text for d in docs),
            "pages_ocr": sum(d.pages_ocr for d in docs),
            "pages_unreadable": sum(d.pages_unreadable for d in docs),
            "recognized_documents": sum(1 for d in docs if d.recognized),
            "destination_codes": destinations,
            "cartons": len(cartons),
            "lines": len(lines),
            "total_units": sum(ln.normalized_quantity or 0 for ln in lines),
            "warnings": self.warning_count(),
            "errors": self.error_count(),
        }

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary(),
            "documents": [d.as_dict() for d in self.documents],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TransferExtractionResult":
        return cls(
            job_id=d["job_id"],
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            documents=[TransferDocumentExtraction.from_dict(x)
                       for x in d.get("documents", [])],
            schema_version=d.get("schema_version", EXTRACTION_SCHEMA_VERSION),
        )
