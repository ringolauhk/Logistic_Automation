"""Deterministic offline routing/classification tests.

MILESTONE 3 SCOPE: runs the synthetic pack's generated PDFs through the REAL
application's page-classification code (invoice_extractor.pdf_utils) - no
providers, no mocking, no full pipeline (invoice_extractor.pipeline.
process_file / process_directory is never called), no aggregation, no
validation, no Excel export.

The one exception is `invoice_extractor.pipeline._chunked` - a small, pure,
already-existing helper this milestone imports directly (not via running the
pipeline) to test chunk-plan derivation; see the module docstring on
TestChunkPlans below for why this is in scope and why it isn't moved/renamed.

Test-file performance: all ten fixtures are built ONCE per module (not once
per test) via a module-scoped fixture, since the image-heavy fixtures
(2, 3, 9) are the module's dominant cost - see the milestone report for the
measured duration.
"""

from pathlib import Path

import fitz
import pytest

from invoice_extractor import pdf_utils
from invoice_extractor.pipeline import _chunked

from .synthetic_fixtures import builders as b
from .synthetic_fixtures import ground_truth as gt
from .synthetic_fixtures import scenarios as sc

THRESHOLD = 20  # matches invoice_extractor.config's TEXT_QUALITY_THRESHOLD default,
                # and is the value every ground-truth page_kind was designed against.

ALL_CORES = gt.ALL_CORES
ALL_FIXTURE_IDS = [core.fixture_id for core in ALL_CORES]


@pytest.fixture(scope="module")
def fixture_paths(tmp_path_factory):
    """Build all ten synthetic PDFs ONCE for the whole module."""
    output_dir = tmp_path_factory.mktemp("synthetic_routing")
    return {core.fixture_id: sc.build_scenario(core.fixture_id, output_dir / core.filename)
            for core in ALL_CORES}


def _core_for(fixture_id: str) -> gt.ExpectedScenarioCore:
    return next(c for c in ALL_CORES if c.fixture_id == fixture_id)


def _analyze(path: Path) -> tuple[list, str]:
    """Call the REAL application classification API - not a reimplementation."""
    pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
    doc_class = pdf_utils.classify_document(pages)
    return pages, doc_class


# ---------------------------------------------------------------------------
# Generic per-fixture routing assertions (Step 3)
# ---------------------------------------------------------------------------

class TestGenericRouting:
    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_filename_matches(self, fixture_paths, fixture_id):
        core = _core_for(fixture_id)
        assert fixture_paths[fixture_id].name == core.filename

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_page_count_matches(self, fixture_paths, fixture_id):
        core = _core_for(fixture_id)
        pages, _ = _analyze(fixture_paths[fixture_id])
        assert len(pages) == len(core.page_layout)

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_page_numbers_one_based_and_ordered(self, fixture_paths, fixture_id):
        pages, _ = _analyze(fixture_paths[fixture_id])
        assert [p.number for p in pages] == list(range(1, len(pages) + 1))

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_every_page_kind_matches_ground_truth(self, fixture_paths, fixture_id):
        core = _core_for(fixture_id)
        pages, _ = _analyze(fixture_paths[fixture_id])
        actual = [p.kind for p in pages]
        expected = [pl.page_kind for pl in core.page_layout]
        assert actual == expected

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_document_classification_matches(self, fixture_paths, fixture_id):
        core = _core_for(fixture_id)
        _, doc_class = _analyze(fixture_paths[fixture_id])
        assert doc_class == core.expected_document_classification

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_text_image_blank_page_lists_match(self, fixture_paths, fixture_id):
        core = _core_for(fixture_id)
        pages, _ = _analyze(fixture_paths[fixture_id])
        text_pages = tuple(p.number for p in pages if p.kind == pdf_utils.PAGE_TEXT)
        image_pages = tuple(p.number for p in pages if p.kind == pdf_utils.PAGE_IMAGE)
        blank_pages = tuple(p.number for p in pages if p.kind == pdf_utils.PAGE_BLANK)
        assert text_pages == core.expected_text_pages
        assert image_pages == core.expected_image_pages
        assert blank_pages == core.expected_blank_pages

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_no_page_omitted_or_classified_twice(self, fixture_paths, fixture_id):
        core = _core_for(fixture_id)
        pages, _ = _analyze(fixture_paths[fixture_id])
        numbers = [p.number for p in pages]
        assert len(numbers) == len(set(numbers))  # no duplicate classification
        assert set(numbers) == set(range(1, len(core.page_layout) + 1))  # none omitted

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_no_unexpected_extraction_error(self, fixture_paths, fixture_id):
        # analyze_pages/classify_document raise on genuine failure; reaching
        # this point at all means no exception occurred. classify_document
        # must not report DOC_ERROR for any of these ten well-formed fixtures.
        _, doc_class = _analyze(fixture_paths[fixture_id])
        assert doc_class != pdf_utils.DOC_ERROR


# ---------------------------------------------------------------------------
# Fixture-specific routing assertions (Step 4)
# ---------------------------------------------------------------------------

class TestFixture01Routing:
    def test_pages_123_are_text_no_image_or_blank(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_01_multipage_text_native"])
        assert [p.number for p in pages if p.kind == pdf_utils.PAGE_TEXT] == [1, 2, 3]
        assert not any(p.kind == pdf_utils.PAGE_IMAGE for p in pages)
        assert not any(p.kind == pdf_utils.PAGE_BLANK for p in pages)
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE


class TestFixture02Routing:
    def test_seven_image_pages_zero_text_at_least_one_image_each(self, fixture_paths):
        path = fixture_paths["fixture_02_multipage_scanned_exceeds_limit"]
        pages, doc_class = _analyze(path)
        assert len(pages) == 7
        assert all(p.kind == pdf_utils.PAGE_IMAGE for p in pages)
        assert all(p.alnum_chars == 0 for p in pages)
        doc = fitz.open(str(path))
        try:
            for i in range(7):
                assert len(doc[i].get_images(full=True)) >= 1, f"page {i+1}"
        finally:
            doc.close()
        assert doc_class == pdf_utils.DOC_IMAGE_ONLY


class TestFixture03Routing:
    def test_exact_page_sequence(self, fixture_paths):
        path = fixture_paths["fixture_03_mixed_text_scan_blank"]
        pages, doc_class = _analyze(path)
        assert [p.kind for p in pages] == [
            pdf_utils.PAGE_TEXT, pdf_utils.PAGE_BLANK,
            pdf_utils.PAGE_IMAGE, pdf_utils.PAGE_IMAGE, pdf_utils.PAGE_TEXT,
        ]
        assert doc_class == pdf_utils.DOC_MIXED

    def test_blank_page_has_zero_text_zero_images_zero_drawings(self, fixture_paths):
        path = fixture_paths["fixture_03_mixed_text_scan_blank"]
        pages, _ = _analyze(path)
        assert pages[1].alnum_chars == 0  # page 2 (0-indexed 1)
        doc = fitz.open(str(path))
        try:
            blank_page = doc[1]
            assert blank_page.get_images(full=True) == []
            assert blank_page.get_drawings() == []
        finally:
            doc.close()


class TestFixture04Routing:
    def test_text_native_and_eu_formatting_visible_and_harmless(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_04_eur_european_number_format"])
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE
        assert pages[0].kind == pdf_utils.PAGE_TEXT
        # European punctuation/currency formatting does not depress the
        # alnum count below threshold or otherwise change classification.
        for s in ("1.234,56", "234,57", "1.469,13"):
            assert s in pages[0].text
        assert pages[0].alnum_chars > THRESHOLD


class TestFixture05Routing:
    def test_text_native_gbp_and_vat_harmless(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_05_gbp_vat_invoice"])
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE
        assert pages[0].kind == pdf_utils.PAGE_TEXT
        assert "£" in pages[0].text and "VAT @ 20%" in pages[0].text


class TestFixture06Routing:
    def test_text_native_negative_discount_harmless(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_06_usd_discount_freight"])
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE
        assert pages[0].kind == pdf_utils.PAGE_TEXT
        assert "-50.00" in pages[0].text


class TestFixture07Routing:
    def test_text_native(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_07_inclusive_tax_invoice"])
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE
        assert pages[0].kind == pdf_utils.PAGE_TEXT


class TestFixture08Routing:
    def test_all_three_pages_text_native_above_threshold(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_08_repeated_table_headers"])
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE
        assert all(p.kind == pdf_utils.PAGE_TEXT for p in pages)
        # repeated column headers do not push any page's count back at or
        # below threshold
        assert all(p.alnum_chars > THRESHOLD for p in pages)


class TestFixture09Routing:
    def test_both_pages_image_only_zero_text(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_09_conflicting_totals"])
        assert len(pages) == 2
        assert all(p.kind == pdf_utils.PAGE_IMAGE for p in pages)
        assert all(p.alnum_chars == 0 for p in pages)
        assert doc_class == pdf_utils.DOC_IMAGE_ONLY


class TestFixture10Routing:
    def test_both_pages_text_native(self, fixture_paths):
        pages, doc_class = _analyze(fixture_paths["fixture_10_two_invoice_numbers"])
        assert len(pages) == 2
        assert all(p.kind == pdf_utils.PAGE_TEXT for p in pages)
        assert doc_class == pdf_utils.DOC_TEXT_NATIVE


# ---------------------------------------------------------------------------
# Chunk-plan tests (Step 5)
# ---------------------------------------------------------------------------
#
# invoice_extractor.pipeline._chunked(items, size) is the application's ONLY
# image-page chunking logic; it does not exist in pdf_utils.py or anywhere
# else. It is a small, pure, side-effect-free function (list slicing) that
# can be imported and called directly WITHOUT running process_file() or
# process_directory() - no PDF is opened, no provider is called, nothing is
# rendered. This satisfies "do not run the full extraction pipeline": we are
# calling one private helper function, not the pipeline's orchestration.
#
# It is named with a leading underscore (pipeline-private) and takes a
# generic `list`, not page numbers specifically - it is not moved or
# refactored here, per instructions, since doing so is not clearly isolated
# from the rest of pipeline.py's vision-routing logic that also calls it.

class TestChunkPlans:
    @pytest.mark.parametrize("fixture_id,expected_by_limit", [
        ("fixture_02_multipage_scanned_exceeds_limit", {
            1: [[1], [2], [3], [4], [5], [6], [7]],
            2: [[1, 2], [3, 4], [5, 6], [7]],
            5: [[1, 2, 3, 4, 5], [6, 7]],
        }),
        ("fixture_03_mixed_text_scan_blank", {
            1: [[3], [4]],
            2: [[3, 4]],
            5: [[3, 4]],
        }),
        ("fixture_09_conflicting_totals", {
            1: [[1], [2]],
            2: [[1, 2]],
            5: [[1, 2]],
        }),
    ])
    def test_chunk_plans_match_expected(self, fixture_id, expected_by_limit):
        core = _core_for(fixture_id)
        image_pages = list(core.expected_image_pages)
        for limit, expected_chunks in expected_by_limit.items():
            actual = _chunked(image_pages, limit)
            assert actual == expected_chunks, f"{fixture_id} @limit={limit}"

    @pytest.mark.parametrize("fixture_id", [
        "fixture_02_multipage_scanned_exceeds_limit",
        "fixture_03_mixed_text_scan_blank",
        "fixture_09_conflicting_totals",
    ])
    @pytest.mark.parametrize("limit", [1, 2, 5])
    def test_every_image_page_appears_exactly_once(self, fixture_id, limit):
        core = _core_for(fixture_id)
        image_pages = list(core.expected_image_pages)
        chunks = _chunked(image_pages, limit)
        flat = [n for chunk in chunks for n in chunk]
        assert sorted(flat) == sorted(image_pages)
        assert len(flat) == len(set(flat))

    @pytest.mark.parametrize("fixture_id", [
        "fixture_02_multipage_scanned_exceeds_limit",
        "fixture_03_mixed_text_scan_blank",
        "fixture_09_conflicting_totals",
    ])
    @pytest.mark.parametrize("limit", [1, 2, 5])
    def test_text_and_blank_pages_never_appear_in_chunks(self, fixture_id, limit):
        core = _core_for(fixture_id)
        image_pages = list(core.expected_image_pages)
        chunks = _chunked(image_pages, limit)
        flat = {n for chunk in chunks for n in chunk}
        forbidden = set(core.expected_text_pages) | set(core.expected_blank_pages)
        assert not (flat & forbidden)

    @pytest.mark.parametrize("fixture_id", [
        "fixture_02_multipage_scanned_exceeds_limit",
        "fixture_03_mixed_text_scan_blank",
        "fixture_09_conflicting_totals",
    ])
    @pytest.mark.parametrize("limit", [1, 2, 5])
    def test_chunk_order_follows_page_order(self, fixture_id, limit):
        core = _core_for(fixture_id)
        image_pages = list(core.expected_image_pages)
        chunks = _chunked(image_pages, limit)
        flat = [n for chunk in chunks for n in chunk]
        assert flat == sorted(flat)

    @pytest.mark.parametrize("fixture_id", [
        "fixture_02_multipage_scanned_exceeds_limit",
        "fixture_03_mixed_text_scan_blank",
        "fixture_09_conflicting_totals",
    ])
    @pytest.mark.parametrize("limit", [1, 2, 5])
    def test_no_chunk_exceeds_the_limit(self, fixture_id, limit):
        core = _core_for(fixture_id)
        image_pages = list(core.expected_image_pages)
        chunks = _chunked(image_pages, limit)
        assert all(len(chunk) <= limit for chunk in chunks)

    def test_zero_limit_raises_clearly(self):
        with pytest.raises(ValueError):
            _chunked([1, 2, 3], 0)

    def test_negative_limit_raises_clearly(self):
        # FIXED in milestone 4 (was previously a silent [] with no error -
        # see tests/test_config.py for the corresponding Config-level fix).
        # _chunked itself now also rejects size<=0 directly, as defense in
        # depth for any caller that invokes it without going through
        # Config's validation.
        with pytest.raises(ValueError):
            _chunked([1, 2, 3], -1)


# ---------------------------------------------------------------------------
# Classifier edge-case regression tests (Step 6)
# ---------------------------------------------------------------------------

def _one_page_pdf(tmp_path: Path, name: str) -> Path:
    return tmp_path / name


class TestClassifierEdgeCases:
    """Locks down ACTUAL current pdf_utils semantics using tiny synthetic
    one-page PDFs built with the milestone-2 builders. Does not change any
    semantics, even where a behavior might seem surprising - see individual
    test docstrings for what each one confirms."""

    def test_exactly_threshold_alnum_chars_is_not_text(self, tmp_path):
        # Confirms strict '>' comparison: exactly THRESHOLD (20) alnum chars
        # does NOT count as text-native; it falls through to the image
        # check, and with no images/drawings but chars > 0, it becomes IMAGE.
        doc = b.new_document()
        b.add_text_page(doc, ["A" * THRESHOLD])
        path = b.save_document(doc, _one_page_pdf(tmp_path, "exactly_threshold.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars == THRESHOLD
        assert pages[0].kind == pdf_utils.PAGE_IMAGE

    def test_one_below_threshold_is_image(self, tmp_path):
        doc = b.new_document()
        b.add_text_page(doc, ["A" * (THRESHOLD - 1)])
        path = b.save_document(doc, _one_page_pdf(tmp_path, "below_threshold.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars == THRESHOLD - 1
        assert pages[0].kind == pdf_utils.PAGE_IMAGE

    def test_one_above_threshold_is_text(self, tmp_path):
        doc = b.new_document()
        b.add_text_page(doc, ["A" * (THRESHOLD + 1)])
        path = b.save_document(doc, _one_page_pdf(tmp_path, "above_threshold.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars == THRESHOLD + 1
        assert pages[0].kind == pdf_utils.PAGE_TEXT

    def test_punctuation_only_is_blank(self, tmp_path):
        doc = b.new_document()
        b.add_text_page(doc, ["!!! *** --- ... ,,, ;;; :::"])
        path = b.save_document(doc, _one_page_pdf(tmp_path, "punctuation_only.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars == 0
        assert pages[0].kind == pdf_utils.PAGE_BLANK

    def test_whitespace_only_is_blank(self, tmp_path):
        doc = b.new_document()
        b.add_text_page(doc, ["     ", "\t\t\t", "   "])
        path = b.save_document(doc, _one_page_pdf(tmp_path, "whitespace_only.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars == 0
        assert pages[0].kind == pdf_utils.PAGE_BLANK

    def test_drawing_only_is_image(self, tmp_path):
        doc = b.new_document()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(50, 50, 200, 150))
        path = b.save_document(doc, _one_page_pdf(tmp_path, "drawing_only.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars == 0
        assert pages[0].kind == pdf_utils.PAGE_IMAGE

    def test_image_plus_short_caption_below_threshold_is_image(self, tmp_path):
        doc = b.new_document()
        png = b.render_lines_to_png(["Scanned content"])
        page = b.add_image_page(doc, png)
        page.insert_text((50, 700), "short caption")  # 12 alnum chars, below threshold
        path = b.save_document(doc, _one_page_pdf(tmp_path, "image_short_caption.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert 0 < pages[0].alnum_chars <= THRESHOLD
        assert pages[0].kind == pdf_utils.PAGE_IMAGE

    def test_image_plus_enough_text_above_threshold_is_text(self, tmp_path):
        # Confirms the threshold check runs BEFORE the image/drawing check:
        # a page with both a substantial text layer (>threshold) AND an
        # embedded image is classified TEXT, not image - text takes
        # priority whenever it alone clears the bar, regardless of what
        # else is on the page. This is existing, intentional behavior
        # (keeps the cheap text route in play whenever it's trustworthy),
        # not something introduced or changed by this milestone.
        doc = b.new_document()
        png = b.render_lines_to_png(["Scanned content"])
        page = b.add_image_page(doc, png)
        page.insert_text((50, 700), "This caption has well more than twenty alnum chars")
        path = b.save_document(doc, _one_page_pdf(tmp_path, "image_long_caption.pdf"))
        pages = pdf_utils.analyze_pages(str(path), THRESHOLD)
        assert pages[0].alnum_chars > THRESHOLD
        assert pages[0].kind == pdf_utils.PAGE_TEXT

    def test_corrupt_pdf_raises_file_data_error(self, tmp_path):
        path = _one_page_pdf(tmp_path, "corrupt.pdf")
        path.write_bytes(b"this is not a pdf file, just garbage bytes")
        with pytest.raises(fitz.FileDataError):
            pdf_utils.analyze_pages(str(path), THRESHOLD)

    def test_password_protected_pdf_raises_on_page_access(self, tmp_path):
        # analyze_pages does not pre-check doc.needs_pass; fitz.open()
        # succeeds even for an encrypted file, so the failure surfaces at
        # the first page.get_text() call instead, as a ValueError - not a
        # FileDataError. Documented here as current, exact behavior.
        doc = b.new_document()
        b.add_text_page(doc, ["Secret invoice content, over twenty alnum characters"])
        path = _one_page_pdf(tmp_path, "encrypted.pdf")
        doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256,
                 user_pw="secret123", owner_pw="owner123")
        doc.close()

        reopened = fitz.open(str(path))
        assert reopened.needs_pass == 1
        assert reopened.is_encrypted is True
        reopened.close()

        with pytest.raises(ValueError):
            pdf_utils.analyze_pages(str(path), THRESHOLD)
