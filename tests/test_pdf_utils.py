from invoice_extractor import pdf_utils
from invoice_extractor.pdf_utils import PAGE_BLANK, PAGE_IMAGE, PAGE_TEXT

from .conftest import TEXT_BODY, build_pdf

THRESHOLD = 20


def kinds(path):
    return [p.kind for p in pdf_utils.analyze_pages(path, THRESHOLD)]


class TestThreshold:
    def test_below_threshold_is_not_text(self, pdf_factory):
        # 5 alnum chars, no images -> too little text to trust -> image route
        path = pdf_factory([("text", "ABCDE")])
        assert kinds(path) == [PAGE_IMAGE]

    def test_equal_threshold_is_not_text(self, pdf_factory):
        path = pdf_factory([("text", "A" * THRESHOLD)])
        pages = pdf_utils.analyze_pages(path, THRESHOLD)
        assert pages[0].alnum_chars == THRESHOLD
        assert pages[0].kind == PAGE_IMAGE  # strict '>' comparison

    def test_above_threshold_is_text(self, pdf_factory):
        path = pdf_factory([("text", "A" * (THRESHOLD + 1))])
        assert kinds(path) == [PAGE_TEXT]

    def test_unicode_alphanumeric_counts(self, pdf_factory):
        # CJK characters count as alphanumeric at the classifier level
        assert pdf_utils.alnum_count("貨運費用發票明細單號一二三四五六七八九十甲乙") > THRESHOLD
        # and a PDF with non-ASCII European text routes as text-native
        # (the PDF builder's base font cannot embed CJK glyphs, so the
        # in-PDF check uses accented Latin text instead)
        body = "Fährgebühren für Überführung nach Zürich, Rechnungsnummer FÜNF"
        path = pdf_factory([("text", body)])
        pages = pdf_utils.analyze_pages(path, THRESHOLD)
        assert pages[0].kind == PAGE_TEXT
        assert pages[0].alnum_chars > THRESHOLD


class TestBlankAndPunctuation:
    def test_blank_page(self, pdf_factory):
        assert kinds(pdf_factory([("blank",)])) == [PAGE_BLANK]

    def test_punctuation_only_page_is_blank(self, pdf_factory):
        assert kinds(pdf_factory([("text", "!!! *** ---- ....")])) == [PAGE_BLANK]


class TestPageKinds:
    def test_text_native_pdf(self, pdf_factory):
        path = pdf_factory([("text", TEXT_BODY), ("text", TEXT_BODY)])
        assert kinds(path) == [PAGE_TEXT, PAGE_TEXT]
        assert pdf_utils.classify_document(pdf_utils.analyze_pages(path, THRESHOLD)) == "text-native"

    def test_image_only_pdf(self, pdf_factory):
        path = pdf_factory([("image",)])
        assert kinds(path) == [PAGE_IMAGE]
        assert pdf_utils.classify_document(pdf_utils.analyze_pages(path, THRESHOLD)) == "image-only"

    def test_mixed_pdf_pages_route_independently(self, pdf_factory):
        path = pdf_factory([("text", TEXT_BODY), ("image",), ("blank",)])
        pages = pdf_utils.analyze_pages(path, THRESHOLD)
        assert [p.kind for p in pages] == [PAGE_TEXT, PAGE_IMAGE, PAGE_BLANK]
        assert [p.number for p in pages] == [1, 2, 3]
        assert pdf_utils.classify_document(pages) == "mixed"

    def test_all_blank_document_is_error_classification(self, pdf_factory):
        path = pdf_factory([("blank",), ("blank",)])
        pages = pdf_utils.analyze_pages(path, THRESHOLD)
        assert pdf_utils.classify_document(pages) == "error"


class TestCorruptPdf:
    def test_corrupt_pdf_raises(self, tmp_path):
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"this is definitely not a PDF file")
        try:
            pdf_utils.analyze_pages(str(bad), THRESHOLD)
        except Exception:
            return
        raise AssertionError("expected analyze_pages to raise on corrupt input")


class TestFormatPageRanges:
    def test_representative_cases(self):
        assert pdf_utils.format_page_ranges([]) == ""
        assert pdf_utils.format_page_ranges([1]) == "1"
        assert pdf_utils.format_page_ranges([1, 2, 3]) == "1-3"
        assert pdf_utils.format_page_ranges([1, 2, 5, 6, 7]) == "1-2,5-7"
        assert pdf_utils.format_page_ranges([2, 9]) == "2,9"

    def test_output_is_sorted_and_deduplicated(self):
        assert pdf_utils.format_page_ranges([3, 1, 2, 3]) == "1-3"


class TestRendering:
    def test_render_selected_pages_in_order(self, pdf_factory):
        path = pdf_factory([("text", "page one " + TEXT_BODY), ("image",), ("blank",)])
        images = pdf_utils.render_pages_png(path, [3, 1], dpi=72)
        assert len(images) == 2
        for png in images:
            assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_ignores_out_of_range_pages(self, pdf_factory):
        path = pdf_factory([("image",)])
        images = pdf_utils.render_pages_png(path, [1, 99], dpi=72)
        assert len(images) == 1
