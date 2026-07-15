"""Benchmark manifest + ground-truth loading and STRICT validation (M6).

Ground truth is human-authored. This module loads it, validates it hard, and
NEVER trusts model output as truth. Validation errors identify case_id, file,
field path, and a SAFE category - they never echo full invoice field values
(only the offending field's name and a value CATEGORY like "not a valid
decimal"), so a validation traceback can't leak invoice contents.

Field model (approved M6 design):

  * SCORED fields are those the extractor workbook can actually produce. Ground
    truth for them is compared and counted in denominators.
  * NOT_EXTRACTABLE fields are approved ground-truth fields the extractor has
    no column for (e.g. ship_to, hs_code). They are VALIDATED but excluded
    from every scored denominator and surfaced per case as
    not_extractable_fields - never silently counted correct.
  * `tax` is a ground-truth alias for the extractor's `tax_amount`; supplying
    BOTH in one ground-truth invoice is rejected.
  * ignored_fields (human intentionally excludes/unavailable) is DISTINCT from
    not_extractable_fields (extractor has no such output) - both drop the
    field from denominators but for different, separately reported reasons.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

# --- Approved field vocabulary ------------------------------------------------
# Ground-truth HEADER names the extractor can score, mapped to the extractor's
# own field name (identity except the `tax` -> `tax_amount` alias).
SCORED_HEADER_FIELDS: dict[str, str] = {
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "seller_name": "seller_name",
    "seller_address": "seller_address",
    "buyer_name": "buyer_name",
    "buyer_address": "buyer_address",
    "currency": "currency",
    "subtotal": "subtotal",
    "tax": "tax_amount",
    "total_amount": "total_amount",
    "po_number": "po_number",
    "reference": "reference",
}
# Approved ground-truth HEADER fields with no extractor column (validated,
# never scored, surfaced as not_extractable_fields).
NOT_EXTRACTABLE_HEADER_FIELDS = frozenset(
    {"ship_to", "bill_to", "incoterms", "country_of_origin"}
)
APPROVED_HEADER_FIELDS = frozenset(SCORED_HEADER_FIELDS) | NOT_EXTRACTABLE_HEADER_FIELDS

SCORED_LINE_FIELDS: dict[str, str] = {
    "line_no": "line_no",
    "item_code": "item_code",
    "description": "description",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "amount": "amount",
}
NOT_EXTRACTABLE_LINE_FIELDS = frozenset({"hs_code", "unit", "uom", "currency"})
APPROVED_LINE_FIELDS = frozenset(SCORED_LINE_FIELDS) | NOT_EXTRACTABLE_LINE_FIELDS

# Which scored fields are numeric (Decimal) vs string.
NUMERIC_HEADER_GT_FIELDS = frozenset({"subtotal", "tax", "total_amount"})
NUMERIC_LINE_GT_FIELDS = frozenset({"quantity", "unit_price", "amount"})

DOCUMENT_TYPES = frozenset({
    "text_single_page", "text_multi_page", "vision_single_page",
    "vision_multi_page", "mixed", "malformed", "blank",
})
EXPECTED_OUTCOMES = frozenset({"extracted", "needs_review", "failed"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BenchmarkConfigError(Exception):
    """A benchmark manifest / ground-truth file is invalid.

    Message is SAFE to log/print: it names case_id, file, field path, and a
    value CATEGORY - never a raw invoice field value.
    """


@dataclass(frozen=True)
class GroundTruthCase:
    case_id: str
    source_file: str          # basename as authored in the manifest
    document_type: str
    expected_outcome: str
    ground_truth_path: str    # for diagnostics only
    invoice: dict             # scored + not-extractable header GT (tax kept as-is)
    line_items: list[dict]
    expected_needs_review: bool
    accepted_review_categories: tuple[str, ...]
    ignored_fields: tuple[str, ...]
    field_tolerances: dict[str, Decimal]
    notes: str = ""

    def not_extractable_header_fields(self) -> tuple[str, ...]:
        return tuple(sorted(f for f in self.invoice if f in NOT_EXTRACTABLE_HEADER_FIELDS))

    def not_extractable_line_fields(self) -> tuple[str, ...]:
        present: set[str] = set()
        for item in self.line_items:
            present |= {f for f in item if f in NOT_EXTRACTABLE_LINE_FIELDS}
        return tuple(sorted(present))


@dataclass
class BenchmarkDataset:
    cases: list[GroundTruthCase] = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)
    fuzzy_enabled: bool = False
    fuzzy_threshold: Decimal = Decimal("0.90")


def normalize_basename(source_file: str) -> str:
    """Path-separator-normalized basename, NO casefold / punctuation change.

    'Invoice-A.pdf' and 'invoice-a.pdf' must stay DISTINCT (adjustment 2), so
    this only strips directories and normalizes separators - it never lowers
    case or touches punctuation."""
    return PurePosixLikePath(source_file).name


class PurePosixLikePath:
    """Tiny helper: split a path on BOTH separators so a manifest authored on
    Windows ('a\\b.pdf') and one on POSIX ('a/b.pdf') yield the same basename,
    without importing os-specific path semantics."""

    def __init__(self, raw: str):
        self._raw = raw.replace("\\", "/")

    @property
    def name(self) -> str:
        return self._raw.rsplit("/", 1)[-1]


def _err(case_id: str | None, file: str, field_path: str, category: str) -> BenchmarkConfigError:
    where = f"case {case_id!r}, " if case_id else ""
    return BenchmarkConfigError(f"{where}file {file!r}, field {field_path!r}: {category}")


def _read_json(path: Path, case_id: str | None) -> dict:
    if not path.exists():
        raise _err(case_id, str(path), "<file>", "ground-truth file not found")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # exc.msg/pos are structural (no invoice content); safe to include.
        raise _err(case_id, str(path), "<json>",
                   f"malformed JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(raw, dict):
        raise _err(case_id, str(path), "<root>", "top level must be a JSON object")
    return raw


def _validate_decimal(value, case_id, file, field_path) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _err(case_id, file, field_path, "not a valid decimal (wrong type)")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise _err(case_id, file, field_path, "not a valid decimal") from None


def _validate_date(value, case_id, file, field_path) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise _err(case_id, file, field_path, "not an ISO date (expected YYYY-MM-DD)")
    # Reject impossible calendar dates without inferring/repairing.
    from datetime import date
    y, m, d = (int(p) for p in value.split("-"))
    try:
        date(y, m, d)
    except ValueError:
        raise _err(case_id, file, field_path, "not a valid calendar date") from None
    return value


def _validate_invoice_block(block, case_id, file) -> dict:
    if not isinstance(block, dict):
        raise _err(case_id, file, "invoice", "must be a JSON object")
    if "tax" in block and "tax_amount" in block:
        raise _err(case_id, file, "invoice",
                   "supplies both 'tax' and its alias 'tax_amount' (use only 'tax')")
    out: dict = {}
    for name, value in block.items():
        fp = f"invoice.{name}"
        if name == "tax_amount":
            raise _err(case_id, file, fp,
                       "use ground-truth alias 'tax', not extractor name 'tax_amount'")
        if name not in APPROVED_HEADER_FIELDS:
            raise _err(case_id, file, fp, "unsupported header field")
        if value is None:
            out[name] = None
        elif name in NUMERIC_HEADER_GT_FIELDS:
            out[name] = _validate_decimal(value, case_id, file, fp)
        elif name == "invoice_date":
            out[name] = _validate_date(value, case_id, file, fp)
        elif name in NOT_EXTRACTABLE_HEADER_FIELDS:
            out[name] = value if isinstance(value, str) else str(value)
        else:
            if not isinstance(value, str):
                raise _err(case_id, file, fp, "expected a string value")
            out[name] = value
    return out


def _validate_line_items(items, case_id, file) -> list[dict]:
    if not isinstance(items, list):
        raise _err(case_id, file, "line_items", "must be a JSON array")
    out: list[dict] = []
    seen_line_no: set[str] = set()
    seen_item_code: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise _err(case_id, file, f"line_items[{i}]", "must be a JSON object")
        row: dict = {}
        for name, value in item.items():
            fp = f"line_items[{i}].{name}"
            if name not in APPROVED_LINE_FIELDS:
                raise _err(case_id, file, fp, "unsupported line-item field")
            if value is None:
                row[name] = None
            elif name in NUMERIC_LINE_GT_FIELDS:
                row[name] = _validate_decimal(value, case_id, file, fp)
            else:
                row[name] = value if isinstance(value, str) else str(value)
        # Duplicate identifier detection: a repeated non-null line_no or
        # item_code would make tier-1/tier-2 matching ambiguous, so reject it
        # at authoring time rather than silently degrade matching.
        ln = row.get("line_no")
        if ln is not None and ln != "":
            key = _norm_id(ln)
            if key in seen_line_no:
                raise _err(case_id, file, f"line_items[{i}].line_no",
                           "duplicate line_no would make matching ambiguous")
            seen_line_no.add(key)
        ic = row.get("item_code")
        if ic is not None and ic != "":
            key = _norm_id(ic)
            if key in seen_item_code:
                raise _err(case_id, file, f"line_items[{i}].item_code",
                           "duplicate item_code would make matching ambiguous")
            seen_item_code.add(key)
        out.append(row)
    return out


def _norm_id(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _validate_tolerances(block, case_id, file) -> dict[str, Decimal]:
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise _err(case_id, file, "field_tolerances", "must be a JSON object")
    out: dict[str, Decimal] = {}
    scored = set(SCORED_HEADER_FIELDS) | set(SCORED_LINE_FIELDS)
    for name, value in block.items():
        if name not in scored:
            raise _err(case_id, file, f"field_tolerances.{name}",
                       "tolerance for an unscored/unknown field")
        tol = _validate_decimal(value, case_id, file, f"field_tolerances.{name}")
        if tol < 0:
            raise _err(case_id, file, f"field_tolerances.{name}", "tolerance must be non-negative")
        out[name] = tol
    return out


def load_ground_truth(path: Path, expected_case_id: str) -> GroundTruthCase:
    """Load + validate ONE ground-truth JSON. `expected_case_id` comes from the
    manifest; a mismatch inside the file is rejected."""
    raw = _read_json(path, expected_case_id)
    file = str(path)
    cid = raw.get("case_id")
    if cid is not None and cid != expected_case_id:
        raise _err(expected_case_id, file, "case_id",
                   "case_id inside file does not match the manifest entry")

    invoice = _validate_invoice_block(raw.get("invoice", {}), expected_case_id, file)
    line_items = _validate_line_items(raw.get("line_items", []), expected_case_id, file)

    enr = raw.get("expected_needs_review", False)
    if not isinstance(enr, bool):
        raise _err(expected_case_id, file, "expected_needs_review", "must be true or false")

    ignored = raw.get("ignored_fields", [])
    if not isinstance(ignored, list) or not all(isinstance(x, str) for x in ignored):
        raise _err(expected_case_id, file, "ignored_fields", "must be an array of field names")

    cats = raw.get("accepted_review_categories", [])
    if not isinstance(cats, list) or not all(isinstance(x, str) for x in cats):
        raise _err(expected_case_id, file, "accepted_review_categories",
                   "must be an array of category strings")

    tolerances = _validate_tolerances(raw.get("field_tolerances"), expected_case_id, file)

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise _err(expected_case_id, file, "notes", "must be a string")

    return GroundTruthCase(
        case_id=expected_case_id,
        source_file="",  # filled in by the manifest loader
        document_type="",
        expected_outcome="",
        ground_truth_path=file,
        invoice=invoice,
        line_items=line_items,
        expected_needs_review=enr,
        accepted_review_categories=tuple(cats),
        ignored_fields=tuple(ignored),
        field_tolerances=tolerances,
        notes=notes,
    )


def load_manifest(manifest_path: Path) -> BenchmarkDataset:
    """Load + validate the manifest and every referenced ground-truth file.
    All-or-nothing: any error aborts before scoring (no half-scored state)."""
    manifest_path = Path(manifest_path)
    raw = _read_json(manifest_path, None)
    entries = raw.get("cases")
    if not isinstance(entries, list) or not entries:
        raise _err(None, str(manifest_path), "cases", "must be a non-empty array")

    base = manifest_path.parent
    cases: list[GroundTruthCase] = []
    seen_ids: set[str] = set()
    seen_entry_keys: set[tuple[str, str]] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _err(None, str(manifest_path), f"cases[{i}]", "must be a JSON object")
        cid = entry.get("case_id")
        if not isinstance(cid, str) or not cid.strip():
            raise _err(None, str(manifest_path), f"cases[{i}].case_id",
                       "missing/empty case_id")
        if cid in seen_ids:
            raise _err(cid, str(manifest_path), "case_id", "duplicate case_id")
        seen_ids.add(cid)

        src = entry.get("source_file")
        if not isinstance(src, str) or not src.strip():
            raise _err(cid, str(manifest_path), "source_file", "missing/empty source_file")
        src = normalize_basename(src)

        dtype = entry.get("document_type")
        if dtype not in DOCUMENT_TYPES:
            raise _err(cid, str(manifest_path), "document_type",
                       f"must be one of {sorted(DOCUMENT_TYPES)}")
        outcome = entry.get("expected_outcome")
        if outcome not in EXPECTED_OUTCOMES:
            raise _err(cid, str(manifest_path), "expected_outcome",
                       f"must be one of {sorted(EXPECTED_OUTCOMES)}")

        entry_key = (cid, src)
        if entry_key in seen_entry_keys:
            raise _err(cid, str(manifest_path), "cases", "duplicate manifest entry")
        seen_entry_keys.add(entry_key)

        gt_rel = entry.get("ground_truth")
        if not isinstance(gt_rel, str) or not gt_rel.strip():
            raise _err(cid, str(manifest_path), "ground_truth", "missing ground_truth path")
        gt_case = load_ground_truth(base / gt_rel, cid)

        cases.append(GroundTruthCase(
            case_id=cid,
            source_file=src,
            document_type=dtype,
            expected_outcome=outcome,
            ground_truth_path=gt_case.ground_truth_path,
            invoice=gt_case.invoice,
            line_items=gt_case.line_items,
            expected_needs_review=gt_case.expected_needs_review,
            accepted_review_categories=gt_case.accepted_review_categories,
            ignored_fields=gt_case.ignored_fields,
            field_tolerances=gt_case.field_tolerances,
            notes=gt_case.notes,
        ))

    thresholds = raw.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise _err(None, str(manifest_path), "thresholds", "must be a JSON object")

    cases.sort(key=lambda c: c.case_id)  # deterministic ordering
    return BenchmarkDataset(cases=cases, thresholds=thresholds)


def load_thresholds(path: Path) -> dict:
    raw = _read_json(Path(path), None)
    if not isinstance(raw, dict):
        raise _err(None, str(path), "<root>", "thresholds file must be a JSON object")
    return raw
