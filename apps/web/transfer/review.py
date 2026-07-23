"""Transfer Note review logic (Build 3): build, correct, exclude, evaluate,
save, and approve - all deterministic and offline.

Key invariants:

  * extraction/result.json is never modified; review/review.json is a
    separate, schema-versioned, atomically-written artifact;
  * every reviewed entity keeps a frozen `original` snapshot; corrections
    live beside it and effective = corrected-if-present else original;
  * issue resolution is a PURE function of effective values + exclusions -
    saving alone never resolves anything;
  * an extraction rerun changes the extraction checksum, which marks any
    existing review STALE; a stale review can never be approved, and
    regeneration archives the old artifact for audit;
  * no product API, no carton resequencing, no row consolidation.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from apps.web.job_manager import JobError, utc_now
from apps.web.transfer import extraction as extraction_mod
from apps.web.transfer import jobs
from apps.web.transfer.extraction_models import (
    AMBIGUOUS_DESTINATION,
    CARTON_TOTAL_MISMATCH,
    DOCUMENT_EXTRACTION_FAILED,
    DOCUMENT_TOTAL_MISMATCH,
    INVALID_EAN,
    INVALID_QUANTITY,
    INVALID_RETAIL_PRICE,
    MALFORMED_ITEM_ROW,
    MISSING_CARTON_NO,
    MISSING_COLOR,
    MISSING_DELIVERY_NOTE_NO,
    MISSING_DESTINATION,
    MISSING_ITEM_IDENTIFIER,
    MISSING_SIZE,
    NO_ITEM_LINES,
    OCR_UNAVAILABLE,
    PRINTED_TOTAL_UNREADABLE,
    SEV_ERROR,
    SEV_WARNING,
    UNREADABLE_PAGE,
    UNRECOGNIZED_DOCUMENT,
    TransferExtractionResult,
)
from apps.web.transfer.models import (
    JOB_EXTRACTED,
    JOB_EXTRACTED_WITH_ISSUES,
    JOB_PRODUCT_LOOKUP_COMPLETE,
    JOB_PRODUCT_LOOKUP_FAILED,
    JOB_PRODUCT_LOOKUP_IN_PROGRESS,
    JOB_PRODUCT_LOOKUP_WITH_ISSUES,
    JOB_READY_FOR_PRODUCT_LOOKUP,
    JOB_REVIEW_IN_PROGRESS,
    JOB_REVIEW_REJECTED,
)
from apps.web.transfer.review_models import (
    CARTON_FIELDS,
    CLEAR,
    HEADER_FIELDS,
    LINE_FIELDS,
    REVIEW_APPROVED,
    REVIEW_IN_PROGRESS,
    REVIEW_REJECTED_STATUS,
    REVIEW_STALE,
    ReviewChange,
    ReviewedTransferCarton,
    ReviewedTransferHeader,
    ReviewedTransferLine,
    TransferReviewResult,
)

REVIEW_DIR = "review"
REVIEW_NAME = "review.json"

# Statuses from which the review workflow is reachable in the UI.
REVIEWABLE_JOB_STATUSES = (JOB_EXTRACTED, JOB_EXTRACTED_WITH_ISSUES,
                           JOB_REVIEW_IN_PROGRESS,
                           JOB_READY_FOR_PRODUCT_LOOKUP, JOB_REVIEW_REJECTED,
                           JOB_PRODUCT_LOOKUP_IN_PROGRESS,
                           JOB_PRODUCT_LOOKUP_COMPLETE,
                           JOB_PRODUCT_LOOKUP_WITH_ISSUES,
                           JOB_PRODUCT_LOOKUP_FAILED)

# Conservative EAN rule (matches the source documents observed so far):
# digits only after trimming, 8-14 digits, leading zeros preserved.
_EAN_VALID_RE = re.compile(r"^\d{8,14}$")


# --- checksum + paths -------------------------------------------------------------

def extraction_checksum(job_id: str) -> str | None:
    """SHA-256 of the persisted extraction artifact's bytes. Any rerun of
    extraction rewrites the file (new finished_at), so a changed checksum
    reliably marks dependent reviews stale."""
    try:
        path = extraction_mod.result_path(job_id)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (JobError, OSError):
        return None


def review_path(job_id: str) -> Path:
    return jobs.transfer_job_dir_for(job_id) / REVIEW_DIR / REVIEW_NAME


# --- building the initial review from an extraction result ------------------------

def _document_id(upload_sequence: int) -> str:
    return f"D{upload_sequence:03d}"


def _carton_entity_id(upload_sequence: int, carton_number: str | None,
                      first_page: int) -> str:
    tail = carton_number if carton_number else f"P{first_page:03d}"
    return f"D{upload_sequence:03d}-C{tail}"


def build_initial_review(result: TransferExtractionResult,
                         checksum: str) -> TransferReviewResult:
    """Convert an extraction result into fresh review data. Original values
    are copied into frozen snapshots; nothing in the extraction is touched.
    Entity IDs derive only from the immutable extraction content."""
    now = utc_now()
    review = TransferReviewResult(job_id=result.job_id,
                                  extraction_checksum=checksum,
                                  created_at=now, updated_at=now)
    for doc in result.documents:
        doc_id = _document_id(doc.upload_sequence)
        h = doc.header
        review.headers.append(ReviewedTransferHeader(
            entity_id=doc_id,
            source_file=doc.source_file,
            upload_sequence=doc.upload_sequence,
            original={
                "batch_reference": h.batch_reference if h else None,
                "from_location_code": h.from_location_code if h else None,
                "from_location_name": h.from_location_name if h else None,
                "to_location_code": h.to_location_code if h else None,
                "to_location_name": h.to_location_name if h else None,
                "pick_reference": h.pick_reference if h else None,
                "delivery_note_number": h.delivery_note_number if h else None,
                "delivery_date": h.delivery_date if h else None,
                "delivery_date_raw": h.delivery_date_raw if h else None,
                "to_location_raw": h.to_location_raw if h else None,
                "from_location_raw": h.from_location_raw if h else None,
                "recognized": doc.recognized,
            }))
        for carton in doc.cartons:
            carton_id = _carton_entity_id(doc.upload_sequence,
                                          carton.original_carton_number,
                                          carton.source_page)
            line_ids = []
            for ordinal, line in enumerate(carton.lines, start=1):
                line_id = f"{carton_id}-L{ordinal:03d}"
                line_ids.append(line_id)
                review.lines.append(ReviewedTransferLine(
                    entity_id=line_id,
                    document_id=doc_id,
                    carton_id=carton_id,
                    source_file=line.source_file,
                    upload_sequence=line.upload_sequence,
                    source_page=line.source_page,
                    original={
                        "item_code": line.normalized_item_code,
                        "ean": line.normalized_ean,
                        "description": line.normalized_description,
                        "retail_price": line.normalized_retail_price,
                        "color_code": line.normalized_color_code,
                        "size_code": line.normalized_size_code,
                        "quantity": (str(line.normalized_quantity)
                                     if line.normalized_quantity is not None
                                     else None),
                        "raw_item_code": line.raw_item_code,
                        "raw_ean": line.raw_ean,
                        "raw_description": line.raw_description,
                        "raw_retail_price": line.raw_retail_price,
                        "raw_color_code": line.raw_color_code,
                        "raw_size_code": line.raw_size_code,
                        "raw_quantity": line.raw_quantity,
                        "source_sequence_number": line.source_sequence_number,
                        "delivery_note_number": line.delivery_note_number,
                        "original_carton_number": line.original_carton_number,
                        "extraction_method": line.extraction_method,
                        "extraction_confidence": line.extraction_confidence,
                    }))
            review.cartons.append(ReviewedTransferCarton(
                entity_id=carton_id,
                document_id=doc_id,
                source_file=carton.source_file,
                upload_sequence=carton.upload_sequence,
                source_pages=list(carton.source_pages),
                line_ids=line_ids,
                original={
                    "original_carton_number": carton.original_carton_number,
                    "destination_code": carton.destination_code,
                    "destination_inherited": carton.destination_inherited,
                    "delivery_note_number": carton.delivery_note_number,
                    "printed_carton_total": carton.printed_carton_total,
                    "printed_carton_total_raw": carton.printed_carton_total_raw,
                    "calculated_carton_total": carton.calculated_carton_total,
                    "validation_status": carton.validation_status,
                }))
    return review


# --- corrections + exclusions -----------------------------------------------------

_UPPERCASE_FIELDS = {"to_location_code", "from_location_code", "color_code",
                     "size_code", "item_code"}


def normalize_correction(fld: str, value: str) -> str:
    value = value.strip()
    if fld in _UPPERCASE_FIELDS:
        value = value.upper()
    return value


def apply_correction(review: TransferReviewResult, entity_type: str,
                     entity_id: str, fld: str, value) -> bool:
    """Set/replace/clear/revert one field's correction. Returns True when
    anything changed (and records an audit entry); a no-op returns False so
    repeated saves cannot duplicate history.

      value = CLEAR        -> deliberate clear (effective None)
      value == original    -> correction removed (revert to unchanged)
      value = str          -> corrected value (trimmed; codes uppercased)
    """
    allowed = {"document": HEADER_FIELDS, "carton": CARTON_FIELDS,
               "line": LINE_FIELDS}[entity_type]
    if fld not in allowed:
        raise JobError(f"Field '{fld}' is not reviewable for {entity_type}.")
    entity = review.entity_by_id(entity_type, entity_id)
    if entity is None:
        raise JobError(f"Unknown {entity_type} '{entity_id}'.")

    previous_corrected = entity.corrections.get(fld, _SENTINEL)
    original = entity.original.get(fld)
    cleared = value is CLEAR
    if cleared:
        new = None
    else:
        new = normalize_correction(fld, str(value))
        if new == (original or ""):
            # matches the original -> not a correction at all
            if fld in entity.corrections:
                del entity.corrections[fld]
                review.changes.append(ReviewChange(
                    entity_type=entity_type, entity_id=entity_id, field=fld,
                    original_value=original,
                    previous_corrected=(None if previous_corrected is _SENTINEL
                                        else previous_corrected),
                    new_corrected=original, changed_at=utc_now()))
                return True
            return False
    if previous_corrected is not _SENTINEL and previous_corrected == new:
        return False
    entity.corrections[fld] = new
    review.changes.append(ReviewChange(
        entity_type=entity_type, entity_id=entity_id, field=fld,
        original_value=original,
        previous_corrected=(None if previous_corrected is _SENTINEL
                            else previous_corrected),
        new_corrected=new, changed_at=utc_now(), cleared=cleared))
    return True


_SENTINEL = object()


def set_exclusion(review: TransferReviewResult, entity_type: str,
                  entity_id: str, excluded: bool,
                  reason: str | None = None) -> bool:
    """Exclude/re-include one entity. Excluding requires a reason. The
    cascade (document -> cartons -> lines) is computed at evaluation time,
    so re-including a document restores its children automatically."""
    entity = review.entity_by_id(entity_type, entity_id)
    if entity is None:
        raise JobError(f"Unknown {entity_type} '{entity_id}'.")
    reason = (reason or "").strip() or None
    if excluded and not reason:
        raise JobError("Excluding requires a reason.")
    if entity.excluded == excluded and entity.exclusion_reason == (
            reason if excluded else None):
        return False
    entity.excluded = excluded
    entity.exclusion_reason = reason if excluded else None
    review.changes.append(ReviewChange(
        entity_type=entity_type, entity_id=entity_id, field="excluded",
        original_value=None, previous_corrected=None,
        new_corrected=(f"excluded: {reason}" if excluded else "included"),
        changed_at=utc_now()))
    return True


# --- evaluation -------------------------------------------------------------------

def valid_ean(value) -> bool:
    return bool(value) and bool(_EAN_VALID_RE.match(str(value).strip()))


def _valid_quantity(value) -> int | None:
    try:
        q = int(str(value).strip())
        return q if q > 0 else None
    except (TypeError, ValueError):
        return None


def _valid_price(value) -> bool:
    if value in (None, ""):
        return True                     # price is optional
    try:
        return Decimal(str(value).strip()) >= 0
    except InvalidOperation:
        return False


@dataclass
class LineEvaluation:
    line_id: str
    effective_excluded: bool
    lookup_ready: bool
    quantity: int | None
    problems: list[str] = field(default_factory=list)   # unresolved codes


@dataclass
class ReviewEvaluation:
    """Deterministic, side-effect-free evaluation of a review against its
    extraction result. Everything the approval gate and the summary panel
    need comes from here."""
    lines: dict[str, LineEvaluation] = field(default_factory=dict)
    carton_effective_totals: dict[str, int] = field(default_factory=dict)
    document_effective_totals: dict[str, int] = field(default_factory=dict)
    unresolved_blocking: list[dict] = field(default_factory=list)
    unresolved_warnings: list[dict] = field(default_factory=list)
    resolved_issue_count: int = 0
    included_documents: int = 0
    included_cartons: int = 0
    included_lines: int = 0
    excluded_documents: int = 0
    excluded_cartons: int = 0
    excluded_lines: int = 0
    total_effective_units: int = 0
    corrected_field_count: int = 0
    lookup_ready_lines: int = 0
    lookup_not_ready_lines: int = 0
    destinations: list[str] = field(default_factory=list)
    approval_problems: list[str] = field(default_factory=list)

    @property
    def can_approve(self) -> bool:
        return not self.approval_problems


def _line_lookup_ready(line: ReviewedTransferLine) -> bool:
    """Lookup-ready: valid EAN (primary identifier), OR the fallback
    identifier Item + Color + Size all present. Documented rule: a valid
    EAN alone is sufficient - unresolved MISSING_COLOR/MISSING_SIZE stay
    visible as warnings but never block approval."""
    if valid_ean(line.effective("ean")):
        return True
    return all(line.effective(f) for f in ("item_code", "color_code",
                                           "size_code"))


def evaluate(result: TransferExtractionResult,
             review: TransferReviewResult) -> ReviewEvaluation:
    ev = ReviewEvaluation()
    headers = {h.entity_id: h for h in review.headers}
    cartons = {c.entity_id: c for c in review.cartons}

    def doc_excluded(doc_id: str) -> bool:
        h = headers.get(doc_id)
        return bool(h and h.excluded)

    def carton_excluded(c: ReviewedTransferCarton) -> bool:
        return c.excluded or doc_excluded(c.document_id)

    # --- per-line evaluation -----------------------------------------------------
    for line in review.lines:
        carton = cartons.get(line.carton_id)
        eff_excluded = (line.excluded
                        or (carton is not None and carton_excluded(carton)))
        qty = _valid_quantity(line.effective("quantity"))
        ready = _line_lookup_ready(line)
        problems = []
        if not eff_excluded:
            if qty is None:
                problems.append(INVALID_QUANTITY)
            if not ready:
                problems.append(MISSING_ITEM_IDENTIFIER)
            if not _valid_price(line.effective("retail_price")):
                problems.append(INVALID_RETAIL_PRICE)
        ev.lines[line.entity_id] = LineEvaluation(
            line_id=line.entity_id, effective_excluded=eff_excluded,
            lookup_ready=ready, quantity=qty, problems=problems)
        if eff_excluded:
            ev.excluded_lines += 1
        else:
            ev.included_lines += 1
            ev.total_effective_units += qty or 0
            if ready:
                ev.lookup_ready_lines += 1
            else:
                ev.lookup_not_ready_lines += 1
        ev.corrected_field_count += len(line.corrections)

    # --- carton + document effective totals --------------------------------------
    doc_totals: dict[str, int] = {}
    dests: set[str] = set()
    for carton in review.cartons:
        excluded = carton_excluded(carton)
        total = sum((ev.lines[lid].quantity or 0)
                    for lid in carton.line_ids
                    if lid in ev.lines and not ev.lines[lid].effective_excluded)
        ev.carton_effective_totals[carton.entity_id] = total
        header = headers.get(carton.document_id)
        dest = (carton.effective("destination_code")
                or (header.effective("to_location_code") if header else None))
        if excluded:
            ev.excluded_cartons += 1
        else:
            ev.included_cartons += 1
            doc_totals[carton.document_id] = (
                doc_totals.get(carton.document_id, 0) + total)
            if dest:
                dests.add(str(dest))
        ev.corrected_field_count += len(carton.corrections)
    ev.document_effective_totals = doc_totals
    ev.destinations = sorted(dests)

    for header in review.headers:
        if header.excluded:
            ev.excluded_documents += 1
        else:
            ev.included_documents += 1
        ev.corrected_field_count += len(header.corrections)

    # --- issue resolution --------------------------------------------------------
    _resolve_issues(result, review, ev, headers, cartons,
                    doc_excluded, carton_excluded)

    # --- approval gate (Step 10) -------------------------------------------------
    problems = ev.approval_problems
    if ev.included_documents == 0:
        problems.append("No included document remains.")
    if ev.included_cartons == 0:
        problems.append("No included carton remains.")
    if ev.included_lines == 0:
        problems.append("No included line remains.")
    for header in review.headers:
        if header.excluded:
            continue
        if not (header.effective("to_location_code") or "").strip():
            problems.append(f"{header.source_file}: destination code "
                            "(To Loc.) is required.")
        if not (header.effective("delivery_note_number") or "").strip():
            problems.append(f"{header.source_file}: delivery-note number "
                            "(D/N#) is required.")
    for carton in review.cartons:
        if carton_excluded(carton):
            continue
        if not (carton.effective("original_carton_number") or "").strip():
            problems.append(f"{carton.source_file} p{carton.source_pages}: "
                            "carton number is required.")
        header = headers.get(carton.document_id)
        dest = (carton.effective("destination_code")
                or (header.effective("to_location_code") if header else None))
        if not (dest or "").strip():
            problems.append(f"Carton {carton.entity_id}: destination code "
                            "is required.")
    for line_ev in ev.lines.values():
        if line_ev.effective_excluded:
            continue
        if line_ev.quantity is None:
            problems.append(f"Line {line_ev.line_id}: positive integer "
                            "quantity required.")
        if not line_ev.lookup_ready:
            problems.append(f"Line {line_ev.line_id}: needs a valid EAN or "
                            "Item+Color+Size.")
    if ev.unresolved_blocking:
        problems.append(f"{len(ev.unresolved_blocking)} unresolved blocking "
                        "issue(s) remain.")
    return ev


def _resolve_issues(result, review, ev, headers, cartons,
                    doc_excluded, carton_excluded) -> None:
    """Deterministic Build 2 issue resolution against effective values."""
    line_by_ref: dict[tuple, list[ReviewedTransferLine]] = {}
    for ln in review.lines:
        key = (ln.upload_sequence, ln.original.get("original_carton_number"),
               ln.original.get("source_sequence_number"))
        line_by_ref.setdefault(key, []).append(ln)

    for doc in result.documents:
        doc_id = f"D{doc.upload_sequence:03d}"
        header = headers.get(doc_id)
        excluded_doc = doc_excluded(doc_id)
        doc_cartons = [c for c in review.cartons if c.document_id == doc_id]

        for issue in doc.issues:
            resolved, downgraded = _issue_state(
                issue, doc, doc_id, header, doc_cartons, review, ev,
                line_by_ref, excluded_doc, carton_excluded)
            if resolved:
                ev.resolved_issue_count += 1
                continue
            record = {"code": issue.code, "severity": issue.severity,
                      "source_file": issue.source_file,
                      "source_page": issue.source_page,
                      "carton": issue.carton, "line_ref": issue.line_ref,
                      "message": issue.message}
            if issue.severity == SEV_ERROR and not downgraded:
                ev.unresolved_blocking.append(record)
            else:
                ev.unresolved_warnings.append(record)


def _issue_state(issue, doc, doc_id, header, doc_cartons, review, ev,
                 line_by_ref, excluded_doc, carton_excluded):
    """(resolved, downgraded_to_warning) for one Build 2 issue."""
    code = issue.code

    def carton_for_issue():
        return next((c for c in doc_cartons
                     if c.original.get("original_carton_number")
                     == issue.carton
                     or (issue.carton is None
                         and issue.source_page in c.source_pages)), None)

    def lines_for_issue():
        return line_by_ref.get(
            (doc.upload_sequence, issue.carton, issue.line_ref), [])

    # Document-level structural problems: only exclusion clears them.
    if code in (UNRECOGNIZED_DOCUMENT, UNREADABLE_PAGE, OCR_UNAVAILABLE,
                DOCUMENT_EXTRACTION_FAILED):
        return excluded_doc, False
    if code == NO_ITEM_LINES:
        has_lines = any(not ev.lines[lid].effective_excluded
                        for c in doc_cartons for lid in c.line_ids
                        if lid in ev.lines)
        return excluded_doc or has_lines, False

    if code in (MISSING_DESTINATION, AMBIGUOUS_DESTINATION):
        if excluded_doc:
            return True, False
        header_dest = (header.effective("to_location_code")
                       if header else None)
        all_have = all(
            (c.effective("destination_code") or header_dest)
            for c in doc_cartons if not carton_excluded(c))
        return bool(header_dest) and all_have, False

    if code == MISSING_DELIVERY_NOTE_NO:
        if excluded_doc:
            return True, False
        return bool((header.effective("delivery_note_number") or "").strip()
                    if header else False), False

    if code == MISSING_CARTON_NO:
        carton = carton_for_issue()
        if carton is None:
            return excluded_doc, False
        if carton_excluded(carton):
            return True, False
        return bool((carton.effective("original_carton_number")
                     or "").strip()), False

    if code in (MISSING_ITEM_IDENTIFIER, MALFORMED_ITEM_ROW):
        lines = lines_for_issue()
        if not lines:
            return excluded_doc, False
        return all(ev.lines[ln.entity_id].effective_excluded
                   or (ev.lines[ln.entity_id].lookup_ready
                       and ev.lines[ln.entity_id].quantity is not None)
                   for ln in lines), False

    if code == INVALID_EAN:
        lines = lines_for_issue()
        if not lines:
            return excluded_doc, False
        return all(ev.lines[ln.entity_id].effective_excluded
                   or valid_ean(ln.effective("ean"))
                   or ev.lines[ln.entity_id].lookup_ready
                   for ln in lines), False

    if code == MISSING_COLOR:
        lines = lines_for_issue()
        return bool(lines) and all(
            ev.lines[ln.entity_id].effective_excluded
            or ln.effective("color_code") for ln in lines), False

    if code == MISSING_SIZE:
        lines = lines_for_issue()
        return bool(lines) and all(
            ev.lines[ln.entity_id].effective_excluded
            or ln.effective("size_code") for ln in lines), False

    if code == INVALID_QUANTITY:
        lines = lines_for_issue()
        if not lines:
            return excluded_doc, False
        return all(ev.lines[ln.entity_id].effective_excluded
                   or ev.lines[ln.entity_id].quantity is not None
                   for ln in lines), False

    if code == INVALID_RETAIL_PRICE:
        lines = lines_for_issue()
        if not lines:
            return excluded_doc, False
        return all(ev.lines[ln.entity_id].effective_excluded
                   or _valid_price(ln.effective("retail_price"))
                   for ln in lines), False

    if code == CARTON_TOTAL_MISMATCH:
        carton = carton_for_issue()
        if carton is None:
            return excluded_doc, False
        if carton_excluded(carton):
            return True, False
        printed = carton.original.get("printed_carton_total")
        if printed is None:
            return False, True              # unreadable printed -> warning
        return (ev.carton_effective_totals.get(carton.entity_id)
                == printed), False

    if code == DOCUMENT_TOTAL_MISMATCH:
        if excluded_doc:
            return True, False
        printed = doc.printed_grand_total
        if printed is None:
            return False, True
        return ev.document_effective_totals.get(doc_id, 0) == printed, False

    if code == PRINTED_TOTAL_UNREADABLE:
        return False, True                   # informational; cannot be fixed
    # unknown/future codes: keep visible at original severity
    return False, False


# --- persistence ------------------------------------------------------------------

def save_review(job_id: str, review: TransferReviewResult, *,
                expected_updated_at: str | None = None) -> TransferReviewResult:
    """Atomic write. `expected_updated_at` (when given) must match the
    artifact on disk - a concurrent save from a stale form is rejected
    instead of silently overwritten."""
    path = review_path(job_id)
    if expected_updated_at is not None and path.is_file():
        current = load_review(job_id, check_stale=False)
        if current is not None and current.updated_at != expected_updated_at:
            raise JobError("The review was changed by another save; reload "
                           "the page and reapply your edits.")
    review.updated_at = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{REVIEW_NAME}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(review.as_dict(), indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return review


def load_review(job_id: str, *,
                check_stale: bool = True) -> TransferReviewResult | None:
    """Reload the persisted review. Malformed artifacts return None (the
    extraction data is untouched and a fresh review can be built). When the
    extraction checksum no longer matches, the review is marked STALE."""
    try:
        path = review_path(job_id)
    except JobError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        review = TransferReviewResult.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if check_stale and review.status != REVIEW_STALE:
        current = extraction_checksum(job_id)
        if current is not None and current != review.extraction_checksum:
            review.status = REVIEW_STALE
            save_review(job_id, review)
    return review


def archive_stale_review(job_id: str) -> Path | None:
    """Move a stale review aside (audit-preserving) so a fresh one can be
    built. Never deletes anything."""
    path = review_path(job_id)
    if not path.is_file():
        return None
    stamp = utc_now().replace(":", "").replace("-", "").split(".")[0]
    target = path.with_name(f"review-stale-{stamp}.json")
    counter = 0
    while target.exists():
        counter += 1
        target = path.with_name(f"review-stale-{stamp}-{counter}.json")
    os.replace(path, target)
    return target


def get_or_create_review(job_id: str) -> TransferReviewResult | None:
    """The page entry point: load the saved review, or build a fresh one
    from the current extraction. A stale review is archived (not lost) and
    rebuilt only when the caller asks via rebuild_review()."""
    review = load_review(job_id)
    if review is not None:
        return review
    result = extraction_mod.load_result(job_id)
    checksum = extraction_checksum(job_id)
    if result is None or checksum is None:
        return None
    review = build_initial_review(result, checksum)
    return save_review(job_id, review)


def rebuild_review(job_id: str) -> TransferReviewResult | None:
    """Archive whatever exists and build a fresh review from the current
    extraction result."""
    archive_stale_review(job_id)
    return get_or_create_review(job_id)


# --- editor-row application (UI convention, kept here so it is testable) ----------

CLEAR_TOKEN = "<clear>"


def apply_editor_rows(review: TransferReviewResult, entity_type: str,
                      rows: list[dict], fields: tuple[str, ...]) -> int:
    """Apply one edited table back onto the review model. Convention
    (documented in the page): a cell equal to the current effective value
    is unchanged; equal to the original removes the correction; the literal
    token `<clear>` deliberately clears the value; an EMPTY cell is ignored
    (the value is restored on rerender) so nothing is erased by accident.
    Exclusion changes require a reason. Returns the number of changes."""
    changed = 0
    for row in rows:
        entity_id = row.get("entity_id")
        entity = review.entity_by_id(entity_type, entity_id)
        if entity is None:
            continue
        for fld in fields:
            if fld not in row:
                continue
            cell = row[fld]
            cell = "" if cell is None else str(cell)
            if cell.strip() == "":
                continue                       # empty edit: never clears
            if cell.strip() == CLEAR_TOKEN:
                if apply_correction(review, entity_type, entity_id, fld,
                                    CLEAR):
                    changed += 1
                continue
            effective = entity.effective(fld)
            if cell.strip() == ("" if effective is None else str(effective)):
                continue                       # unchanged
            if apply_correction(review, entity_type, entity_id, fld, cell):
                changed += 1
        if "excluded" in row:
            want = bool(row["excluded"])
            reason = (row.get("exclusion_reason") or "").strip() or None
            if want != entity.excluded or (
                    want and reason and reason != entity.exclusion_reason):
                if want and not reason:
                    raise JobError(
                        f"{entity_type} {entity_id}: excluding requires a "
                        "reason in the 'Exclusion reason' column.")
                if set_exclusion(review, entity_type, entity_id, want,
                                 reason):
                    changed += 1
    return changed


# --- approval + state transitions -------------------------------------------------

def begin_review(job_id: str) -> None:
    job = jobs.load_transfer_job(job_id)
    if job is not None and job.status in (JOB_EXTRACTED,
                                          JOB_EXTRACTED_WITH_ISSUES):
        jobs.update_job_status(job_id, JOB_REVIEW_IN_PROGRESS)


def reopen_review(job_id: str) -> None:
    job = jobs.load_transfer_job(job_id)
    if job is not None and job.status == JOB_READY_FOR_PRODUCT_LOOKUP:
        jobs.update_job_status(job_id, JOB_REVIEW_IN_PROGRESS)
        review = load_review(job_id, check_stale=False)
        if review is not None and review.status == REVIEW_APPROVED:
            review.status = REVIEW_IN_PROGRESS
            save_review(job_id, review)


def reject_review(job_id: str, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise JobError("Rejecting a review requires a reason.")
    review = load_review(job_id, check_stale=False)
    if review is None:
        raise JobError("No review exists to reject.")
    review.status = REVIEW_REJECTED_STATUS
    review.changes.append(ReviewChange(
        entity_type="review", entity_id=review.job_id, field="status",
        original_value=None, previous_corrected=None,
        new_corrected=f"rejected: {reason}", changed_at=utc_now()))
    save_review(job_id, review)
    jobs.update_job_status(job_id, JOB_REVIEW_REJECTED)


def approve_review(job_id: str) -> TransferReviewResult:
    """Approve for product lookup. Every Step 10 gate is re-checked here -
    the UI button state is never trusted. No API is called."""
    review = load_review(job_id)          # staleness re-checked on load
    if review is None:
        raise JobError("No saved review exists.")
    if review.status == REVIEW_STALE:
        raise JobError("The extraction result changed after this review was "
                       "created; the review is stale and cannot be "
                       "approved. Rebuild the review first.")
    current = extraction_checksum(job_id)
    if current is None or current != review.extraction_checksum:
        raise JobError("Review does not match the current extraction result.")
    result = extraction_mod.load_result(job_id)
    if result is None:
        raise JobError("Extraction result is missing.")
    ev = evaluate(result, review)
    if not ev.can_approve:
        raise JobError("Review cannot be approved: "
                       + " ".join(ev.approval_problems[:5]))
    job = jobs.load_transfer_job(job_id)
    if job is not None and job.status in (JOB_EXTRACTED,
                                          JOB_EXTRACTED_WITH_ISSUES):
        jobs.update_job_status(job_id, JOB_REVIEW_IN_PROGRESS)
    review.status = REVIEW_APPROVED
    review.changes.append(ReviewChange(
        entity_type="review", entity_id=review.job_id, field="status",
        original_value=None, previous_corrected=None,
        new_corrected=REVIEW_APPROVED, changed_at=utc_now()))
    save_review(job_id, review)
    if job is not None and job.status != JOB_READY_FOR_PRODUCT_LOOKUP:
        jobs.update_job_status(job_id, JOB_READY_FOR_PRODUCT_LOOKUP)
    return review
