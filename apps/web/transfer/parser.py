"""Deterministic Transfer Delivery Note parser (Build 2). No LLM, no cloud.

Works on the normalized PageText spans (embedded words or OCR line tokens -
same code path). Strategy per page:

  1. cluster positioned spans into visual rows (y proximity);
  2. parse the repeated header block with label-anchored geometry
     (left-column labels take values left of ~60% page width so the
     barcode text on the right can never contaminate them);
  3. locate the item-table header row and derive per-column x bands;
  4. assemble item rows (a row starts at an item-code-bearing cluster;
     stray fragments such as wrapped descriptions or OCR-split cells are
     re-attached to the adjacent row that lacks that field);
  5. rescue short cells (single-letter sizes) that OCR page detection
     missed via recognition-only OCR on the exact cell crop;
  6. assemble cartons across pages by PRINTED carton number (a carton may
     span pages; numbers keep leading zeros and are never invented or
     resequenced);
  7. validate printed carton/grand totals against calculated line sums -
     exactly, without ever adjusting quantities.

Raw source text is preserved beside every normalized value.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from apps.web.transfer.extraction_models import (
    AMBIGUOUS_DESTINATION,
    CARTON_TOTAL_MISMATCH,
    DOCUMENT_TOTAL_MISMATCH,
    INVALID_EAN,
    INVALID_QUANTITY,
    INVALID_RETAIL_PRICE,
    MALFORMED_ITEM_ROW,
    METHOD_OCR,
    METHOD_UNREADABLE,
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
    TransferCarton,
    TransferDocumentExtraction,
    TransferExtractionIssue,
    TransferNoteHeader,
    TransferNoteLine,
)
from apps.web.transfer.ocr import OcrAdapter
from apps.web.transfer.pagetext import PageText, TextSpan


def _norm(text: str) -> str:
    """NFKC (folds fullwidth OCR glyphs), collapse whitespace."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def _upper(text: str) -> str:
    return _norm(text).upper()


# --- row clustering ---------------------------------------------------------------

@dataclass
class Row:
    y: float
    spans: list[TextSpan] = field(default_factory=list)

    def text(self) -> str:
        return _norm(" ".join(s.text for s in sorted(self.spans,
                                                     key=lambda s: s.x0)))


def cluster_rows(spans: list[TextSpan]) -> list[Row]:
    """Group spans into visual rows by y-center proximity."""
    if not spans:
        return []
    heights = sorted(s.y1 - s.y0 for s in spans)
    tolerance = max(3.0, heights[len(heights) // 2] * 0.6)
    rows: list[Row] = []
    for span in sorted(spans, key=lambda s: (s.y0 + s.y1) / 2):
        center = (span.y0 + span.y1) / 2
        if rows and abs(center - rows[-1].y) <= tolerance:
            row = rows[-1]
            row.spans.append(span)
            row.y = (row.y * (len(row.spans) - 1) + center) / len(row.spans)
        else:
            rows.append(Row(y=center, spans=[span]))
    for row in rows:
        row.spans.sort(key=lambda s: s.x0)
    return rows


# --- document recognition ---------------------------------------------------------

_MARKERS = (
    (re.compile(r"TRANSFER\s*DELIVERY\s*NOTE"), 3),
    (re.compile(r"IMAGINEX"), 2),
    (re.compile(r"D\s*/\s*N\s*#"), 2),
    (re.compile(r"TO\s*LOC"), 2),
    (re.compile(r"PICK\s*REF"), 2),
    (re.compile(r"EAN\s*CODE"), 1),
    (re.compile(r"\bCARTON\b"), 1),
    (re.compile(r"\bBATCH\b"), 1),
)
_RECOGNITION_THRESHOLD = 4


def recognition_score(page_texts: list[str]) -> int:
    blob = _upper(" ".join(page_texts))
    return sum(weight for rx, weight in _MARKERS if rx.search(blob))


# --- header parsing ---------------------------------------------------------------

_LEFT_LABELS = {"BATCH": "batch", "FROM": "from", "TO LOC": "to",
                "PICK REF": "pick"}
_RIGHT_LABELS = {"D/N#": "dn", "DATE": "date", "PAGE": "page"}


def _label_key(text: str) -> str:
    return _upper(text).rstrip(".:").replace(" .", "").strip()


# A value never runs past the next label on the same line (e.g. Pick Ref's
# value must stop before the mid-row "Carton"/"D/N" labels).
_STOP_LABEL_KEYS = {"CARTON", "D/N", "D/N#", "PAGE", "DATE", "BATCH", "FROM",
                    "TO LOC", "PICK REF"}


def _value_right_of(rows: list[Row], span: TextSpan, *, page_width: float,
                    x_max: float, prefix: str = "") -> str:
    """Collect value text right of a label span within a slightly widened
    y band (OCR baselines wobble across the label/value gap), stopping at
    the next label on the line. 1.0x the label height captures the real
    samples' wobbling value baselines while staying inside the header
    block's row pitch for both embedded text and 200-dpi OCR."""
    band = (span.y1 - span.y0) * 1.0
    center = (span.y0 + span.y1) / 2
    stop_x = x_max
    for row in rows:
        for key, s in _iter_label_spans(row):
            s_center = (s.y0 + s.y1) / 2
            if (key in _STOP_LABEL_KEYS and s.x0 > span.x1 + 2
                    and abs(s_center - center) <= band):
                stop_x = min(stop_x, s.x0)
    parts = []
    for row in rows:
        for s in row.spans:
            s_center = (s.y0 + s.y1) / 2
            if abs(s_center - center) > band:
                continue
            if s.x0 <= span.x1 - 2 or s.x0 >= stop_x:
                continue
            parts.append((s.x0, s.text))
    value = " ".join(t for _, t in sorted(parts))
    value = (prefix + " " + value) if prefix else value
    return _norm(value).lstrip(":").strip()


def split_location(raw: str) -> tuple[str | None, str | None]:
    """'ZZOHK101 Multi Brand(Outlet)-Office-Main Office' -> code + name.
    OCR may fuse them ('ZZOHK101Multi ...'). Codes are uppercased; the
    descriptive name keeps its source casing."""
    value = _norm(raw)
    if not value:
        return None, None
    head, _, rest = value.partition(" ")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{3,11}", head) and rest:
        return head.upper(), rest.strip() or None
    m = re.match(r"^([A-Za-z]{2,8}\d{1,5})(.*)$", value)
    if m:
        name = m.group(2).strip(" -")
        return m.group(1).upper(), name or None
    return None, value


def parse_date(raw: str) -> str | None:
    """Normalize to ISO YYYY-MM-DD. Slash dates are read day-first
    (DD/MM/YYYY, the note's regional convention); month-first is accepted
    only when day-first is impossible. Source string is always kept."""
    value = _norm(raw)
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass
class PageHeader:
    batch: str | None = None
    from_raw: str | None = None
    to_raw: str | None = None
    pick: str | None = None
    carton: str | None = None
    dn: str | None = None
    date_raw: str | None = None
    page_no: int | None = None
    page_count: int | None = None


_TWO_WORD_LABELS = {"TO LOC", "PICK REF"}


def _iter_label_spans(row: Row):
    """Yield (label_key, span) treating adjacent word pairs like
    'To'+'Loc.' as one label (embedded text splits them; OCR usually
    doesn't)."""
    spans = row.spans
    i = 0
    while i < len(spans):
        if i + 1 < len(spans):
            pair = _label_key(spans[i].text.rstrip(".") + " "
                              + spans[i + 1].text)
            if pair in _TWO_WORD_LABELS:
                yield pair, TextSpan(
                    text=spans[i].text + " " + spans[i + 1].text,
                    x0=spans[i].x0, y0=min(spans[i].y0, spans[i + 1].y0),
                    x1=spans[i + 1].x1, y1=max(spans[i].y1, spans[i + 1].y1))
                i += 2
                continue
        yield _label_key(spans[i].text), spans[i]
        i += 1


def parse_page_header(rows: list[Row]) -> PageHeader:
    header = PageHeader()
    spans = [s for row in rows for s in row.spans]
    if not spans:
        return header
    page_width = max(s.x1 for s in spans)
    for row in rows:
        for key, span in _iter_label_spans(row):
            token = _upper(span.text)
            # left-column labels: value stays left of ~60% width so the
            # barcode text top-right can never bleed in
            for label, attr in _LEFT_LABELS.items():
                prefix = ""
                if key == label or key == label.replace(" ", ""):
                    pass
                elif token.startswith(label + ":") or token.startswith(
                        label.replace(" ", "") + ":"):
                    prefix = span.text.split(":", 1)[1]
                else:
                    continue
                if getattr(header, {"batch": "batch", "from": "from_raw",
                                    "to": "to_raw", "pick": "pick"}[attr]):
                    continue
                value = prefix.strip() or _value_right_of(
                    rows, span, page_width=page_width,
                    x_max=page_width * 0.60)
                setattr(header, {"batch": "batch", "from": "from_raw",
                                 "to": "to_raw", "pick": "pick"}[attr], value or None)
            # right-column labels
            for label, attr in _RIGHT_LABELS.items():
                if _label_key(span.text) != _label_key(label):
                    continue
                if span.x0 < page_width * 0.45:
                    continue                       # e.g. the bare mid-row D/N
                value = _value_right_of(rows, span, page_width=page_width,
                                        x_max=page_width)
                if not value:
                    continue
                if attr == "dn" and not header.dn:
                    header.dn = value
                elif attr == "date" and not header.date_raw:
                    header.date_raw = value.split()[0] if value else None
                elif attr == "page" and header.page_no is None:
                    m = re.match(r"^(\d+)\s*(?:/|OF)?\s*(\d+)?", _upper(value))
                    if m:
                        header.page_no = int(m.group(1))
                        header.page_count = (int(m.group(2))
                                             if m.group(2) else None)
            # Carton NNN (mid-page label; value is the next numeric
            # token, or OCR fuses label+number into one token)
            if key == "CARTON" and header.carton is None:
                value = _value_right_of(rows, span, page_width=page_width,
                                        x_max=span.x1 + page_width * 0.18)
                m = re.match(r"^(\d{1,6})\b", value)
                if m:
                    header.carton = m.group(1)     # string: keeps zeros
            elif header.carton is None:
                fused = re.match(r"^CARTON[:\s]*(\d{1,6})$", token)
                if fused:
                    header.carton = fused.group(1)
    return header


# --- item table -------------------------------------------------------------------

_COLUMNS = ("seq", "item", "ean", "description", "price", "color", "size",
            "quantity")
_COLUMN_LABELS = {
    "SEQ": "seq", "ITEM": "item", "EAN CODE": "ean", "EANCODE": "ean",
    "DESCRIPTION": "description", "RETAIL PRICE": "price",
    "RETAILPRICE": "price", "COLOR": "color", "SIZE": "size",
    "QUANTITY": "quantity",
}

# Full IMAGINEX item-code shape (e.g. ZEHE380331E997), tolerant of a fused
# leading sequence number and a fused trailing EAN - all common OCR results.
_ITEM_FULL_RE = re.compile(
    r"^(\d{1,3})?\s*([A-Z]{2,5}\d{4,8}[A-Z]\d{3})\s*(\d{8,14})?$")
# Fallback for unusual codes: letters+digits mix, at least one digit, so a
# wrapped description word ("BOTTOM") can never look like an item code.
_ITEM_CODE_RE = re.compile(r"^(\d{1,3})?\s*([A-Z]{2,}[A-Z0-9]*\d[A-Z0-9]*)$")
_EAN_RE = re.compile(r"^\d{8,14}$")
_QTY_RE = re.compile(r"^(\d+)\s*(?:PCS|PC|UNITS|UNIT)?$")
_TOTAL_RE = re.compile(
    r"(GRAND\s*TOTAL|CARTON\s*TOTAL|TOTAL)\s*:?\s*(\S+)?\s*(UNIT|UNITS|PCS|PC)?\s*$")


def find_column_anchors(rows: list[Row]) -> tuple[dict[str, float], float] | None:
    """Locate the table header row; return column-label x centers and its y."""
    for row in rows:
        found: dict[str, float] = {}
        spans = row.spans
        i = 0
        while i < len(spans):
            merged = None
            if i + 1 < len(spans):
                two = _label_key(spans[i].text + " " + spans[i + 1].text)
                if two in _COLUMN_LABELS:
                    merged = (_COLUMN_LABELS[two],
                              (spans[i].x0 + spans[i + 1].x1) / 2)
                    i += 2
            if merged is None:
                key = _label_key(spans[i].text)
                if key in _COLUMN_LABELS:
                    merged = (_COLUMN_LABELS[key],
                              (spans[i].x0 + spans[i].x1) / 2)
                i += 1
            if merged:
                found.setdefault(merged[0], merged[1])
        if len(found) >= 4 and "quantity" in found:
            return found, row.y
    return None


def column_bands(anchors: dict[str, float],
                 page_width: float) -> list[tuple[str, float, float]]:
    ordered = sorted(anchors.items(), key=lambda kv: kv[1])
    bands = []
    for i, (name, x) in enumerate(ordered):
        lo = 0.0 if i == 0 else (ordered[i - 1][1] + x) / 2
        hi = page_width if i == len(ordered) - 1 else (x + ordered[i + 1][1]) / 2
        bands.append((name, lo, hi))
    return bands


def band_of(bands: list[tuple[str, float, float]], span: TextSpan) -> str:
    center = (span.x0 + span.x1) / 2
    for name, lo, hi in bands:
        if lo <= center < hi:
            return name
    return bands[-1][0]


@dataclass
class RawItemRow:
    y: float
    page: int
    cells: dict[str, list[str]] = field(default_factory=dict)
    confidences: list[float] = field(default_factory=list)
    y_min: float = 0.0
    y_max: float = 0.0

    def put(self, band: str, text: str) -> None:
        self.cells.setdefault(band, []).append(text)

    def get(self, band: str) -> str | None:
        parts = self.cells.get(band)
        return _norm(" ".join(parts)) if parts else None


def _split_lead(text: str) -> tuple[str | None, str, str | None] | None:
    """(seq, item_code, fused_ean) from a row's leading text, or None when
    the row does not start an item line."""
    t = _upper(text)
    m = _ITEM_FULL_RE.match(t)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _ITEM_CODE_RE.match(t)
    if m and len(m.group(2)) >= 6:
        return m.group(1), m.group(2), None
    return None


def parse_item_rows(rows: list[Row], bands: list[tuple[str, float, float]],
                    header_y: float, page_number: int,
                    ) -> tuple[list[RawItemRow], list[Row]]:
    """Assemble item rows below the header row. Returns (rows, total_rows).

    Fragment clusters without an item code (wrapped descriptions, or OCR
    splitting a row's cells across two baselines) are re-attached to the
    neighbouring row that is missing that field - descriptions prefer the
    following row when it has none, matching how OCR splits wide cells."""
    items: list[RawItemRow] = []
    fragments: list[tuple[float, dict[str, list[str]], list[float]]] = []
    total_rows: list[Row] = []
    for row in rows:
        if row.y <= header_y + 1:
            continue
        text = _upper(row.text())
        lead, consumed = _lead_of(row)
        split = _split_lead(lead) if lead else None
        if _TOTAL_RE.search(text) and split is None:
            total_rows.append(row)
            continue
        if re.match(r"^(REMARKS|SCANNED BY|CHECKED|AUTHORIZED|DELIVERED|"
                    r"RECEIVED|DATE\b)", text):
            continue
        confs = [s.confidence for s in row.spans]
        if split is not None:
            seq, item_code, fused_ean = split
            rest = row.spans[consumed:]
            if (fused_ean is None and rest
                    and re.fullmatch(r"\d{8,14}", _norm(rest[0].text))):
                fused_ean = _norm(rest[0].text)
                rest = rest[1:]
            cells = _group_spans(rest, bands)
            # the lead splitter owns identity fields regardless of whether
            # the Seq./Item column anchors were readable on this page
            cells.pop("seq", None)
            cells["item"] = [item_code]
            if seq:
                cells["seq"] = [seq]
            if fused_ean:
                cells["ean"] = [fused_ean]
            item = RawItemRow(y=row.y, page=page_number, cells=cells,
                              confidences=confs,
                              y_min=min(s.y0 for s in row.spans),
                              y_max=max(s.y1 for s in row.spans))
            items.append(item)
        else:
            cells = _group_spans(row.spans, bands)
            if cells:
                fragments.append((row.y, cells, confs))
    _attach_fragments(items, fragments)
    return items, total_rows


def _lead_of(row: Row) -> tuple[str, int]:
    """The row's leading item-identity text: the first span, or the first
    two when a bare sequence number stands alone."""
    spans = row.spans
    if not spans:
        return "", 0
    first = _norm(spans[0].text)
    if re.fullmatch(r"\d{1,3}", first) and len(spans) > 1:
        return first + " " + spans[1].text, 2
    return first, 1


def _group_spans(spans: list[TextSpan], bands) -> dict[str, list[str]]:
    cells: dict[str, list[str]] = {}
    band_by_name = {b[0]: b for b in bands}
    for span in spans:
        name = band_of(bands, span)
        text = span.text
        # a long description token can run into the price band: split a
        # trailing bare number off a description-band-start span (OCR may
        # fuse them with or without a space)
        if (name == "description" and "description" in band_by_name
                and span.x1 > band_by_name["description"][2]):
            m = (re.match(r"^(.*[A-Za-z].*?)\s+(\d{2,6})$", _norm(text))
                 or re.match(r"^(.*[A-Za-z][^\d])(\d{3,6})$", _norm(text)))
            if m:
                cells.setdefault("description", []).append(m.group(1))
                cells.setdefault("price", []).append(m.group(2))
                continue
        cells.setdefault(name, []).append(text)
    return cells


def _attach_fragments(items: list[RawItemRow], fragments) -> None:
    for y, cells, confs in fragments:
        if not items:
            continue
        prev = max((it for it in items if it.y <= y),
                   key=lambda it: it.y, default=None)
        nxt = min((it for it in items if it.y > y),
                  key=lambda it: it.y, default=None)
        for band, texts in cells.items():
            target = None
            if band == "description":
                if nxt is not None and not nxt.cells.get("description"):
                    target = nxt
                elif prev is not None:
                    target = prev
                else:
                    target = nxt
            else:
                candidates = [it for it in (prev, nxt)
                              if it is not None and not it.cells.get(band)]
                if candidates:
                    target = min(candidates, key=lambda it: abs(it.y - y))
            if target is not None:
                for t in texts:
                    target.put(band, t)
                target.confidences.extend(confs)


# --- line normalization -----------------------------------------------------------

def _issue(issues, code, severity, message, *, source_file, page=None,
           carton=None, line_ref=None, field_name=None, raw=None):
    issues.append(TransferExtractionIssue(
        code=code, severity=severity, message=message, source_file=source_file,
        source_page=page, carton=carton, line_ref=line_ref, field=field_name,
        raw_value=raw))


_SIZE_OK_RE = re.compile(r"^[A-Z0-9][A-Z0-9/\-\.]{0,5}$")


def _rescue_cell(page_text: PageText, adapter: OcrAdapter | None,
                 bands, band_name: str, row: RawItemRow) -> tuple[str, float] | None:
    """Recognition-only OCR on one cell crop (OCR pages only)."""
    if adapter is None or page_text.image_png is None:
        return None
    band = next((b for b in bands if b[0] == band_name), None)
    if band is None or row.y_max <= row.y_min:
        return None
    pad = 6
    try:
        text, conf = adapter.recognize_cell(
            page_text.image_png,
            (band[1] + 2, row.y_min - pad, band[2] - 2, row.y_max + pad))
    except Exception:
        return None
    text = _upper(text)
    return (text, conf) if text else None


def normalize_line(row: RawItemRow, *, source_file: str, upload_sequence: int,
                   dn: str | None, carton_no: str | None, method: str,
                   page_text: PageText, adapter: OcrAdapter | None, bands,
                   issues: list[TransferExtractionIssue]) -> TransferNoteLine:
    page = row.page

    # seq + item (OCR often fuses "12ZEER388975E085")
    raw_item = row.get("item")
    raw_seq = row.get("seq")
    combined = _upper(" ".join(filter(None, [raw_seq, raw_item])))
    seq_no = None
    item_code = None
    m = _ITEM_CODE_RE.match(combined)
    if m:
        seq_no = int(m.group(1)) if m.group(1) else None
        item_code = m.group(2)
    elif raw_seq and raw_seq.isdigit():
        seq_no = int(raw_seq)

    line = TransferNoteLine(
        source_file=source_file, upload_sequence=upload_sequence,
        source_page=page, delivery_note_number=dn,
        original_carton_number=carton_no, source_sequence_number=seq_no,
        raw_item_code=raw_item or raw_seq,
        normalized_item_code=item_code,
        raw_description=row.get("description"),
        normalized_description=_norm(row.get("description") or "") or None,
        extraction_method=method,
    )
    if row.confidences:
        line.extraction_confidence = round(
            sum(row.confidences) / len(row.confidences), 3)

    ref = dict(source_file=source_file, page=page, carton=carton_no,
               line_ref=seq_no)

    # EAN: string, leading zeros preserved
    raw_ean = row.get("ean")
    line.raw_ean = raw_ean
    if raw_ean:
        candidate = _upper(raw_ean).replace(" ", "")
        if _EAN_RE.match(candidate):
            line.normalized_ean = candidate
        else:
            _issue(issues, INVALID_EAN, SEV_WARNING,
                   f"EAN '{raw_ean}' is not 8-14 digits.",
                   field_name="ean", raw=raw_ean, **ref)
    elif item_code:
        _issue(issues, INVALID_EAN, SEV_WARNING,
               "EAN missing; the Item+Color+Size fallback identifier will "
               "apply in later builds.", field_name="ean", **ref)
    if not item_code and not line.normalized_ean:
        _issue(issues, MISSING_ITEM_IDENTIFIER, SEV_ERROR,
               "Row has neither a readable Item code nor an EAN.",
               field_name="item", raw=combined or None, **ref)

    # retail price -> Decimal
    raw_price = row.get("price")
    line.raw_retail_price = raw_price
    if raw_price:
        try:
            line.normalized_retail_price = str(
                Decimal(raw_price.replace(",", "").replace(" ", "")))
        except InvalidOperation:
            _issue(issues, INVALID_RETAIL_PRICE, SEV_WARNING,
                   f"Retail price '{raw_price}' is not numeric.",
                   field_name="retail_price", raw=raw_price, **ref)

    # color
    raw_color = row.get("color")
    if not raw_color:
        rescued = _rescue_cell(page_text, adapter, bands, "color", row)
        if rescued:
            raw_color = rescued[0]
    line.raw_color_code = raw_color
    if raw_color:
        line.normalized_color_code = _upper(raw_color)
    else:
        _issue(issues, MISSING_COLOR, SEV_WARNING, "Color code missing.",
               field_name="color", **ref)

    # size (single letters are routinely missed by OCR page detection)
    raw_size = row.get("size")
    if not raw_size:
        rescued = _rescue_cell(page_text, adapter, bands, "size", row)
        if rescued and _SIZE_OK_RE.match(rescued[0]):
            raw_size = rescued[0]
    line.raw_size_code = raw_size
    if raw_size:
        line.normalized_size_code = _upper(raw_size)
    else:
        _issue(issues, MISSING_SIZE, SEV_WARNING, "Size code missing.",
               field_name="size", **ref)

    # quantity -> positive int
    raw_qty = row.get("quantity")
    line.raw_quantity = raw_qty
    if raw_qty:
        m = _QTY_RE.match(_upper(raw_qty))
        if m and int(m.group(1)) > 0:
            line.normalized_quantity = int(m.group(1))
        else:
            _issue(issues, INVALID_QUANTITY, SEV_ERROR,
                   f"Quantity '{raw_qty}' is not a positive integer.",
                   field_name="quantity", raw=raw_qty, **ref)
    else:
        _issue(issues, INVALID_QUANTITY, SEV_ERROR, "Quantity missing.",
               field_name="quantity", **ref)

    populated = sum(1 for v in (item_code, line.normalized_ean,
                                line.normalized_description,
                                line.normalized_quantity) if v)
    if populated < 2:
        _issue(issues, MALFORMED_ITEM_ROW, SEV_ERROR,
               "Row could not be read as an item line; kept for review.",
               raw=row.get("description") or combined or None, **ref)
    return line


# --- totals -----------------------------------------------------------------------

def parse_total_rows(total_rows: list[Row]) -> list[tuple[str, str | None]]:
    """[(kind 'carton'|'grand', raw_value_text)] found on one page."""
    out = []
    for row in total_rows:
        m = _TOTAL_RE.search(_upper(row.text()))
        if not m:
            continue
        label = m.group(1)
        kind = "grand" if "GRAND" in label else "carton"
        out.append((kind, m.group(2)))
    return out


def _total_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    cleaned = _upper(raw).replace(",", "")
    return int(cleaned) if cleaned.isdigit() else None


# --- document assembly ------------------------------------------------------------

def parse_document(*, source_file: str, upload_sequence: int,
                   pages: list[PageText],
                   adapter: OcrAdapter | None) -> TransferDocumentExtraction:
    doc = TransferDocumentExtraction(source_file=source_file,
                                     upload_sequence=upload_sequence,
                                     page_count=len(pages))
    issues = doc.issues
    page_rows: dict[int, list[Row]] = {}
    for pt in pages:
        doc.page_methods.append(pt.method)
        if pt.method == METHOD_UNREADABLE:
            doc.pages_unreadable += 1
            code = (OCR_UNAVAILABLE if pt.error == "ocr_unavailable"
                    else UNREADABLE_PAGE)
            sev = SEV_ERROR
            _issue(issues, code, sev,
                   f"Page {pt.page_number} could not be read"
                   + (" (install the optional local OCR dependency to "
                      "process scanned pages)." if code == OCR_UNAVAILABLE
                      else f": {pt.error}."),
                   source_file=source_file, page=pt.page_number)
            continue
        if pt.method == METHOD_OCR:
            doc.pages_ocr += 1
        else:
            doc.pages_embedded_text += 1
        page_rows[pt.page_number] = cluster_rows(pt.spans)

    if not page_rows:
        return doc

    # recognition uses every readable page together
    score = recognition_score([r.text() for rows in page_rows.values()
                               for r in rows])
    doc.recognized = score >= _RECOGNITION_THRESHOLD
    if not doc.recognized:
        _issue(issues, UNRECOGNIZED_DOCUMENT, SEV_ERROR,
               "This PDF does not look like an IMAGINEX Transfer Delivery "
               "Note; marked for review.", source_file=source_file)
        return doc

    headers: dict[int, PageHeader] = {n: parse_page_header(rows)
                                      for n, rows in page_rows.items()}

    # --- document-level header --------------------------------------------------
    def first(attr):
        for n in sorted(headers):
            v = getattr(headers[n], attr)
            if v:
                return v
        return None

    to_values = {}
    for n in sorted(headers):
        raw = headers[n].to_raw
        if raw:
            code, _ = split_location(raw)
            to_values.setdefault(code or _norm(raw), raw)
    header = TransferNoteHeader(
        source_file=source_file, upload_sequence=upload_sequence,
        document_title="TRANSFER DELIVERY NOTE",
        batch_reference=first("batch"),
        from_location_raw=first("from_raw"),
        to_location_raw=first("to_raw"),
        pick_reference=first("pick"),
        delivery_note_number=first("dn"),
        delivery_date_raw=first("date_raw"),
        declared_page_count=max((h.page_count or 0
                                 for h in headers.values()), default=0) or None,
    )
    first_page_no = min(headers)
    header.declared_page_number = headers[first_page_no].page_no
    if header.from_location_raw:
        header.from_location_code, header.from_location_name = split_location(
            header.from_location_raw)
    if header.to_location_raw:
        header.to_location_code, header.to_location_name = split_location(
            header.to_location_raw)
    if header.delivery_date_raw:
        header.delivery_date = parse_date(header.delivery_date_raw)
    doc.header = header

    if len(to_values) > 1:
        _issue(issues, AMBIGUOUS_DESTINATION, SEV_ERROR,
               "Pages disagree on To Loc.: "
               + ", ".join(sorted(to_values)) + ".",
               source_file=source_file, field_name="to_location")
    elif not to_values:
        _issue(issues, MISSING_DESTINATION, SEV_ERROR,
               "No To Loc. destination could be read from any page "
               "(destinations are never inferred from filenames).",
               source_file=source_file, field_name="to_location")
    if not header.delivery_note_number:
        _issue(issues, MISSING_DELIVERY_NOTE_NO, SEV_ERROR,
               "No D/N# could be read from any page.",
               source_file=source_file, field_name="delivery_note_number")

    unambiguous_dest = (header.to_location_code
                        if len(to_values) == 1 else None)

    # --- cartons across pages (upload order, then page order) -------------------
    current: TransferCarton | None = None
    for page_no in sorted(page_rows):
        rows = page_rows[page_no]
        page_header = headers[page_no]
        pt = next(p for p in pages if p.page_number == page_no)
        anchor = find_column_anchors(rows)
        if anchor is None:
            continue
        anchors, header_y = anchor
        page_width = max(s.x1 for r in rows for s in r.spans)
        bands = column_bands(anchors, page_width)
        item_rows, total_rows = parse_item_rows(rows, bands, header_y, page_no)

        carton_no = page_header.carton
        page_dest_code = None
        if page_header.to_raw:
            page_dest_code, _n = split_location(page_header.to_raw)
        inherited = False
        if page_dest_code is None and unambiguous_dest:
            page_dest_code = unambiguous_dest
            inherited = True

        if carton_no is None:
            _issue(issues, MISSING_CARTON_NO, SEV_ERROR,
                   f"Page {page_no} has no readable carton number; a number "
                   "is never invented.", source_file=source_file, page=page_no)
        if current is None or (carton_no or object()) != current.original_carton_number:
            current = TransferCarton(
                source_file=source_file, upload_sequence=upload_sequence,
                source_page=page_no,
                delivery_note_number=page_header.dn or header.delivery_note_number,
                destination_code=page_dest_code,
                original_carton_number=carton_no,
                destination_inherited=inherited,
            )
            doc.cartons.append(current)
        current.source_pages.append(page_no)

        for row in item_rows:
            line = normalize_line(
                row, source_file=source_file, upload_sequence=upload_sequence,
                dn=current.delivery_note_number,
                carton_no=carton_no, method=pt.method, page_text=pt,
                adapter=adapter, bands=bands, issues=issues)
            current.lines.append(line)

        for kind, raw_total in parse_total_rows(total_rows):
            value = _total_int(raw_total)
            if kind == "carton":
                current.printed_carton_total_raw = raw_total
                if value is None:
                    _issue(issues, PRINTED_TOTAL_UNREADABLE, SEV_WARNING,
                           f"Printed carton total '{raw_total}' is not "
                           "readable as a number.", source_file=source_file,
                           page=page_no, carton=carton_no, raw=raw_total)
                else:
                    current.printed_carton_total = value
            else:
                doc.printed_grand_total_raw = raw_total
                if value is None:
                    _issue(issues, PRINTED_TOTAL_UNREADABLE, SEV_WARNING,
                           f"Printed grand total '{raw_total}' is not "
                           "readable as a number.", source_file=source_file,
                           page=page_no, raw=raw_total)
                else:
                    doc.printed_grand_total = value

    if not any(c.lines for c in doc.cartons):
        _issue(issues, NO_ITEM_LINES, SEV_ERROR,
               "No item lines could be read from this document.",
               source_file=source_file)

    _validate_totals(doc, issues)
    return doc


def _validate_totals(doc: TransferDocumentExtraction,
                     issues: list[TransferExtractionIssue]) -> None:
    """Exact integer comparison; quantities are never changed to fit."""
    for carton in doc.cartons:
        carton.calculated_carton_total = sum(
            ln.normalized_quantity or 0 for ln in carton.lines)
        if carton.printed_carton_total is None:
            carton.validation_status = ("no_printed_total"
                                        if carton.printed_carton_total_raw is None
                                        else "unreadable_printed_total")
        elif carton.printed_carton_total == carton.calculated_carton_total:
            carton.validation_status = "matched"
        else:
            carton.validation_status = "mismatch"
            _issue(issues, CARTON_TOTAL_MISMATCH, SEV_ERROR,
                   f"Carton {carton.original_carton_number or '?'} printed "
                   f"total {carton.printed_carton_total} != calculated "
                   f"{carton.calculated_carton_total}.",
                   source_file=doc.source_file, page=carton.source_page,
                   carton=carton.original_carton_number,
                   field_name="carton_total",
                   raw=carton.printed_carton_total_raw)
    doc.calculated_grand_total = sum(c.calculated_carton_total
                                     for c in doc.cartons)
    if (doc.printed_grand_total is not None
            and doc.printed_grand_total != doc.calculated_grand_total):
        _issue(issues, DOCUMENT_TOTAL_MISMATCH, SEV_ERROR,
               f"Printed grand total {doc.printed_grand_total} != calculated "
               f"{doc.calculated_grand_total}.",
               source_file=doc.source_file, field_name="grand_total",
               raw=doc.printed_grand_total_raw)
