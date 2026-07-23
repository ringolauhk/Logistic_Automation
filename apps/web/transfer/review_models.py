"""Review-data models for the Transfer Note workflow (Build 3).

The Build 2 extraction result (extraction/result.json) is the IMMUTABLE
source record. Review data lives in a separate artifact (review/review.json)
holding, per entity, a frozen snapshot of the original extracted values, an
explicit corrections map, and exclusion state - original values are never
overwritten and every change is recorded in an audit trail.

Correction semantics are explicit, never inferred from display text:

  * field NOT in corrections            -> unchanged (effective = original)
  * corrections[field] = "value"        -> corrected (effective = "value")
  * corrections[field] = None           -> deliberately cleared (effective
                                           = None); an empty form cell never
                                           clears anything by accident
  * excluded=True (+ reason)            -> entity excluded from effective
                                           review output; the original and
                                           any corrections stay stored

Single-user pilot: there is no authentication, so reviewed_by is the fixed
string "local-user" (documented limitation - no identity system exists).
"""

from dataclasses import asdict, dataclass, field

REVIEW_SCHEMA_VERSION = 1
REVIEWED_BY = "local-user"

# review artifact status
REVIEW_IN_PROGRESS = "IN_PROGRESS"
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED_STATUS = "REJECTED"
REVIEW_STALE = "STALE"

REVIEW_STATUSES = (REVIEW_IN_PROGRESS, REVIEW_APPROVED,
                   REVIEW_REJECTED_STATUS, REVIEW_STALE)

# Explicit clear sentinel used by callers of apply_correction; persisted as
# JSON null inside corrections.
CLEAR = object()

HEADER_FIELDS = ("batch_reference", "from_location_code",
                 "from_location_name", "to_location_code",
                 "to_location_name", "pick_reference",
                 "delivery_note_number", "delivery_date")
CARTON_FIELDS = ("original_carton_number",)
LINE_FIELDS = ("item_code", "ean", "description", "retail_price",
               "color_code", "size_code", "quantity")


def _effective(original: dict, corrections: dict, fld: str):
    if fld in corrections:
        return corrections[fld]
    return original.get(fld)


@dataclass
class ReviewChange:
    """One audit-trail entry. Appended only when a stored value actually
    changes, so repeated identical saves never duplicate history."""
    entity_type: str            # document | carton | line
    entity_id: str
    field: str
    original_value: str | None
    previous_corrected: str | None
    new_corrected: str | None   # None here means "cleared"
    changed_at: str
    cleared: bool = False       # distinguishes cleared from reverted

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewChange":
        return cls(**d)


@dataclass
class ReviewedEntity:
    """Shared shape: frozen original snapshot + explicit corrections +
    exclusion. `entity_id` is a stable function of the immutable
    extraction result (never a table row position)."""
    entity_id: str
    original: dict = field(default_factory=dict)
    corrections: dict = field(default_factory=dict)
    excluded: bool = False
    exclusion_reason: str | None = None

    def effective(self, fld: str):
        return _effective(self.original, self.corrections, fld)

    def is_corrected(self, fld: str) -> bool:
        return fld in self.corrections

    def changed_fields(self) -> list[str]:
        return sorted(self.corrections)

    def _base_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "original": dict(self.original),
            "corrections": dict(self.corrections),
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass
class ReviewedTransferHeader(ReviewedEntity):
    """One document's header. `original` carries the extraction header
    fields plus raw values and context (source_file, upload_sequence,
    destination_inherited, ...)."""
    source_file: str = ""
    upload_sequence: int = 0

    def as_dict(self) -> dict:
        d = self._base_dict()
        d["source_file"] = self.source_file
        d["upload_sequence"] = self.upload_sequence
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewedTransferHeader":
        return cls(entity_id=d["entity_id"], original=d.get("original", {}),
                   corrections=d.get("corrections", {}),
                   excluded=d.get("excluded", False),
                   exclusion_reason=d.get("exclusion_reason"),
                   source_file=d.get("source_file", ""),
                   upload_sequence=d.get("upload_sequence", 0))


@dataclass
class ReviewedTransferCarton(ReviewedEntity):
    document_id: str = ""
    source_file: str = ""
    upload_sequence: int = 0
    source_pages: list[int] = field(default_factory=list)
    line_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self._base_dict()
        d.update(document_id=self.document_id, source_file=self.source_file,
                 upload_sequence=self.upload_sequence,
                 source_pages=list(self.source_pages),
                 line_ids=list(self.line_ids))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewedTransferCarton":
        return cls(entity_id=d["entity_id"], original=d.get("original", {}),
                   corrections=d.get("corrections", {}),
                   excluded=d.get("excluded", False),
                   exclusion_reason=d.get("exclusion_reason"),
                   document_id=d.get("document_id", ""),
                   source_file=d.get("source_file", ""),
                   upload_sequence=d.get("upload_sequence", 0),
                   source_pages=list(d.get("source_pages", [])),
                   line_ids=list(d.get("line_ids", [])))


@dataclass
class ReviewedTransferLine(ReviewedEntity):
    document_id: str = ""
    carton_id: str = ""
    source_file: str = ""
    upload_sequence: int = 0
    source_page: int = 0

    def as_dict(self) -> dict:
        d = self._base_dict()
        d.update(document_id=self.document_id, carton_id=self.carton_id,
                 source_file=self.source_file,
                 upload_sequence=self.upload_sequence,
                 source_page=self.source_page)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewedTransferLine":
        return cls(entity_id=d["entity_id"], original=d.get("original", {}),
                   corrections=d.get("corrections", {}),
                   excluded=d.get("excluded", False),
                   exclusion_reason=d.get("exclusion_reason"),
                   document_id=d.get("document_id", ""),
                   carton_id=d.get("carton_id", ""),
                   source_file=d.get("source_file", ""),
                   upload_sequence=d.get("upload_sequence", 0),
                   source_page=d.get("source_page", 0))


@dataclass
class TransferReviewResult:
    """The persisted review artifact (review/review.json)."""
    job_id: str
    extraction_checksum: str
    created_at: str
    updated_at: str
    source_extraction_schema_version: int = 1
    status: str = REVIEW_IN_PROGRESS
    reviewed_by: str = REVIEWED_BY
    headers: list[ReviewedTransferHeader] = field(default_factory=list)
    cartons: list[ReviewedTransferCarton] = field(default_factory=list)
    lines: list[ReviewedTransferLine] = field(default_factory=list)
    changes: list[ReviewChange] = field(default_factory=list)
    schema_version: int = REVIEW_SCHEMA_VERSION

    def header_by_id(self, entity_id: str) -> ReviewedTransferHeader | None:
        return next((h for h in self.headers if h.entity_id == entity_id),
                    None)

    def carton_by_id(self, entity_id: str) -> ReviewedTransferCarton | None:
        return next((c for c in self.cartons if c.entity_id == entity_id),
                    None)

    def line_by_id(self, entity_id: str) -> ReviewedTransferLine | None:
        return next((ln for ln in self.lines if ln.entity_id == entity_id),
                    None)

    def entity_by_id(self, entity_type: str, entity_id: str):
        if entity_type == "document":
            return self.header_by_id(entity_id)
        if entity_type == "carton":
            return self.carton_by_id(entity_id)
        if entity_type == "line":
            return self.line_by_id(entity_id)
        return None

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "source_extraction_schema_version":
                self.source_extraction_schema_version,
            "extraction_checksum": self.extraction_checksum,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "reviewed_by": self.reviewed_by,
            "headers": [h.as_dict() for h in self.headers],
            "cartons": [c.as_dict() for c in self.cartons],
            "lines": [ln.as_dict() for ln in self.lines],
            "changes": [ch.as_dict() for ch in self.changes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TransferReviewResult":
        return cls(
            job_id=d["job_id"],
            extraction_checksum=d["extraction_checksum"],
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            source_extraction_schema_version=d.get(
                "source_extraction_schema_version", 1),
            status=d.get("status", REVIEW_IN_PROGRESS),
            reviewed_by=d.get("reviewed_by", REVIEWED_BY),
            headers=[ReviewedTransferHeader.from_dict(x)
                     for x in d.get("headers", [])],
            cartons=[ReviewedTransferCarton.from_dict(x)
                     for x in d.get("cartons", [])],
            lines=[ReviewedTransferLine.from_dict(x)
                   for x in d.get("lines", [])],
            changes=[ReviewChange.from_dict(x) for x in d.get("changes", [])],
            schema_version=d.get("schema_version", REVIEW_SCHEMA_VERSION),
        )
