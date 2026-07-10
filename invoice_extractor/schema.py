"""Unified invoice schema: normalization, coercion, and validation."""

import re

HEADER_FIELDS = [
    "invoice_number",
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

# Fields that must be non-null or the invoice is flagged for review.
REQUIRED_FIELDS = [
    "invoice_number",
    "invoice_date",
    "currency",
    "seller_name",
    "total_amount",
]


class ExtractionError(Exception):
    """LLM output was unusable (bad JSON, missing required fields, etc.).

    Not retried against the same provider; triggers the fallback provider.
    """


def empty_invoice() -> dict:
    inv: dict = {field: None for field in HEADER_FIELDS}
    inv["line_items"] = []
    return inv


_NUMBER_CLEAN_RE = re.compile(r"[^\d.,\-]")


def coerce_number(value) -> float | None:
    """Coerce LLM output ('1,234.50', '$99', 42) to float, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = _NUMBER_CLEAN_RE.sub("", value).strip()
        if not cleaned:
            return None
        # European style "1.234,56" -> "1234.56"
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            # Lone comma is a decimal separator if it looks like one ("123,45")
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _coerce_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_invoice(raw) -> dict:
    """Map raw LLM JSON onto the unified schema, coercing types.

    Unknown keys are dropped; missing keys become None.
    """
    if not isinstance(raw, dict):
        raise ExtractionError(f"expected a JSON object, got {type(raw).__name__}")

    inv = empty_invoice()
    for field in HEADER_FIELDS:
        value = raw.get(field)
        if field in NUMERIC_HEADER_FIELDS:
            inv[field] = coerce_number(value)
        else:
            inv[field] = _coerce_str(value)

    items = raw.get("line_items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = {}
            for field in LINE_ITEM_FIELDS:
                value = item.get(field)
                if field in NUMERIC_LINE_ITEM_FIELDS:
                    normalized[field] = coerce_number(value)
                else:
                    normalized[field] = _coerce_str(value)
            if any(v is not None for v in normalized.values()):
                inv["line_items"].append(normalized)
    return inv


def missing_required_fields(inv: dict) -> list[str]:
    return [f for f in REQUIRED_FIELDS if inv.get(f) is None]


def check_required(inv: dict) -> None:
    """Raise ExtractionError when required fields are missing.

    Used on provider output to decide whether to try the fallback provider.
    """
    missing = missing_required_fields(inv)
    if missing:
        raise ExtractionError(f"missing required fields: {', '.join(missing)}")


def validate_invoice(inv: dict) -> str | None:
    """Return a review reason if the invoice fails validation, else None."""
    reasons = []

    missing = missing_required_fields(inv)
    if missing:
        reasons.append(f"missing required fields: {', '.join(missing)}")

    items = inv.get("line_items") or []
    if not items:
        reasons.append("no line items extracted")
    else:
        amounts = [it["amount"] for it in items if it.get("amount") is not None]
        total = inv.get("total_amount")
        if amounts and total is not None:
            line_sum = sum(amounts)
            tax = inv.get("tax_amount") or 0.0
            tolerance = max(0.02, abs(total) * 0.01)
            if abs(line_sum + tax - total) > tolerance:
                reasons.append(
                    f"totals mismatch: sum(line_items)={line_sum:.2f} "
                    f"+ tax={tax:.2f} != total={total:.2f}"
                )
        elif not amounts:
            reasons.append("line items have no amounts")

    return "; ".join(reasons) if reasons else None
