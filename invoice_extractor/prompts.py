"""Shared LLM prompts. Both providers use the same fixed JSON schema."""

import json
import re

from invoice_extractor.schema import ExtractionError

JSON_SCHEMA_BLOCK = """{
  "invoice_number": "string or null - only if the document itself labels a value as the invoice number",
  "po_number": "string or null - customer purchase order / PO number, if shown",
  "reference": "string or null - any other shipment/order/reference number shown, if it is not clearly the invoice number or PO number",
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
      "line_no": "string or null - ONLY the printed row/sequence number of this line in the invoice's own item table (e.g. '1', '2', '003', '10A'), if and only if the invoice actually prints one. This is NEVER a SKU, PLU, product code, article number, style code, or barcode - those go in item_code instead. Null if the invoice does not print a row number.",
      "item_code": "string or null - the product/SKU/article/style/item identifier printed for this line (e.g. '31C207', '73SA041601'), if shown. This is NEVER a row/sequence number - that goes in line_no instead. Null if none is printed.",
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
- If a value appears in a non-English language, extract it as-is (do not translate names or addresses).
- Not every document has a true invoice number: some commercial/customs invoices only show a PO number or other reference. Extract invoice_number ONLY if the document itself labels a value as the invoice number; otherwise set it to null and populate po_number/reference instead. Never copy a PO number or reference into invoice_number just because invoice_number would otherwise be empty.
- line_no and item_code are DIFFERENT fields and must never be swapped: line_no is ONLY a printed row/sequence number (e.g. '1', '2'); item_code is ONLY a product/SKU/article/style identifier (e.g. '31C207'). A product code is NEVER a line_no. Never merge either one into description unless it is genuinely written as part of the description text itself."""


def text_extraction_prompt(invoice_text: str, *, chunk_context: str | None = None) -> str:
    """chunk_context, when given, is inserted after RULES and before the raw
    text - used only for a text-native document split into bounded chunks
    (M3.1); omitted (None) reproduces the exact prompt used everywhere else
    (direct Gemini/Claude, and the OpenRouter ladder's single-chunk case)."""
    return (
        "You are an invoice data extraction engine. Below is the raw text extracted "
        "from the text-native pages of an invoice PDF (any vendor, any country, any "
        "layout). Page boundaries are marked with '--- PAGE n ---'; the pages shown "
        "may be a subset of the document. Extract the fields into this exact JSON "
        "schema:\n\n"
        f"{JSON_SCHEMA_BLOCK}\n\n{RULES}"
        + (chunk_context or "")
        + "\n\n--- INVOICE TEXT START ---\n"
        f"{invoice_text}\n"
        "--- INVOICE TEXT END ---"
    )


def text_chunk_context(page_range: str) -> str:
    """Extra guidance appended to the prompt ONLY when this text is one
    bounded chunk of a larger text-native document (MAX_TEXT_PAGES, M3.1) -
    never used for a whole-document (single-chunk) call, so chunk-level
    hard-required relaxation and this prompt addition always travel
    together (see openrouter_client._attempt_model's is_chunked)."""
    return (
        f"\n\nIMPORTANT - PARTIAL DOCUMENT: the text below is only pages {page_range} "
        "of a LARGER invoice PDF - other pages exist before and/or after this chunk "
        "and are sent as separate requests. Extract ONLY what is visible in these "
        "specific pages. Do not invent header fields (invoice number, dates, seller/"
        "buyer, totals) that are not shown here - leave them null if this chunk does "
        "not contain them; a later or earlier chunk may supply them instead. List "
        "only the line items printed on these specific pages - do not repeat line "
        "items that would belong to a different chunk."
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
