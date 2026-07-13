"""Tests for the synthetic invoice PDF builders and scenario registry.

MILESTONE 2 SCOPE: validates GENERATED PDF STRUCTURE against the ground
truth from milestone 1. Does NOT call invoice_extractor (no pipeline, no
provider clients, no LLM-normalization functions), makes no network calls,
requires no .env, and writes files only under pytest's tmp_path (auto-
cleaned). PDF byte/SHA identity is deliberately NOT asserted - PyMuPDF's own
container metadata makes byte-level output unstable across runs even for
identical semantic content (verified empirically while building this
suite); only semantic/structural determinism is checked.
"""

import copy
from pathlib import Path

import fitz
import pytest

from .synthetic_fixtures import ground_truth as gt
from .synthetic_fixtures import scenarios as sc

ALL_FIXTURE_IDS = [core.fixture_id for core in gt.ALL_CORES]


def _page_kind(page: fitz.Page) -> str:
    """Structural page-kind inspection, independent of invoice_extractor -
    mirrors the pack's own vocabulary, not a call into pdf_utils.py."""
    text_len = len(page.get_text("text").strip())
    if text_len > 20:
        return "text"
    if page.get_images(full=True) or page.get_drawings():
        return "image"
    if text_len > 0:
        return "image"
    return "blank"


def _core_for(fixture_id: str) -> gt.ExpectedScenarioCore:
    return next(c for c in gt.ALL_CORES if c.fixture_id == fixture_id)


# ---------------------------------------------------------------------------
# All ten PDFs generate; filenames/page counts/page kinds match ground truth
# ---------------------------------------------------------------------------

class TestAllFixturesGenerate:
    def test_all_ten_can_be_generated(self, tmp_path):
        paths = sc.build_all_scenarios(tmp_path)
        assert len(paths) == 10
        for p in paths:
            assert p.exists()

    def test_filenames_match_ground_truth(self, tmp_path):
        paths = sc.build_all_scenarios(tmp_path)
        generated_names = {p.name for p in paths}
        expected_names = {core.filename for core in gt.ALL_CORES}
        assert generated_names == expected_names

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_page_count_matches_ground_truth(self, tmp_path, fixture_id):
        core = _core_for(fixture_id)
        path = sc.build_scenario(fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            assert doc.page_count == len(core.page_layout), fixture_id
        finally:
            doc.close()

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_page_kinds_match_ground_truth(self, tmp_path, fixture_id):
        core = _core_for(fixture_id)
        path = sc.build_scenario(fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            actual_kinds = [_page_kind(doc[i]) for i in range(doc.page_count)]
        finally:
            doc.close()
        expected_kinds = [p.page_kind for p in core.page_layout]
        assert actual_kinds == expected_kinds, fixture_id


# ---------------------------------------------------------------------------
# Fixture 2: seven image-only pages, no extractable text behind them
# ---------------------------------------------------------------------------

class TestFixture02:
    def test_seven_image_only_pages(self, tmp_path):
        core = gt.FIXTURE_02.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            assert doc.page_count == 7
            for i in range(7):
                page = doc[i]
                assert len(page.get_text("text").strip()) == 0, f"page {i+1} has extractable text"
                assert len(page.get_images(full=True)) >= 1, f"page {i+1} has no embedded image"
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# Fixture 3: exact page sequence text, blank, image, image, text
# ---------------------------------------------------------------------------

class TestFixture03:
    def test_exact_page_sequence(self, tmp_path):
        core = gt.FIXTURE_03.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            kinds = [_page_kind(doc[i]) for i in range(doc.page_count)]
        finally:
            doc.close()
        assert kinds == ["text", "blank", "image", "image", "text"]

    def test_blank_page_is_genuinely_blank(self, tmp_path):
        core = gt.FIXTURE_03.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            blank_page = doc[1]  # page 2, 0-indexed
            assert blank_page.get_text("text").strip() == ""
            assert blank_page.get_images(full=True) == []
            assert blank_page.get_drawings() == []
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# Fixture 4: exact European-formatted visible strings
# ---------------------------------------------------------------------------

class TestFixture04:
    def test_exact_european_formatted_strings_present(self, tmp_path):
        core = gt.FIXTURE_04.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text = doc[0].get_text("text")
        finally:
            doc.close()
        for expected in ("1.234,56", "234,57", "1.469,13"):
            assert expected in text, f"missing {expected!r} in extracted text"

    def test_no_normalized_decimal_point_values_replace_them(self, tmp_path):
        # The canonical (non-EU) forms must NOT appear as if they had
        # replaced the EU-formatted ones.
        core = gt.FIXTURE_04.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text = doc[0].get_text("text")
        finally:
            doc.close()
        assert "1234.56" not in text
        assert "1469.13" not in text


# ---------------------------------------------------------------------------
# Fixture 5: visible GBP/VAT terms
# ---------------------------------------------------------------------------

class TestFixture05:
    def test_gbp_and_vat_terms_visible(self, tmp_path):
        core = gt.FIXTURE_05.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text = doc[0].get_text("text")
        finally:
            doc.close()
        assert "£" in text
        assert "VAT @ 20%" in text


# ---------------------------------------------------------------------------
# Fixture 6: discount/freight visible and separate from line items
# ---------------------------------------------------------------------------

class TestFixture06:
    def test_discount_and_freight_visible(self, tmp_path):
        core = gt.FIXTURE_06.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text = doc[0].get_text("text")
        finally:
            doc.close()
        assert "Discount" in text and "-50.00" in text
        assert "Freight" in text and "75.00" in text

    def test_discount_freight_lines_separate_from_item_table(self, tmp_path):
        core = gt.FIXTURE_06.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text = doc[0].get_text("text")
        finally:
            doc.close()
        lines = [ln for ln in text.split("\n") if ln.strip()]
        item_line_indices = [i for i, ln in enumerate(lines)
                             if "Freight brokerage" in ln or "Handling and dispatch" in ln]
        charge_line_indices = [i for i, ln in enumerate(lines)
                               if ln.startswith("Discount:") or ln.startswith("Freight:")]
        assert item_line_indices and charge_line_indices
        # No charge line is interleaved among the item lines
        assert max(item_line_indices) < min(charge_line_indices)


# ---------------------------------------------------------------------------
# Fixture 7: tax-inclusive wording, no pre-tax subtotal stated
# ---------------------------------------------------------------------------

class TestFixture07:
    def test_inclusive_wording_present_no_subtotal(self, tmp_path):
        core = gt.FIXTURE_07.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text = doc[0].get_text("text")
        finally:
            doc.close()
        assert "include tax" in text.lower() or "tax incl" in text.lower()
        assert "Subtotal" not in text


# ---------------------------------------------------------------------------
# Fixture 8: header repeated exactly 3 times; exactly 6 genuine items
# ---------------------------------------------------------------------------

class TestFixture08:
    def test_header_repeated_exactly_three_times(self, tmp_path):
        core = gt.FIXTURE_08.clean.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            occurrences = sum(
                1 for i in range(doc.page_count)
                if "Description" in doc[i].get_text("text") and "Qty" in doc[i].get_text("text")
            )
        finally:
            doc.close()
        assert occurrences == 3

    def test_exactly_six_genuine_item_descriptions(self, tmp_path):
        core = gt.FIXTURE_08.clean.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            full_text = "".join(doc[i].get_text("text") for i in range(doc.page_count))
        finally:
            doc.close()
        expected_items = [li.description for li in gt.FIXTURE_08.clean.expected_line_items]
        assert len(expected_items) == 6
        for description in expected_items:
            assert description in full_text
        # exactly one occurrence each (no duplication introduced by the builder)
        for description in expected_items:
            assert full_text.count(description) == 1


# ---------------------------------------------------------------------------
# Fixture 9: both totals exposed on the intended (pre-rasterization) pages
# ---------------------------------------------------------------------------

class TestFixture09:
    def test_both_totals_present_on_intended_pages(self):
        page1_lines, page2_lines = sc.fixture_09_page_lines()
        assert any("500.00" in ln for ln in page1_lines)
        assert any("650.00" in ln for ln in page2_lines)
        assert not any("650.00" in ln for ln in page1_lines)
        assert not any("500.00" in ln for ln in page2_lines)

    def test_pdf_pages_are_image_only(self, tmp_path):
        core = gt.FIXTURE_09.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            for i in range(doc.page_count):
                assert len(doc[i].get_text("text").strip()) == 0
                assert len(doc[i].get_images(full=True)) >= 1
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# Fixture 10: INV-A100 only on page 1, INV-B200 only on page 2
# ---------------------------------------------------------------------------

class TestFixture10:
    def test_invoice_numbers_isolated_per_page(self, tmp_path):
        core = gt.FIXTURE_10.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text1 = doc[0].get_text("text")
            text2 = doc[1].get_text("text")
        finally:
            doc.close()
        assert "INV-A100" in text1 and "INV-B200" not in text1
        assert "INV-B200" in text2 and "INV-A100" not in text2

    def test_invoices_are_visually_distinct(self, tmp_path):
        core = gt.FIXTURE_10.core
        path = sc.build_scenario(core.fixture_id, tmp_path / core.filename)
        doc = fitz.open(str(path))
        try:
            text1 = doc[0].get_text("text")
            text2 = doc[1].get_text("text")
        finally:
            doc.close()
        assert "Vanguard Shipping Co" in text1
        assert "Different Seller Corp" in text2
        assert text1 != text2


# ---------------------------------------------------------------------------
# Determinism (semantic, not byte/SHA), registry integrity, error handling
# ---------------------------------------------------------------------------

class TestSemanticDeterminism:
    """Repeated generation produces equivalent semantic content and page
    structure. Byte-level/SHA identity is NOT asserted - verified during
    development that PyMuPDF's own container metadata makes two builds of
    identical content differ by a byte or two even with no random content
    written by this package."""

    @pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
    def test_repeated_generation_same_page_kinds_and_text(self, tmp_path, fixture_id):
        core = _core_for(fixture_id)
        path_a = sc.build_scenario(fixture_id, tmp_path / f"a_{core.filename}")
        path_b = sc.build_scenario(fixture_id, tmp_path / f"b_{core.filename}")

        doc_a, doc_b = fitz.open(str(path_a)), fitz.open(str(path_b))
        try:
            assert doc_a.page_count == doc_b.page_count
            for i in range(doc_a.page_count):
                assert _page_kind(doc_a[i]) == _page_kind(doc_b[i])
                assert doc_a[i].get_text("text") == doc_b[i].get_text("text")
        finally:
            doc_a.close()
            doc_b.close()


class TestBuildersDoNotMutateGroundTruth:
    def test_ground_truth_unchanged_after_building_all(self, tmp_path):
        before = copy.deepcopy(gt.ALL_CORES)
        sc.build_all_scenarios(tmp_path)
        after = gt.ALL_CORES
        assert before == after


class TestRegistryBehavior:
    def test_unknown_fixture_id_raises_clearly(self):
        with pytest.raises(KeyError, match="unknown fixture_id"):
            sc.get_scenario("does_not_exist")

    def test_list_scenarios_returns_all_ten(self):
        assert len(sc.list_scenarios()) == 10

    def test_building_one_fixture_does_not_build_others(self, tmp_path):
        sc.build_scenario("fixture_04_eur_european_number_format",
                          tmp_path / "eur_european_number_format.pdf")
        # No other fixture's file should have appeared as a side effect.
        other_names = {c.filename for c in gt.ALL_CORES
                       if c.fixture_id != "fixture_04_eur_european_number_format"}
        for name in other_names:
            assert not (tmp_path / name).exists()

    def test_importing_scenarios_module_writes_no_files(self, tmp_path, monkeypatch):
        # Import already happened at collection time with no tmp_path
        # involvement; re-importing (forcing re-exec) must still not write
        # anything into an empty directory we control.
        import importlib

        before = set(tmp_path.iterdir())
        importlib.reload(sc)
        after = set(tmp_path.iterdir())
        assert before == after


class TestGeneratorScriptBehavior:
    """Exercises the manual generator script's module-level functions
    directly (no subprocess needed) - argument parsing and file-writing
    logic in scripts/generate_synthetic_fixtures.py."""

    @pytest.fixture(autouse=True)
    def _import_script(self):
        import importlib
        import sys as _sys

        project_root = Path(__file__).resolve().parent.parent
        if str(project_root) not in _sys.path:
            _sys.path.insert(0, str(project_root))
        self.script = importlib.import_module("scripts.generate_synthetic_fixtures")

    def test_generate_one_fixture(self, tmp_path):
        rc = self.script.main([
            "--output", str(tmp_path),
            "--fixture", "fixture_05_gbp_vat_invoice",
        ])
        assert rc == 0
        assert (tmp_path / "gbp_vat_invoice.pdf").exists()
        assert not (tmp_path / "eur_european_number_format.pdf").exists()

    def test_generate_all_fixtures(self, tmp_path):
        rc = self.script.main(["--output", str(tmp_path)])
        assert rc == 0
        for core in gt.ALL_CORES:
            assert (tmp_path / core.filename).exists()

    def test_refuses_overwrite_without_force(self, tmp_path, capsys):
        self.script.main(["--output", str(tmp_path), "--fixture", "fixture_05_gbp_vat_invoice"])
        rc = self.script.main(["--output", str(tmp_path), "--fixture", "fixture_05_gbp_vat_invoice"])
        assert rc != 0
        assert "already exists" in capsys.readouterr().err

    def test_force_allows_overwrite(self, tmp_path):
        self.script.main(["--output", str(tmp_path), "--fixture", "fixture_05_gbp_vat_invoice"])
        rc = self.script.main([
            "--output", str(tmp_path), "--fixture", "fixture_05_gbp_vat_invoice", "--force",
        ])
        assert rc == 0

    def test_unknown_fixture_exits_nonzero(self, tmp_path, capsys):
        rc = self.script.main(["--output", str(tmp_path), "--fixture", "bogus"])
        assert rc != 0
        assert "unknown fixture_id" in capsys.readouterr().err
