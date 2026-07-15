"""Deterministic invoice + line-item matching for the benchmark (M6).

Line matching runs in fixed tiers; fuzzy description matching is OFF by
default and, even when enabled, never overrides a numeric conflict and never
forces a tie - ties are reported as ambiguous. All comparisons are Decimal,
never binary float.
"""

import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

DEFAULT_AMOUNT_TOLERANCE = Decimal("0.01")
DEFAULT_FUZZY_THRESHOLD = Decimal("0.90")
_FUZZY_TIE_MARGIN = Decimal("0.02")  # top-2 within this => ambiguous, not forced


# --- Normalization ------------------------------------------------------------

def norm_string(value) -> str | None:
    """NFKC + trim + collapse internal whitespace + casefold. For descriptions
    and case-insensitive header fields (names/addresses/currency)."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join(text.split())
    return text.casefold()


def norm_identifier(value) -> str | None:
    """Trim + casefold only - NO space/hyphen removal (distinct identifiers
    must stay distinct). For invoice_number/po_number/reference/item_code/
    line_no."""
    if value is None:
        return None
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def norm_date(value) -> str | None:
    """Compare on the ISO string as-is (ground truth is validated to YYYY-MM-DD;
    the workbook stores the extractor's normalized date). No year inference."""
    if value is None:
        return None
    return str(value).strip()


def as_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


# --- Line matching ------------------------------------------------------------

@dataclass(frozen=True)
class LinePair:
    expected_index: int | None   # None for an extra actual line
    actual_index: int | None     # None for a missing expected line
    method: str                  # line_no|item_code|composite|fuzzy|missing|extra|ambiguous
    confidence: Decimal          # 1.0 for exact tiers; SequenceMatcher ratio for fuzzy


@dataclass
class LineMatchOutcome:
    pairs: list[LinePair] = field(default_factory=list)

    @property
    def matched(self) -> list[LinePair]:
        return [p for p in self.pairs
                if p.method in ("line_no", "item_code", "composite", "fuzzy")]

    @property
    def missing(self) -> list[LinePair]:
        return [p for p in self.pairs if p.method == "missing"]

    @property
    def extra(self) -> list[LinePair]:
        return [p for p in self.pairs if p.method == "extra"]

    @property
    def ambiguous(self) -> list[LinePair]:
        return [p for p in self.pairs if p.method == "ambiguous"]


def _composite_key(item):
    desc = norm_string(item.get("description"))
    qty = as_decimal(item.get("quantity"))
    unit = as_decimal(item.get("unit_price"))
    amt = as_decimal(item.get("amount"))
    if None in (desc, qty, unit, amt):
        return None
    return (desc, qty, unit, amt)


def _numeric_compatible(exp: dict, act: dict, amount_tolerance: Decimal) -> bool:
    exp_amt, act_amt = as_decimal(exp.get("amount")), as_decimal(act.get("amount"))
    if exp_amt is not None and act_amt is not None:
        if abs(exp_amt - act_amt) > amount_tolerance:
            return False
    exp_qty, act_qty = as_decimal(exp.get("quantity")), as_decimal(act.get("quantity"))
    if exp_qty is not None and act_qty is not None:
        if exp_qty != act_qty:
            return False
    return True


def match_lines(
    expected: list[dict], actual: list[dict], *,
    amount_tolerance: Decimal = DEFAULT_AMOUNT_TOLERANCE,
    fuzzy_enabled: bool = False,
    fuzzy_threshold: Decimal = DEFAULT_FUZZY_THRESHOLD,
) -> LineMatchOutcome:
    exp_free = set(range(len(expected)))
    act_free = set(range(len(actual)))
    pairs: list[LinePair] = []

    def run_exact_tier(key_fn, method):
        # Unique-key maps restricted to still-free indices on each side.
        exp_keys = _restricted_unique_map(expected, exp_free, key_fn)
        act_keys = _restricted_unique_map(actual, act_free, key_fn)
        for key, ei in sorted(exp_keys.items()):
            ai = act_keys.get(key)
            if ai is not None and ei in exp_free and ai in act_free:
                pairs.append(LinePair(ei, ai, method, Decimal("1")))
                exp_free.discard(ei)
                act_free.discard(ai)

    run_exact_tier(lambda it: norm_identifier(it.get("line_no")), "line_no")
    run_exact_tier(lambda it: norm_identifier(it.get("item_code")), "item_code")
    run_exact_tier(_composite_key, "composite")

    if fuzzy_enabled:
        _fuzzy_tier(expected, actual, exp_free, act_free, pairs,
                    amount_tolerance, fuzzy_threshold)

    for ei in sorted(exp_free):
        pairs.append(LinePair(ei, None, "missing", Decimal("0")))
    for ai in sorted(act_free):
        pairs.append(LinePair(None, ai, "extra", Decimal("0")))
    return LineMatchOutcome(pairs=pairs)


def _restricted_unique_map(items, free: set[int], key_fn) -> dict:
    counts: dict = {}
    first: dict = {}
    for idx in sorted(free):
        key = key_fn(items[idx])
        if key is None or key == "":
            continue
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, idx)
    return {k: first[k] for k, n in counts.items() if n == 1}


def _fuzzy_tier(expected, actual, exp_free, act_free, pairs,
                amount_tolerance, fuzzy_threshold):
    for ei in sorted(exp_free):
        exp_desc = norm_string(expected[ei].get("description"))
        if not exp_desc:
            continue
        scored = []
        for ai in sorted(act_free):
            if not _numeric_compatible(expected[ei], actual[ai], amount_tolerance):
                continue
            act_desc = norm_string(actual[ai].get("description"))
            if not act_desc:
                continue
            ratio = Decimal(str(round(
                SequenceMatcher(None, exp_desc, act_desc).ratio(), 6)))
            if ratio >= fuzzy_threshold:
                scored.append((ratio, ai))
        if not scored:
            continue
        scored.sort(key=lambda t: (-t[0], t[1]))
        best_ratio, best_ai = scored[0]
        if len(scored) >= 2 and (best_ratio - scored[1][0]) <= _FUZZY_TIE_MARGIN:
            # Near-tie: report ambiguous, do NOT force a choice.
            pairs.append(LinePair(ei, None, "ambiguous", best_ratio))
            continue
        pairs.append(LinePair(ei, best_ai, "fuzzy", best_ratio))
        exp_free.discard(ei)
        act_free.discard(best_ai)


# --- Invoice (source_file) matching -------------------------------------------

@dataclass
class InvoiceMatch:
    case_id: str
    source_file: str
    status: str          # matched | missing_result | duplicate_result
    actual_row: dict | None = None
    duplicate_count: int = 0


def match_invoices(cases, actual_by_source: dict[str, list[dict]]):
    """Match each ground-truth case to at most one workbook Invoices row by
    EXACT basename (no casefold, no punctuation change). Returns
    (matches, extra_source_files)."""
    matches: list[InvoiceMatch] = []
    used: set[str] = set()
    for case in cases:
        rows = actual_by_source.get(case.source_file, [])
        used.add(case.source_file)
        if not rows:
            matches.append(InvoiceMatch(case.case_id, case.source_file, "missing_result"))
        elif len(rows) > 1:
            matches.append(InvoiceMatch(
                case.case_id, case.source_file, "duplicate_result",
                duplicate_count=len(rows)))
        else:
            matches.append(InvoiceMatch(
                case.case_id, case.source_file, "matched", actual_row=rows[0]))
    extra = sorted(s for s in actual_by_source if s not in used)
    return matches, extra
