"""Transfer Note extraction, Build 2: page classification, OCR routing,
recognition, header/carton/line parsing, total validation, persistence, and
UI wiring. Fully offline - synthetic PDFs and fake OCR adapters only; no
cloud provider and no internal API is ever touched."""

import json
from pathlib import Path

import fitz
import pytest

from apps.web.job_manager import JobError
from apps.web.transfer import extraction
from apps.web.transfer import extraction_models as em
from apps.web.transfer import jobs as tjobs
from apps.web.transfer import models as tm
from apps.web.transfer.ocr import OcrError, OcrToken
from apps.web.transfer.pagetext import extract_page_texts
from apps.web.transfer.parser import (
    parse_date,
    parse_document,
    split_location,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "transfer-jobs"))
    return tmp_path


# --- synthetic Transfer Delivery Note builders ------------------------------------

DEFAULT_HEADER = dict(
    batch="ZZM11 to 101(ZE) 17-JUN",
    from_loc="ZZWHKM11 Multi Brand(Outlet)-HKWH",
    to_loc="ZZOHK101 Multi Brand(Outlet)-Office",
    pick="STOCK RETURN TO VENDOR",
    dn="ZZWHKM11-OZSO202606040285",
    date="06/06/2026",
)

ROW_A = ("1", "ZEHE380331E997", "0210116339257", "TOP - SQ NK BRA",
         "1400", "E997", "S", "1 PCS")
ROW_B = ("2", "ZETF381237E085", "0210116369698", "SRT - JAZZ SHORTS",
         "1900", "E085", "XS", "2 PCS")


def draw_note_page(page: fitz.Page, *, header=None, carton="001",
                   page_no=1, rows=(ROW_A, ROW_B), carton_total="3 UNIT",
                   grand_total=None, title=True, to_loc_override=None):
    h = dict(DEFAULT_HEADER, **(header or {}))
    if to_loc_override is not None:
        h["to_loc"] = to_loc_override
    t = page.insert_text
    if title:
        t((230, 50), "IMAGINEX OUTLET", fontsize=9)
        t((215, 65), "TRANSFER DELIVERY NOTE", fontsize=9)
    t((20, 92), "Batch", fontsize=7);   t((78, 92), ":", fontsize=7)
    t((85, 92), h["batch"], fontsize=7)
    t((20, 112), "From", fontsize=7);   t((78, 112), ":", fontsize=7)
    t((85, 112), h["from_loc"], fontsize=7)
    if h["to_loc"]:
        t((20, 132), "To Loc.", fontsize=7)
        t((78, 132), ":", fontsize=7)
        t((85, 132), h["to_loc"], fontsize=7)
    t((20, 152), "Pick Ref", fontsize=7)
    t((78, 152), ":", fontsize=7)
    t((85, 152), h["pick"], fontsize=7)
    if carton is not None:
        t((225, 152), "Carton", fontsize=7)
        t((272, 152), carton, fontsize=7)
    t((325, 152), "D/N", fontsize=7)
    if h["dn"]:
        t((380, 112), "D/N#", fontsize=7)
        t((430, 112), h["dn"], fontsize=7)
    t((380, 132), "Date", fontsize=7)
    t((430, 132), h["date"], fontsize=7)
    t((380, 152), "Page", fontsize=7)
    t((430, 152), str(page_no), fontsize=7)
    # table header
    t((25, 175), "Seq.", fontsize=7)
    t((48, 175), "Item", fontsize=7)
    t((120, 175), "EAN", fontsize=7);   t((140, 175), "Code", fontsize=7)
    t((205, 175), "Description", fontsize=7)
    t((378, 175), "Retail", fontsize=7)
    t((402, 175), "Price", fontsize=7)
    t((443, 175), "Color", fontsize=7)
    t((492, 175), "Size", fontsize=7)
    t((527, 175), "Quantity", fontsize=7)
    y = 195
    for seq, item, ean, desc, price, color, size, qty in rows:
        if seq:
            t((25, y), seq, fontsize=7)
        if item:
            t((48, y), item, fontsize=7)
        if ean:
            t((125, y), ean, fontsize=7)
        if desc:
            t((205, y), desc, fontsize=7)
        if price:
            t((385, y), price, fontsize=7)
        if color:
            t((445, y), color, fontsize=7)
        if size:
            t((495, y), size, fontsize=7)
        if qty:
            t((525, y), qty, fontsize=7)
        y += 18
    if carton_total is not None:
        t((400, y + 8), "Carton", fontsize=7)
        t((428, y + 8), "Total:", fontsize=7)
        t((510, y + 8), carton_total, fontsize=7)
        y += 18
    if grand_total is not None:
        t((402, y + 14), "Grand", fontsize=7)
        t((427, y + 14), "Total:", fontsize=7)
        t((510, y + 14), grand_total, fontsize=7)


def note_pdf(path, page_specs) -> str:
    """page_specs: list of dicts of draw_note_page kwargs."""
    doc = fitz.open()
    for spec in page_specs:
        draw_note_page(doc.new_page(), **spec)
    doc.save(str(path))
    doc.close()
    return str(path)


def image_pdf(path, pages=1) -> str:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
        pm.set_rect(pm.irect, (150, 150, 150))
        page.insert_image(fitz.Rect(50, 50, 550, 750), pixmap=pm)
    doc.save(str(path))
    doc.close()
    return str(path)


def plain_pdf(path, text="COMMERCIAL INVOICE\nItem Quantity Total 42") -> str:
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(text.split("\n")):
        page.insert_text((60, 80 + i * 20), line + " lorem ipsum dolor sit",
                         fontsize=10)
    doc.save(str(path))
    doc.close()
    return str(path)


# --- fake OCR adapters ------------------------------------------------------------

class FakeOcr:
    """Deterministic offline stand-in for the local OCR engine."""

    def __init__(self, page_tokens=None, cell_text="", cell_conf=0.5,
                 fail_pages=()):
        self.page_tokens = list(page_tokens or [])
        self.cell_text = cell_text
        self.cell_conf = cell_conf
        self.fail_pages = set(fail_pages)
        self.page_calls = 0
        self.cell_calls = []

    def recognize_page(self, png: bytes):
        self.page_calls += 1
        if self.page_calls in self.fail_pages:
            raise OcrError("simulated OCR failure")
        if not self.page_tokens:
            return []
        return self.page_tokens.pop(0)

    def recognize_cell(self, png: bytes, box):
        self.cell_calls.append(box)
        return self.cell_text, self.cell_conf


def tok(text, x, y, w=None, h=24.0, conf=0.9):   # ~200-dpi OCR token height
    w = w if w is not None else 7.0 * len(text)
    return OcrToken(text=text, x0=x, y0=y, x1=x + w, y1=y + h,
                    confidence=conf)


def ocr_note_tokens(*, carton="001", page_no=1, to_loc=None, fused_title=True,
                    rows=None, carton_total="3 UNIT", grand_total=None,
                    batch_offset=0.0, omit_sizes=False):
    """OCR-shaped tokens (chunky line fragments, fused seq+item) mirroring
    the real RapidOCR output geometry (~1654px wide page)."""
    to_loc = to_loc if to_loc is not None else "ZZOHK101Multi Brand(Outlet)-Office"
    rows = rows if rows is not None else [
        ("1ZEHE380331E997", "0210116339257", "TOP-SQ NK BRA", "1400",
         "E997", "S", "1 PCS"),
        ("2ZETF381237E085", "0210116369698", "SRT-JAZZ SHORTS", "1900",
         "E085", "XS", "2 PCS"),
    ]
    tokens = [
        tok("IMAGINEX OUTLET", 570, 55),
        tok("TRANSFERDELIVERYNOTE" if fused_title
            else "TRANSFER DELIVERY NOTE", 500, 100),
        tok("OZS0202606040285", 1030, 190),           # barcode text, top right
        tok("Batch", 40, 195), tok("ZZM11to 101(ZE)17-JUN", 180,
                                   178 + batch_offset),
        tok("From", 40, 240), tok("ZZWHKM11Multi Brand(Outlet)-HKWH", 180, 240),
        tok("D/N#", 915, 240), tok(":ZZWHKM11-OZSO202606040285", 1000, 240),
        tok("Date", 915, 285), tok("06/06/2026", 1000, 285),
        tok("Pick Ref", 40, 330), tok("STOCKRETURNTOVENDOR", 180, 330),
        tok("Carton", 520, 330), tok(carton, 610, 330) if carton else None,
        tok("D/N", 700, 330),
        tok("Page", 915, 330), tok(str(page_no), 990, 330),
        tok("Seq.", 55, 374), tok("Item", 105, 374), tok("EAN Code", 335, 374),
        tok("Description", 605, 374), tok("Retail Price", 1065, 374),
        tok("Color", 1215, 374), tok("Size", 1390, 374),
        tok("Quantity", 1500, 374),
    ]
    if to_loc:
        tokens += [tok("To Loc.", 40, 285), tok(to_loc, 180, 285)]
    y = 418
    for item, ean, desc, price, color, size, qty in rows:
        tokens += [tok(item, 98, y), tok(ean, 334, y), tok(desc, 606, y),
                   tok(price, 1065, y), tok(color, 1211, y)]
        if size and not omit_sizes:
            tokens.append(tok(size, 1395, y))
        tokens.append(tok(qty, 1542, y))
        y += 44
    if carton_total:
        tokens.append(tok("Carton Total:", 1140, y + 10))
        tokens.append(tok(carton_total, 1520, y + 10))
        y += 44
    if grand_total:
        tokens.append(tok("Grand Total:", 1135, y + 20))
        tokens.append(tok(grand_total, 1520, y + 20))
    return [t for t in tokens if t is not None]


def parse_pdf(path, adapter=None):
    pages = extract_page_texts(str(path), ocr_adapter=adapter)
    return parse_document(source_file=Path(path).name, upload_sequence=1,
                          pages=pages, adapter=adapter)


# --- page classification ----------------------------------------------------------

class TestPageClassification:
    def test_text_native_page_uses_embedded_text_never_ocr(self, tmp_path):
        path = note_pdf(tmp_path / "a.pdf", [{}])

        class Exploding:
            def recognize_page(self, png):
                raise AssertionError("OCR must not run for text pages")

            def recognize_cell(self, png, box):
                raise AssertionError("no cell OCR for text pages")

        pages = extract_page_texts(path, ocr_adapter=Exploding())
        assert [p.method for p in pages] == [em.METHOD_EMBEDDED]

    def test_image_page_routed_to_ocr_adapter(self, tmp_path):
        path = image_pdf(tmp_path / "s.pdf")
        fake = FakeOcr([ocr_note_tokens()])
        pages = extract_page_texts(path, ocr_adapter=fake)
        assert [p.method for p in pages] == [em.METHOD_OCR]
        assert fake.page_calls == 1
        assert pages[0].image_png is not None      # kept in memory only

    def test_image_page_without_adapter_is_unreadable(self, tmp_path):
        path = image_pdf(tmp_path / "s.pdf")
        pages = extract_page_texts(path, ocr_adapter=None)
        assert pages[0].method == em.METHOD_UNREADABLE
        assert pages[0].error == "ocr_unavailable"

    def test_mixed_pdf_per_page_methods(self, tmp_path):
        doc = fitz.open()
        draw_note_page(doc.new_page(), page_no=1)
        page2 = doc.new_page()
        pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 30, 30), False)
        page2.insert_image(fitz.Rect(40, 40, 560, 760), pixmap=pm)
        path = tmp_path / "m.pdf"
        doc.save(str(path))
        doc.close()
        fake = FakeOcr([ocr_note_tokens(carton="002", page_no=2)])
        pages = extract_page_texts(str(path), ocr_adapter=fake)
        assert [p.method for p in pages] == [em.METHOD_EMBEDDED, em.METHOD_OCR]

    def test_one_failing_page_keeps_the_others(self, tmp_path):
        path = image_pdf(tmp_path / "s.pdf", pages=2)
        fake = FakeOcr([ocr_note_tokens(),
                        ocr_note_tokens(carton="002", page_no=2)],
                       fail_pages={2})
        pages = extract_page_texts(path, ocr_adapter=fake)
        assert pages[0].method == em.METHOD_OCR
        assert pages[1].method == em.METHOD_UNREADABLE
        doc = parse_document(source_file="s.pdf", upload_sequence=1,
                             pages=pages, adapter=fake)
        assert doc.pages_ocr == 1 and doc.pages_unreadable == 1
        assert any(i.code == em.UNREADABLE_PAGE for i in doc.issues)
        assert len(doc.cartons) == 1               # page 1 still extracted


# --- document recognition ---------------------------------------------------------

class TestRecognition:
    def test_valid_note_recognized(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{}]))
        assert doc.recognized is True
        assert not any(i.code == em.UNRECOGNIZED_DOCUMENT for i in doc.issues)

    def test_unrelated_pdf_rejected_not_silently_accepted(self, tmp_path):
        doc = parse_pdf(plain_pdf(tmp_path / "x.pdf"))
        assert doc.recognized is False
        issues = [i for i in doc.issues if i.code == em.UNRECOGNIZED_DOCUMENT]
        assert issues and issues[0].severity == em.SEV_ERROR
        assert doc.cartons == []

    def test_partial_ocr_markers_still_recognized(self, tmp_path):
        # no title, no IMAGINEX - but D/N#, To Loc., Pick Ref, EAN Code
        tokens = [t for t in ocr_note_tokens(fused_title=False)
                  if "TRANSFER" not in t.text and "IMAGINEX" not in t.text]
        doc = parse_document(
            source_file="s.pdf", upload_sequence=1,
            pages=[_ocr_pagetext(tokens)], adapter=None)
        assert doc.recognized is True

    def test_fused_ocr_title_counts(self, tmp_path):
        fake = FakeOcr([ocr_note_tokens(fused_title=True)])
        doc = parse_pdf(image_pdf(tmp_path / "s.pdf"), adapter=fake)
        assert doc.recognized is True


def _ocr_pagetext(tokens, page_number=1):
    from apps.web.transfer.pagetext import PageText, TextSpan
    spans = [TextSpan(text=t.text, x0=t.x0, y0=t.y0, x1=t.x1, y1=t.y1,
                      confidence=t.confidence) for t in tokens]
    return PageText(page_number, em.METHOD_OCR, spans=spans)


# --- header parsing ---------------------------------------------------------------

class TestHeaderParsing:
    def test_all_header_fields_from_text_native_page(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{}]))
        h = doc.header
        assert h.batch_reference == "ZZM11 to 101(ZE) 17-JUN"
        assert h.from_location_code == "ZZWHKM11"
        assert h.from_location_name == "Multi Brand(Outlet)-HKWH"
        assert h.to_location_code == "ZZOHK101"
        assert h.to_location_name == "Multi Brand(Outlet)-Office"
        assert h.pick_reference == "STOCK RETURN TO VENDOR"
        assert h.delivery_note_number == "ZZWHKM11-OZSO202606040285"
        assert h.delivery_date_raw == "06/06/2026"
        assert h.delivery_date == "2026-06-06"
        assert h.declared_page_number == 1
        assert h.to_location_raw.startswith("ZZOHK101")

    def test_fused_ocr_location_split(self):
        code, name = split_location("ZZOHK101Multi Brand(Outlet)-Office")
        assert code == "ZZOHK101"
        assert name == "Multi Brand(Outlet)-Office"

    def test_location_split_spaced_and_case(self):
        code, name = split_location("zzohk101 Multi Brand(Outlet)")
        assert code == "ZZOHK101"                  # codes uppercased
        assert name == "Multi Brand(Outlet)"       # names keep their case

    def test_dates_normalized_day_first(self):
        assert parse_date("06/06/2026") == "2026-06-06"
        assert parse_date("17/06/2026") == "2026-06-17"   # day-first
        assert parse_date("06/17/2026") == "2026-06-17"   # month-first fallback
        assert parse_date("not a date") is None

    def test_header_from_ocr_page_with_offset_batch_value(self, tmp_path):
        # the real sample: the Batch VALUE baseline sits a row above the
        # label; the widened label band must still capture it, and the
        # barcode text top-right must never leak into Batch.
        fake = FakeOcr([ocr_note_tokens(batch_offset=-6.0)])
        doc = parse_pdf(image_pdf(tmp_path / "s.pdf"), adapter=fake)
        assert doc.header.batch_reference == "ZZM11to 101(ZE)17-JUN"
        assert "OZS0" not in (doc.header.batch_reference or "")
        assert doc.header.delivery_note_number == "ZZWHKM11-OZSO202606040285"
        assert doc.header.to_location_code == "ZZOHK101"

    def test_missing_destination_is_an_error_never_from_filename(self, tmp_path):
        path = note_pdf(tmp_path / "ZZOHK999_hint.pdf",
                        [{"to_loc_override": ""}])
        doc = parse_pdf(path)
        issues = [i for i in doc.issues if i.code == em.MISSING_DESTINATION]
        assert issues and issues[0].severity == em.SEV_ERROR
        assert doc.header.to_location_code is None   # filename never used

    def test_conflicting_destinations_flagged_ambiguous(self, tmp_path):
        path = note_pdf(tmp_path / "a.pdf", [
            {"page_no": 1},
            {"page_no": 2, "carton": "002",
             "to_loc_override": "ZZOHK202 Другой Office"},
        ])
        doc = parse_pdf(path)
        assert any(i.code == em.AMBIGUOUS_DESTINATION for i in doc.issues)

    def test_single_destination_inherited_to_page_without_one(self, tmp_path):
        path = note_pdf(tmp_path / "a.pdf", [
            {"page_no": 1},
            {"page_no": 2, "carton": "002", "to_loc_override": ""},
        ])
        doc = parse_pdf(path)
        assert not any(i.code in (em.MISSING_DESTINATION,
                                  em.AMBIGUOUS_DESTINATION)
                       for i in doc.issues)
        second = doc.cartons[1]
        assert second.destination_code == "ZZOHK101"
        assert second.destination_inherited is True
        assert doc.cartons[0].destination_inherited is False


# --- carton parsing ---------------------------------------------------------------

class TestCartonParsing:
    def test_leading_zero_carton_number_preserved(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{"carton": "001"}]))
        assert doc.cartons[0].original_carton_number == "001"

    def test_multiple_cartons_keep_page_and_upload_order(self, tmp_path):
        path = note_pdf(tmp_path / "a.pdf", [
            {"page_no": 1, "carton": "001"},
            {"page_no": 2, "carton": "002"},
            {"page_no": 3, "carton": "003"},
        ])
        doc = parse_pdf(path)
        assert [c.original_carton_number for c in doc.cartons] == [
            "001", "002", "003"]
        assert [c.source_page for c in doc.cartons] == [1, 2, 3]
        assert all(c.upload_sequence == 1 for c in doc.cartons)

    def test_carton_spanning_two_pages_is_one_carton(self, tmp_path):
        path = note_pdf(tmp_path / "a.pdf", [
            {"page_no": 1, "carton": "007", "carton_total": None},
            {"page_no": 2, "carton": "007",
             "rows": (("3", "ZEGO400074E997", "0210116799983",
                       "TOP - SCUBA BODY", "900", "E997", "M", "1 PCS"),),
             "carton_total": "4 UNIT"},
        ])
        doc = parse_pdf(path)
        assert len(doc.cartons) == 1
        carton = doc.cartons[0]
        assert carton.source_pages == [1, 2]
        assert len(carton.lines) == 3
        assert carton.printed_carton_total == 4
        assert carton.calculated_carton_total == 4     # 1+2+1

    def test_missing_carton_number_issue_never_invented(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{"carton": None}]))
        assert doc.cartons[0].original_carton_number is None
        assert any(i.code == em.MISSING_CARTON_NO
                   and i.severity == em.SEV_ERROR for i in doc.issues)

    def test_printed_carton_total_parsed_with_raw(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"carton_total": "3 UNIT"}]))
        carton = doc.cartons[0]
        assert carton.printed_carton_total == 3
        assert carton.printed_carton_total_raw == "3"


# --- item line parsing ------------------------------------------------------------

class TestLineParsing:
    def test_valid_row_fully_normalized(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{}]))
        line = doc.cartons[0].lines[0]
        assert line.source_sequence_number == 1
        assert line.normalized_item_code == "ZEHE380331E997"
        assert line.normalized_ean == "0210116339257"    # leading zero kept
        assert line.raw_ean == "0210116339257"
        assert line.normalized_description == "TOP - SQ NK BRA"
        assert line.normalized_retail_price == "1400"
        assert line.normalized_color_code == "E997"
        assert line.normalized_size_code == "S"
        assert line.raw_quantity == "1 PCS"
        assert line.normalized_quantity == 1
        assert line.extraction_method == em.METHOD_EMBEDDED

    def test_quantity_formats(self, tmp_path):
        rows = (
            ("1", "ZEAA111111E001", "0210000000011", "A", "100", "E001", "S",
             "1 PCS"),
            ("2", "ZEAA111111E002", "0210000000012", "B", "100", "E002", "M",
             "2 PCS"),
            ("3", "ZEAA111111E003", "0210000000013", "C", "100", "E003", "L",
             "35 UNIT"),
        )
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"rows": rows, "carton_total": "38 UNIT"}]))
        assert [ln.normalized_quantity
                for ln in doc.cartons[0].lines] == [1, 2, 35]

    def test_invalid_quantity_kept_with_issue(self, tmp_path):
        rows = (("1", "ZEAA111111E001", "0210000000011", "A", "100", "E001",
                 "S", "N/A"),)
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"rows": rows, "carton_total": None}]))
        line = doc.cartons[0].lines[0]
        assert line.normalized_quantity is None
        assert line.raw_quantity == "N/A"
        assert any(i.code == em.INVALID_QUANTITY and i.severity == em.SEV_ERROR
                   for i in doc.issues)

    def test_decimal_retail_price(self, tmp_path):
        rows = (("1", "ZEAA111111E001", "0210000000011", "A", "1400.50",
                 "E001", "S", "1 PCS"),)
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{"rows": rows}]))
        assert doc.cartons[0].lines[0].normalized_retail_price == "1400.50"

    def test_multiword_description_and_wrap_merged(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        draw_note_page(page, rows=(
            ("1", "ZEER388975E085", "0210116587870",
             "SKI - TANAGRA_SHORT SARONG", "2050", "E085", "F", "1 PCS"),))
        page.insert_text((205, 213), "ATTACHED BELT", fontsize=7)  # wrap line
        path = tmp_path / "w.pdf"
        doc.save(str(path))
        doc.close()
        parsed = parse_pdf(path)
        line = parsed.cartons[0].lines[0]
        assert line.normalized_description == (
            "SKI - TANAGRA_SHORT SARONG ATTACHED BELT")

    def test_missing_ean_with_item_present_warns_and_keeps_row(self, tmp_path):
        rows = (("1", "ZEAA111111E001", "", "A THING", "100", "E001", "S",
                 "1 PCS"),)
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{"rows": rows}]))
        line = doc.cartons[0].lines[0]
        assert line.normalized_item_code == "ZEAA111111E001"
        assert line.normalized_ean is None
        assert any(i.code == em.INVALID_EAN and i.severity == em.SEV_WARNING
                   for i in doc.issues)
        assert not any(i.code == em.MISSING_ITEM_IDENTIFIER
                       for i in doc.issues)

    def test_malformed_row_retained_with_issue(self, tmp_path):
        rows = (ROW_A,
                ("2", "ZEXXBROKEN99", "", "", "", "", "", ""))
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"rows": rows, "carton_total": None}]))
        assert len(doc.cartons[0].lines) == 2      # never silently dropped
        assert any(i.code == em.MALFORMED_ITEM_ROW for i in doc.issues)

    def test_row_order_preserved(self, tmp_path):
        rows = (("9", "ZEAA111111E009", "0210000000019", "NINTH", "100",
                 "E009", "S", "1 PCS"),
                ("1", "ZEAA111111E001", "0210000000011", "FIRST", "100",
                 "E001", "S", "1 PCS"))
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{"rows": rows}]))
        assert [ln.source_sequence_number
                for ln in doc.cartons[0].lines] == [9, 1]   # source order

    def test_fused_seq_item_ocr_token_split(self, tmp_path):
        fake = FakeOcr([ocr_note_tokens()])
        doc = parse_pdf(image_pdf(tmp_path / "s.pdf"), adapter=fake)
        line = doc.cartons[0].lines[0]
        assert line.source_sequence_number == 1
        assert line.normalized_item_code == "ZEHE380331E997"
        assert line.extraction_method == em.METHOD_OCR
        assert line.extraction_confidence is not None

    def test_cell_rescue_recovers_single_letter_size(self, tmp_path):
        fake = FakeOcr([ocr_note_tokens(omit_sizes=True)],
                       cell_text="Ｓ", cell_conf=0.12)   # fullwidth S
        doc = parse_pdf(image_pdf(tmp_path / "s.pdf"), adapter=fake)
        line = doc.cartons[0].lines[0]
        assert line.normalized_size_code == "S"    # NFKC-folded, rescued
        assert fake.cell_calls                     # crop OCR actually ran
        assert not any(i.code == em.MISSING_SIZE and i.line_ref == 1
                       for i in doc.issues)

    def test_missing_color_and_size_warn(self, tmp_path):
        rows = (("1", "ZEAA111111E001", "0210000000011", "A", "100", "",
                 "", "1 PCS"),)
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf", [{"rows": rows}]))
        codes = {i.code for i in doc.issues}
        assert em.MISSING_COLOR in codes and em.MISSING_SIZE in codes


# --- totals -----------------------------------------------------------------------

class TestTotals:
    def test_carton_total_match(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"carton_total": "3 UNIT"}]))
        assert doc.cartons[0].validation_status == "matched"
        assert not any(i.code == em.CARTON_TOTAL_MISMATCH for i in doc.issues)

    def test_carton_total_mismatch_identifies_both_numbers(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"carton_total": "9 UNIT"}]))
        carton = doc.cartons[0]
        assert carton.validation_status == "mismatch"
        assert carton.printed_carton_total == 9
        assert carton.calculated_carton_total == 3     # quantities unchanged
        issue = next(i for i in doc.issues
                     if i.code == em.CARTON_TOTAL_MISMATCH)
        assert "9" in issue.message and "3" in issue.message
        assert issue.carton == "001" and issue.source_page == 1

    def test_document_total_match_and_mismatch(self, tmp_path):
        ok = parse_pdf(note_pdf(tmp_path / "ok.pdf",
                                [{"grand_total": "3 UNIT"}]))
        assert ok.printed_grand_total == 3
        assert not any(i.code == em.DOCUMENT_TOTAL_MISMATCH for i in ok.issues)
        bad = parse_pdf(note_pdf(tmp_path / "bad.pdf",
                                 [{"grand_total": "99 UNIT"}]))
        issue = next(i for i in bad.issues
                     if i.code == em.DOCUMENT_TOTAL_MISMATCH)
        assert "99" in issue.message and "3" in issue.message

    def test_missing_printed_total_is_not_an_error(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"carton_total": None}]))
        assert doc.cartons[0].validation_status == "no_printed_total"
        assert not any(i.code in (em.CARTON_TOTAL_MISMATCH,
                                  em.PRINTED_TOTAL_UNREADABLE)
                       for i in doc.issues)

    def test_unreadable_printed_total_warns(self, tmp_path):
        doc = parse_pdf(note_pdf(tmp_path / "a.pdf",
                                 [{"carton_total": "3S UNIT"}]))
        carton = doc.cartons[0]
        assert carton.printed_carton_total is None
        assert carton.printed_carton_total_raw == "3S"
        assert any(i.code == em.PRINTED_TOTAL_UNREADABLE
                   and i.severity == em.SEV_WARNING for i in doc.issues)


# --- persistence + job state ------------------------------------------------------

def _job_with(tmp_path, files):
    uploads = []
    for name, path in files:
        uploads.append((name, Path(path).read_bytes()))
    validated, issues = tjobs.validate_transfer_uploads(uploads)
    assert issues == []
    return tjobs.create_transfer_job(uploads, validated)


class TestPersistence:
    def test_extraction_persists_atomic_versioned_result(self, tmp_path):
        pdf = note_pdf(tmp_path / "n.pdf", [{"grand_total": "3 UNIT"}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        result = extraction.run_extraction(job_id, use_default_adapter=False)
        path = extraction.result_path(job_id)
        assert path.is_file()
        raw = json.loads(path.read_text())
        assert raw["schema_version"] == em.EXTRACTION_SCHEMA_VERSION
        assert raw["summary"]["total_units"] == 3
        assert not list(path.parent.glob("*.tmp-*"))
        assert tjobs.load_transfer_job(job_id).status == tm.JOB_EXTRACTED
        assert result.summary()["recognized_documents"] == 1

    def test_refresh_reload_round_trip(self, tmp_path):
        pdf = note_pdf(tmp_path / "n.pdf", [{}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        first = extraction.run_extraction(job_id, use_default_adapter=False)
        reloaded = extraction.load_result(job_id)
        assert reloaded is not None
        assert reloaded.as_dict() == first.as_dict()

    def test_retry_replaces_never_duplicates(self, tmp_path):
        pdf = note_pdf(tmp_path / "n.pdf", [{}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        extraction.run_extraction(job_id, use_default_adapter=False)
        again = extraction.run_extraction(job_id, use_default_adapter=False)
        assert len(again.documents) == 1
        reloaded = extraction.load_result(job_id)
        assert len(reloaded.documents) == 1
        assert len(list(extraction.result_path(job_id).parent.iterdir())) == 1

    def test_failed_document_keeps_successful_one(self, tmp_path):
        good = note_pdf(tmp_path / "good.pdf", [{}])
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 truncated garbage")
        job_id = _job_with(tmp_path, [("good.pdf", good)])
        # sneak a corrupt stored file in AFTER validation (simulates a file
        # that passed upload checks but cannot be parsed at extraction time)
        job_dir = tjobs.transfer_job_dir_for(job_id)
        (job_dir / "input" / "002-bad.pdf").write_bytes(bad.read_bytes())
        job = tjobs.load_transfer_job(job_id)
        from apps.web.transfer.models import TransferUploadFile
        job.files.append(TransferUploadFile(
            sequence=2, original_name="bad.pdf", stored_name="002-bad.pdf",
            size_bytes=10, status="VALIDATED"))
        tjobs._write_metadata(job_dir, job)
        result = extraction.run_extraction(job_id, use_default_adapter=False)
        assert len(result.documents) == 2
        assert result.documents[0].recognized is True       # kept
        failed = result.documents[1]
        assert any(i.code == em.DOCUMENT_EXTRACTION_FAILED
                   for i in failed.issues)
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_EXTRACTED_WITH_ISSUES)

    def test_issue_status_when_errors_present(self, tmp_path):
        pdf = plain_pdf(tmp_path / "x.pdf")                 # unrecognized
        job_id = _job_with(tmp_path, [("x.pdf", pdf)])
        extraction.run_extraction(job_id, use_default_adapter=False)
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_EXTRACTED_WITH_ISSUES)

    def test_upload_sequence_recorded_on_lines(self, tmp_path):
        a = note_pdf(tmp_path / "a.pdf", [{}])
        b = note_pdf(tmp_path / "b.pdf", [{"carton": "002", "header": {
            "dn": "ZZWHKM11-OZSO202606049999"}}])
        job_id = _job_with(tmp_path, [("zz-first.pdf", a),
                                      ("aa-second.pdf", b)])
        result = extraction.run_extraction(job_id, use_default_adapter=False)
        assert [d.upload_sequence for d in result.documents] == [1, 2]
        assert [d.source_file for d in result.documents] == [
            "zz-first.pdf", "aa-second.pdf"]     # upload order, not name order
        assert result.documents[1].cartons[0].lines[0].upload_sequence == 2

    def test_cancelled_job_cannot_extract(self, tmp_path):
        pdf = note_pdf(tmp_path / "n.pdf", [{}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        tjobs.update_job_status(job_id, tm.JOB_CANCELLED)
        with pytest.raises(JobError):
            extraction.run_extraction(job_id, use_default_adapter=False)

    def test_invalid_transition_rejected(self, tmp_path):
        pdf = note_pdf(tmp_path / "n.pdf", [{}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        with pytest.raises(JobError):
            tjobs.update_job_status(job_id, tm.JOB_EXTRACTED)  # skip EXTRACTING

    def test_stale_extracting_job_can_retry(self, tmp_path):
        pdf = note_pdf(tmp_path / "n.pdf", [{}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        tjobs.update_job_status(job_id, tm.JOB_EXTRACTING)   # simulated crash
        result = extraction.run_extraction(job_id, use_default_adapter=False)
        assert len(result.documents) == 1
        assert tjobs.load_transfer_job(job_id).status == tm.JOB_EXTRACTED

    def test_invoice_loader_still_isolated(self, tmp_path):
        from apps.web import job_manager
        from apps.web.progress import read_status
        pdf = note_pdf(tmp_path / "n.pdf", [{}])
        job_id = _job_with(tmp_path, [("note.pdf", pdf)])
        extraction.run_extraction(job_id, use_default_adapter=False)
        assert not job_manager.JOB_ID_RE.match(job_id)
        with pytest.raises(job_manager.JobError):
            job_manager.job_dir_for(job_id)
        # no invoice-format status.json ever appears in a transfer job dir
        assert read_status(tjobs.transfer_job_dir_for(job_id)) is None


# --- UI wiring (static) -----------------------------------------------------------

class TestUiWiring:
    PAGE = (ROOT / "apps" / "web" / "transfer" / "page.py").read_text(
        encoding="utf-8")

    def test_extract_button_gated_by_extractable_states(self):
        assert "Extract Transfer Notes" in self.PAGE
        assert "EXTRACTABLE_STATUSES" in self.PAGE

    def test_summary_and_issue_rendering_present(self):
        assert "Extraction summary" in self.PAGE
        assert "Blocking errors" in self.PAGE
        assert "Extraction issues - source record" in self.PAGE

    def test_no_product_api_or_packing_list_controls(self):
        low = self.PAGE.lower()
        for forbidden in ("plulabel", "access_token", "download",
                          "openpyxl", "api gateway", "httpx"):
            assert forbidden not in low, forbidden

    def test_transfer_session_keys_still_prefixed(self):
        import re
        keys = set(re.findall(r'session_state\[["\']([^"\']+)["\']\]',
                              self.PAGE))
        keys |= set(re.findall(r'session_state\.get\(["\']([^"\']+)["\']',
                               self.PAGE))
        assert keys and all(k.startswith("transfer_") for k in keys)

    def test_extraction_modules_never_call_cloud_or_gateway(self):
        for name in ("extraction.py", "parser.py", "pagetext.py", "ocr.py",
                     "extraction_models.py"):
            src = (ROOT / "apps" / "web" / "transfer" / name).read_text(
                encoding="utf-8").lower()
            for forbidden in ("openrouter", "gemini", "claude", "httpx",
                              "requests.", "plulabel", "api_gateway",
                              "access_token", "openpyxl"):
                assert forbidden not in src, f"{name}: {forbidden}"
