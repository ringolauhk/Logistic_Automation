"""Deterministic aggregation of per-route extraction results.

The PoC assumes ONE INVOICE PER PDF. When a mixed PDF is extracted via two
routes (text pages + vision pages), the route results are merged with these
documented rules:

1. Routes are ordered by their first contributing page; line items are
   concatenated in that order (within-route order is document order, enforced
   by the prompt), so page order is preserved.
2. Repeated table-header rows are excluded at the prompt level; additionally,
   a line item is dropped as a duplicate ONLY on strong evidence: all four
   fields non-null and exactly equal to an item already merged from an
   earlier route.
3. Header fields prefer non-null values. When two non-null values conflict
   (after whitespace/case normalization for strings, numeric equality for
   amounts), the choice is deterministic but NEVER silent - the invoice is
   flagged needs_review with the field name and both values:
     - monetary fields (subtotal, tax_amount, total_amount): the value from
       the route containing the LAST meaningful page wins (document totals
       normally appear at the end), but the conflict is still flagged;
     - all other fields: the value from the FIRST route (page order) wins.
4. A conflicting invoice_number is treated as a likely multi-invoice PDF and
   flagged explicitly - multi-invoice segmentation is NOT implemented.
"""

from dataclasses import dataclass, field

from invoice_extractor.pdf_utils import format_page_ranges
from invoice_extractor.schema import (
    HEADER_FIELDS,
    NUMERIC_HEADER_FIELDS,
    Invoice,
    LineItem,
)


@dataclass
class RouteResult:
    route: str  # "text" | "vision"
    pages: list[int]  # 1-based pages this route covered
    invoice: Invoice
    provider: str  # "gemini" | "claude"
    model: str


@dataclass
class AggregationOutcome:
    invoice: Invoice
    # (field_name, human-readable detail incl. values). Log field names only;
    # the detail (which contains invoice data) belongs in the Excel output.
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _norm(value):
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _item_key(item: LineItem):
    return (_norm(item.description), item.quantity, item.unit_price, item.amount)


def _is_fully_specified(item: LineItem) -> bool:
    return all(
        v is not None
        for v in (item.description, item.quantity, item.unit_price, item.amount)
    )


def aggregate(routes: list[RouteResult]) -> AggregationOutcome:
    if not routes:
        raise ValueError("aggregate() requires at least one route result")
    if len(routes) == 1:
        return AggregationOutcome(invoice=routes[0].invoice)

    ordered = sorted(routes, key=lambda r: min(r.pages))
    last_route = max(routes, key=lambda r: max(r.pages))
    conflicts: list[tuple[str, str]] = []
    notes: list[str] = []
    data: dict = {}

    for fld in HEADER_FIELDS:
        present = [
            (r, getattr(r.invoice, fld))
            for r in ordered
            if getattr(r.invoice, fld) is not None
        ]
        if not present:
            data[fld] = None
            continue
        distinct = {_norm(v) for _, v in present}
        if len(distinct) == 1:
            data[fld] = present[0][1]
            continue
        # Conflict: choose deterministically, flag always.
        if fld in NUMERIC_HEADER_FIELDS:
            from_last = getattr(last_route.invoice, fld)
            chosen = from_last if from_last is not None else present[-1][1]
        else:
            chosen = present[0][1]
        detail = " vs ".join(
            f"'{v}' (pages {format_page_ranges(r.pages)}, {r.provider})"
            for r, v in present
        ) + f"; kept '{chosen}'"
        conflicts.append((fld, detail))
        data[fld] = chosen
        if fld == "invoice_number":
            notes.append(
                "possible multiple invoices in one PDF (invoice_number differs "
                "across pages); multi-invoice segmentation is not supported - "
                "split the file and re-run"
            )

    merged_items: list[LineItem] = list(ordered[0].invoice.line_items)
    seen = {_item_key(it) for it in merged_items if _is_fully_specified(it)}
    dropped = 0
    for route in ordered[1:]:
        for item in route.invoice.line_items:
            if _is_fully_specified(item) and _item_key(item) in seen:
                dropped += 1
                continue
            merged_items.append(item)
            if _is_fully_specified(item):
                seen.add(_item_key(item))
    if dropped:
        notes.append(f"dropped {dropped} exact-duplicate line item(s) across routes")

    data["line_items"] = merged_items
    return AggregationOutcome(invoice=Invoice(**data), conflicts=conflicts, notes=notes)
