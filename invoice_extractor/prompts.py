"""Shared LLM prompts. Both providers use the same fixed JSON schema."""

import json
import re

from invoice_extractor.schema import ExtractionError

JSON_SCHEMA_BLOCK = """{
  "invoice_number": "string or null",
  "invoice_date": "string in YYYY-MM-DD format, or null",
  "currency": "3-letter ISO 4217 code (e.g. USD, EUR, GBP), or null",
  "seller_name": "string or null",
  "seller_address": "single-line string or null",
  "buyer_name": "string or null",
  "buyer_address": "single-line string or null",
  "subtotal": "number or null",
  "tax_amount": "number or null (0 if the invoice explicitly shows zero tax)",
  "total_amount": "number or null",
  "payment_terms": "string or null (e.g. 'Net 30', 'Due on receipt')",
  "line_items": [
    {
      "description": "string or null",
      "quantity": "number or null",
      "unit_price": "number or null",
      "amount": "number or null (line total)"
    }
  ]
}"""

RULES = """Rules:
- Return ONLY a single JSON object matching the schema exactly. No markdown fences, no commentary, no extra keys.
- Use null for anything not present. Never invent values (including currency - only report a currency you can actually see or unambiguously infer from a symbol).
- Numbers must be plain JSON numbers: no currency symbols, no thousands separators. Preserve zero as 0, not null.
- Convert dates to YYYY-MM-DD. Interpret ambiguous formats using the invoice's country/locale cues.
- The seller is the party issuing the invoice; the buyer is the party being billed.
- Include every line item, in the order they appear in the document. Do NOT repeat table header rows as line items, and exclude subtotal/discount/shipping/tax/total rows from line_items.
- If the same table header repeats on continuation pages, count the items underneath it only once.
- If a value appears in a non-English language, extract it as-is (do not translate names or addresses)."""


def text_extraction_prompt(invoice_text: str) -> str:
    return (
        "You are an invoice data extraction engine. Below is the raw text extracted "
        "from the text-native pages of an invoice PDF (any vendor, any country, any "
        "layout). Page boundaries are marked with '--- PAGE n ---'; the pages shown "
        "may be a subset of the document. Extract the fields into this exact JSON "
        "schema:\n\n"
        f"{JSON_SCHEMA_BLOCK}\n\n{RULES}\n\n"
        "--- INVOICE TEXT START ---\n"
        f"{invoice_text}\n"
        "--- INVOICE TEXT END ---"
    )


def vision_extraction_prompt(page_count: int) -> str:
    pages = "image" if page_count == 1 else f"{page_count} images (one per page, in order)"
    return (
        f"You are an invoice data extraction engine. The attached {pages} show scanned "
        "page(s) of an invoice (any vendor, any country, any layout); they may be a "
        "subset of the document. Read the page(s) and extract the fields into this "
        "exact JSON schema:\n\n"
        f"{JSON_SCHEMA_BLOCK}\n\n{RULES}"
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_response(text: str) -> dict:
    """Parse an LLM response into a dict, tolerating code fences and preamble.

    Error messages never embed response content (they get logged); the raw
    payload rides on ExtractionError.detail for optional debug artifacts.
    """
    if not text or not text.strip():
        raise ExtractionError("empty response from model")
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span (handles stray preamble text).
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ExtractionError(
                f"no JSON object found in response (length={len(cleaned)})",
                detail=text,
            )
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                f"malformed JSON in response ({exc.msg} at pos {exc.pos})",
                detail=text,
            ) from exc
    if not isinstance(data, dict):
        raise ExtractionError(
            f"expected JSON object, got {type(data).__name__}", detail=text
        )
    return data
