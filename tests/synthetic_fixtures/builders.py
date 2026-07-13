"""Low-level, reusable PDF and page-construction helpers.

MILESTONE 2 SCOPE: pure PyMuPDF page composition. No ground-truth invoice
values are hardcoded here (those belong in scenarios.py, sourced from
ground_truth.py) - this module only knows how to lay out whatever content it
is given. No pipeline or provider imports. No filesystem writes except to an
explicitly supplied output path (see `save_document`).

Determinism: no timestamps, UUIDs, or random values are ever written into
page content. PDF container metadata (e.g. an embedded creation date) is not
independently controlled by this module - see the "known limitations" note
in the milestone report regarding byte-level (not semantic) determinism.

Fonts: Base-14 "helv" (Helvetica) only - built into PyMuPDF, no external font
files, no network access. Verified to round-trip GBP's "£" symbol correctly.

PyMuPDF footgun: a `fitz.Page` object returned by `doc.new_page()` becomes
stale ("page is None") once further pages are added to the same document.
Use the returned Page immediately if at all; to inspect pages after a
document is fully assembled, always re-fetch by index (`doc[i]`) or reopen
the saved file from disk (`fitz.open(path)`) rather than holding onto a
handle returned mid-construction.
"""

from pathlib import Path

import fitz  # PyMuPDF

PAGE_WIDTH = 612.0  # US Letter, points
PAGE_HEIGHT = 792.0
MARGIN_X = 50.0
TOP_Y = 60.0
LINE_HEIGHT = 14.0
FONT_SIZE = 10.0
FONT_NAME = "helv"


# --- Document / page primitives ---------------------------------------------

def new_document() -> fitz.Document:
    """A fresh, empty in-memory PDF document."""
    return fitz.open()


def add_text_page(
    doc: fitz.Document,
    lines: list[str],
    *,
    fontsize: float = FONT_SIZE,
    fontname: str = FONT_NAME,
    top_y: float = TOP_Y,
    margin_x: float = MARGIN_X,
    line_height: float = LINE_HEIGHT,
    width: float = PAGE_WIDTH,
    height: float = PAGE_HEIGHT,
) -> fitz.Page:
    """Add a page with genuinely extractable text: each line is a real text
    object inserted via insert_text, not an image."""
    page = doc.new_page(width=width, height=height)
    y = top_y
    for line in lines:
        if line:  # skip inserting truly empty strings; blank lines just advance y
            page.insert_text((margin_x, y), line, fontsize=fontsize, fontname=fontname)
        y += line_height
    return page


def add_blank_page(
    doc: fitz.Document, *, width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT
) -> fitz.Page:
    """A page with no text, no images, no drawings - genuinely blank."""
    return doc.new_page(width=width, height=height)


def render_lines_to_png(
    lines: list[str],
    *,
    fontsize: float = FONT_SIZE,
    fontname: str = FONT_NAME,
    top_y: float = TOP_Y,
    margin_x: float = MARGIN_X,
    line_height: float = LINE_HEIGHT,
    width: float = PAGE_WIDTH,
    height: float = PAGE_HEIGHT,
    dpi: int = 150,
) -> bytes:
    """Render invoice-like text content into a PNG image (no text layer).

    Uses a throwaway in-memory document purely as a rasterization canvas -
    the returned bytes are a picture, not a PDF, and carry no extractable
    text of their own.
    """
    scratch = fitz.open()
    try:
        page = add_text_page(
            scratch, lines,
            fontsize=fontsize, fontname=fontname, top_y=top_y,
            margin_x=margin_x, line_height=line_height, width=width, height=height,
        )
        zoom = dpi / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pixmap.tobytes("png")
    finally:
        scratch.close()


def add_image_page(
    doc: fitz.Document,
    png_bytes: bytes,
    *,
    width: float = PAGE_WIDTH,
    height: float = PAGE_HEIGHT,
) -> fitz.Page:
    """Add a page whose only content is a full-page embedded image - zero
    meaningful extractable text, by construction."""
    page = doc.new_page(width=width, height=height)
    page.insert_image(fitz.Rect(0, 0, width, height), stream=png_bytes)
    return page


def add_rendered_image_page(
    doc: fitz.Document,
    lines: list[str],
    **kwargs,
) -> fitz.Page:
    """Convenience: render `lines` to a PNG, then embed it as a full page."""
    png_bytes = render_lines_to_png(lines, **kwargs)
    return add_image_page(doc, png_bytes, width=kwargs.get("width", PAGE_WIDTH),
                           height=kwargs.get("height", PAGE_HEIGHT))


def save_document(doc: fitz.Document, output_path: str | Path) -> Path:
    """Write the document to the explicitly supplied path only."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
    return output_path


# --- Generic content-block composers -----------------------------------------
# These take concrete values as arguments; none of the values below are
# hardcoded invoice data - callers (scenarios.py) supply them from
# ground_truth.py.

def header_lines(
    *,
    title: str = "INVOICE",
    invoice_number: str,
    invoice_date: str,
    currency_label: str,
    seller_name: str,
    seller_extra: str | None = None,
    buyer_name: str,
    buyer_extra: str | None = None,
) -> list[str]:
    lines = [
        title,
        f"Invoice Number: {invoice_number}",
        f"Date: {invoice_date}",
        f"Currency: {currency_label}",
        f"Seller: {seller_name}",
    ]
    if seller_extra:
        lines.append(f"  {seller_extra}")
    lines.append(f"Bill To: {buyer_name}")
    if buyer_extra:
        lines.append(f"  {buyer_extra}")
    return lines


def table_header_line(
    columns: tuple[str, str, str, str] = ("Description", "Qty", "Unit Price", "Amount"),
) -> str:
    """A single column-header row string. Callers that need the SAME header
    repeated verbatim across pages (e.g. fixture 8) should reuse the exact
    same return value on every page, not regenerate it."""
    return f"{columns[0]:<28}{columns[1]:>5}{columns[2]:>14}{columns[3]:>12}"


def line_item_lines(
    items: list[tuple[str, str, str, str]],  # (description, quantity, unit_price, amount)
    *,
    amount_formatter=lambda v: v,
) -> list[str]:
    """Format (description, quantity, unit_price, amount) tuples as table rows."""
    lines = []
    for description, quantity, unit_price, amount in items:
        lines.append(
            f"{description:<28}{quantity:>5}{amount_formatter(unit_price):>14}"
            f"{amount_formatter(amount):>12}"
        )
    return lines


def totals_lines(
    *,
    subtotal: str | None = None,
    tax: str | None = None,
    total: str,
    subtotal_label: str = "Subtotal",
    tax_label: str = "Tax",
    total_label: str = "Total",
    amount_formatter=lambda v: v,
) -> list[str]:
    lines = []
    if subtotal is not None:
        lines.append(f"{subtotal_label}: {amount_formatter(subtotal)}")
    if tax is not None:
        lines.append(f"{tax_label}: {amount_formatter(tax)}")
    lines.append(f"{total_label}: {amount_formatter(total)}")
    return lines


def footer_lines(payment_terms: str | None) -> list[str]:
    return [f"Payment Terms: {payment_terms}"] if payment_terms else []


def compose_invoice_lines(
    *,
    header: list[str] | None = None,
    table_header: str | None = None,
    item_lines: list[str] | None = None,
    charge_lines: list[str] | None = None,  # e.g. discount/freight, kept visually separate
    totals: list[str] | None = None,
    footer: list[str] | None = None,
    extra_notes: list[str] | None = None,
) -> list[str]:
    """Stitch content blocks into one page's worth of lines, with a blank
    line separating each section so charge lines (discount/freight) render
    visually apart from the item table."""
    lines: list[str] = []
    if header:
        lines.extend(header)
        lines.append("")
    if table_header:
        lines.append(table_header)
    if item_lines:
        lines.extend(item_lines)
    if charge_lines:
        lines.append("")
        lines.extend(charge_lines)
    if totals:
        lines.append("")
        lines.extend(totals)
    if extra_notes:
        lines.append("")
        lines.extend(extra_notes)
    if footer:
        lines.append("")
        lines.extend(footer)
    return lines


# --- Money formatting utilities -----------------------------------------------
# Pure string transforms; independent of invoice_extractor.schema.coerce_decimal
# (which goes the OPPOSITE direction: display text -> canonical Decimal). These
# helpers go canonical ground-truth string -> display text for the PDF.

def format_amount_eu(value: str) -> str:
    """'1234.56' -> '1.234,56' (thousands dot, decimal comma)."""
    sign = ""
    v = value
    if v.startswith("-"):
        sign, v = "-", v[1:]
    if "." in v:
        int_part, dec_part = v.split(".", 1)
    else:
        int_part, dec_part = v, "00"
    reversed_digits = int_part[::-1]
    groups = [reversed_digits[i : i + 3] for i in range(0, len(reversed_digits), 3)]
    grouped = ".".join(groups)[::-1]
    return f"{sign}{grouped},{dec_part}"


def format_amount_gbp(value: str) -> str:
    """'500.00' -> '£500.00'."""
    return f"£{value}"


def format_amount_plain(value: str) -> str:
    """Identity formatter - plain Decimal-style string, no symbol."""
    return value
