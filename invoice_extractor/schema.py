"""Unified invoice schema: strict Pydantic models, Decimal coercion, validation.

Raw LLM JSON is first *filtered* to known schema keys (LLMs occasionally emit
commentary keys; hard-failing on those would waste a paid call - this is the
documented reason the raw payload itself is not validated with extra="forbid").
The filtered dict is then validated by Pydantic models that DO forbid extras,
so any programming error upstream fails loudly.
"""

import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

HEADER_FIELDS = [
    "invoice_number",
    "po_number",
    "reference",
    "invoice_date",
    "currency",
    "seller_name",
    "seller_address",
    "buyer_name",
    "buyer_address",
    "subtotal",
    "tax_amount",
    "total_amount",
    "payment_terms",
]

NUMERIC_HEADER_FIELDS = ["subtotal", "tax_amount", "total_amount"]

LINE_ITEM_FIELDS = ["description", "quantity", "unit_price", "amount"]
NUMERIC_LINE_ITEM_FIELDS = ["quantity", "unit_price", "amount"]

# Fields that must be non-null or the LLM call is treated as a hard failure
# (triggers provider fallback / the invoice-level failed/needs_review outcome
# - see check_required below). invoice_number is deliberately NOT here: many
# real commercial/customs invoices have no true invoice number at all, only a
# PO number or other reference (see missing_identifier below for the softer,
# review-only check that covers that case instead).
REQUIRED_FIELDS = [
    "invoice_date",
    "currency",
    "seller_name",
    "total_amount",
]


class ExtractionError(Exception):
    """LLM output was unusable (bad JSON, missing required fields, etc.).

    `message` must never contain raw response/invoice content (it is logged);
    `detail` may carry the raw payload for debug artifacts only.
    """

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = None
    po_number: str | None = None
    reference: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    seller_name: str | None = None
    seller_address: str | None = None
    buyer_name: str | None = None
    buyer_address: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    payment_terms: str | None = None
    line_items: list[LineItem] = []


def empty_invoice() -> Invoice:
    return Invoice()


_NUMBER_CLEAN_RE = re.compile(r"[^\d.,\-]")


def coerce_decimal(value) -> Decimal | None:
    """Coerce LLM output ('1,234.56', '(99.00)', '€1.234,56', 0) to Decimal.

    Zero is preserved as Decimal('0') - never converted to None. Unparseable
    values return None (the original value is kept for diagnostics upstream).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None
    # Accounting-style negatives: (1,234.56)
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = _NUMBER_CLEAN_RE.sub("", s).strip()
    if not s or s == "-":
        return None
    # European style "1.234,56" -> "1234.56"
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = s.replace(",", ".")  # decimal comma: "123,45"
        else:
            s = s.replace(",", "")  # thousands: "12,345"
    try:
        result = Decimal(s)
    except InvalidOperation:
        return None
    return -result if negative and result > 0 else result


_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP"}  # only unambiguous symbols


def normalize_currency(value) -> str | None:
    """Normalize to an uppercase ISO-4217-shaped code without inventing one.

    Unambiguous symbols are mapped; anything unrecognized is kept as-is so
    the reviewer can see what the model actually returned.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[s]
    if len(s) == 3 and s.isalpha():
        return s.upper()
    return s


def _coerce_str(value) -> str | None:
    """Coerce to a stripped string; empty strings normalize to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_invoice(raw) -> Invoice:
    """Map raw (filtered) LLM JSON onto the strict schema, coercing types.

    Unknown keys are dropped (see module docstring); missing keys become None;
    empty strings become None; zero survives as Decimal('0').
    """
    if not isinstance(raw, dict):
        raise ExtractionError(
            f"expected a JSON object, got {type(raw).__name__}"
        )

    data: dict = {}
    for field in HEADER_FIELDS:
        value = raw.get(field)
        if field in NUMERIC_HEADER_FIELDS:
            data[field] = coerce_decimal(value)
        elif field == "currency":
            data[field] = normalize_currency(value)
        else:
            data[field] = _coerce_str(value)

    items: list[LineItem] = []
    raw_items = raw.get("line_items")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            fields = {}
            for field in LINE_ITEM_FIELDS:
                value = item.get(field)
                if field in NUMERIC_LINE_ITEM_FIELDS:
                    fields[field] = coerce_decimal(value)
                else:
                    fields[field] = _coerce_str(value)
            if any(v is not None for v in fields.values()):
                items.append(LineItem(**fields))
    data["line_items"] = items
    return Invoice(**data)


def unknown_keys(raw: dict) -> list[str]:
    """Top-level keys the LLM emitted that are not part of the schema."""
    known = set(HEADER_FIELDS) | {"line_items"}
    return sorted(k for k in raw.keys() if k not in known)


def missing_required_fields(inv: Invoice) -> list[str]:
    return [f for f in REQUIRED_FIELDS if getattr(inv, f) is None]


def check_required(inv: Invoice) -> None:
    """Raise ExtractionError when required fields are missing.

    Called on each provider's output: a failure here is what triggers the
    fallback provider.
    """
    missing = missing_required_fields(inv)
    if missing:
        raise ExtractionError(f"missing required fields: {', '.join(missing)}")


def missing_identifier(inv: Invoice) -> bool:
    """True when the invoice has no invoice_number AND no po_number AND no
    reference - nothing a reviewer or downstream system could use to
    identify or look up this document.

    Not a hard failure (see REQUIRED_FIELDS' docstring): plenty of real
    commercial/customs invoices only carry a PO number or shipment
    reference, never a true invoice number. This is a review-only signal,
    checked in validate_invoice - it never triggers provider fallback.
    """
    return inv.invoice_number is None and inv.po_number is None and inv.reference is None


def suspicious_line_item_rows(items: list[LineItem]) -> list[int]:
    """1-based indices of line items with no amount at all.

    A genuine line item always contributes a monetary amount to the
    invoice. A row with no amount is already invisible to totals
    reconciliation (see validate_invoice's `amounts` filter below) and is
    far more likely to be a hallucinated table header/label row than a real
    charge - e.g. a repeated column header ("Description", "Qty", ...)
    the model mistook for an item on a continuation page. Rows are never
    dropped for this (normalize_invoice's filter is unchanged); this only
    flags them for human review.
    """
    return [i for i, it in enumerate(items, start=1) if it.amount is None]


def validate_invoice(
    inv: Invoice,
    abs_tolerance: Decimal = Decimal("0.02"),
    rel_tolerance: Decimal = Decimal("0.005"),
) -> str | None:
    """Return a review reason if the invoice fails validation, else None.

    Totals reconciliation (documented rules, in order - passing any check
    means the arithmetic is consistent):

      1. sum(line_items.amount) + tax_amount ~= total_amount   (exclusive tax)
      2. sum(line_items.amount) ~= total_amount                (inclusive/zero tax)
      3. subtotal + tax_amount ~= total_amount AND
         sum(line_items.amount) ~= subtotal                    (subtotal chain)

    tolerance = max(TOTAL_ABS_TOLERANCE, TOTAL_REL_TOLERANCE * |total|).

    When no check passes, the arithmetic is flagged as INCONCLUSIVE rather
    than wrong: the schema has no fields for discount / shipping / duties /
    rounding, so an unexplained difference may be a legitimate charge the
    schema cannot represent. Missing amounts are never invented (a None tax
    is only treated as absent, not as zero, except via check 2).

    Independently of the arithmetic checks, any line item missing an amount
    is flagged via suspicious_line_item_rows() - see its docstring. This
    fires even when the arithmetic itself reconciles (a hallucinated
    amount-less row doesn't change the sum), which is exactly the case it
    is designed to catch.
    """
    reasons = []

    missing = missing_required_fields(inv)
    if missing:
        reasons.append(f"missing required fields: {', '.join(missing)}")

    if missing_identifier(inv):
        reasons.append(
            "missing invoice_number and no alternative PO/reference identifier"
        )

    items = inv.line_items
    if not items:
        reasons.append("no line items extracted")
    else:
        suspicious = suspicious_line_item_rows(items)
        if suspicious:
            rows = ", ".join(str(i) for i in suspicious)
            reasons.append(
                f"{len(suspicious)} line item(s) missing an amount (row(s) {rows}); "
                "possible hallucinated header/label row - review line items before use"
            )

        amounts = [it.amount for it in items if it.amount is not None]
        total = inv.total_amount
        if not amounts:
            reasons.append("line items have no amounts")
        elif total is not None:
            line_sum = sum(amounts, Decimal("0"))
            tax = inv.tax_amount
            subtotal = inv.subtotal
            tolerance = max(abs_tolerance, rel_tolerance * abs(total))

            def close(a: Decimal, b: Decimal) -> bool:
                return abs(a - b) <= tolerance

            ok = (
                (tax is not None and close(line_sum + tax, total))
                or close(line_sum, total)
                or (
                    subtotal is not None
                    and close(subtotal + (tax or Decimal("0")), total)
                    and close(line_sum, subtotal)
                )
            )
            if not ok:
                diff = line_sum + (tax or Decimal("0")) - total
                reasons.append(
                    "totals inconclusive: "
                    f"sum(line_items)={line_sum} tax={tax if tax is not None else 'n/a'} "
                    f"total={total} unexplained difference={diff:+} "
                    "(may be discount/shipping/duties/rounding not captured by schema)"
                )

    return "; ".join(reasons) if reasons else None
