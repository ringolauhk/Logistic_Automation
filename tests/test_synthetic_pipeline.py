"""Deterministic offline full-pipeline tests: HAPPY PATH (Milestone 4 Part D).

Runs invoice_extractor.pipeline.process_file FOR REAL against all ten
synthetic fixtures. ONLY the provider boundary is mocked
(gemini_client._generate / claude_client._request, via
provider_responses.install_provider_seams). PDF analysis, page
classification, rendering, chunking, aggregation, schema normalization, and
validation all execute unmocked. Rendering is spied (not replaced) via
provider_responses.install_render_spy so exact page numbers per vision
request can be asserted without disabling real rendering.

No network calls, no real invoices, no live benchmark, no .env required.
"""

import json
from decimal import Decimal

from invoice_extractor.aggregation import RouteResult, aggregate
from invoice_extractor.pipeline import process_file
from invoice_extractor.schema import normalize_invoice

from .conftest import make_config
from .synthetic_fixtures import ground_truth as gt
from .synthetic_fixtures import provider_responses as pr


def assert_header_matches(invoice, scenario) -> None:
    """Shared header/Decimal comparison against an ExpectedInvoiceScenario -
    uses the REAL Invoice object's fields, never reimplements coercion."""
    assert invoice.invoice_number == scenario.expected_invoice_number
    assert invoice.invoice_date == scenario.expected_invoice_date
    assert invoice.currency == scenario.expected_currency
    assert invoice.seller_name == scenario.expected_seller_name
    assert invoice.buyer_name == scenario.expected_buyer_name
    if scenario.expected_subtotal is not None:
        assert invoice.subtotal == Decimal(scenario.expected_subtotal)
    else:
        assert invoice.subtotal is None
    if scenario.expected_tax_amount is not None:
        assert invoice.tax_amount == Decimal(scenario.expected_tax_amount)
    assert invoice.total_amount == Decimal(scenario.expected_total_amount)
    assert invoice.payment_terms == scenario.expected_payment_terms
    for actual, expected in zip(invoice.line_items, scenario.expected_line_items):
        assert actual.description == expected.description
        assert actual.quantity == Decimal(expected.quantity)
        assert actual.unit_price == Decimal(expected.unit_price)
        assert actual.amount == Decimal(expected.amount)


# ---------------------------------------------------------------------------
# Fixture 1 - multi-page text-native
# ---------------------------------------------------------------------------

class TestFixture01HappyPath:
    def test_one_text_call_six_ordered_items_no_review(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.invoice_response_json(gt.FIXTURE_01)],
        )

        result = process_file(path, cfg, logger)

        assert result.source_file == path.name
        assert result.page_count == 3
        assert result.document_classification == "text-native"
        assert result.extraction_method == "text"
        assert result.provider == "gemini"
        assert result.model == cfg.gemini_text_model
        assert result.text_pages == [1, 2, 3]
        assert result.image_pages == []
        assert result.blank_pages == []
        assert result.failed_pages == []
        assert result.vision_chunk_count == 0
        assert_header_matches(result.invoice, gt.FIXTURE_01)
        assert len(result.invoice.line_items) == 6
        assert result.needs_review is False
        assert result.review_reason is None
        # call accounting
        assert recorder.gemini_text_count == 1
        assert recorder.gemini_vision_count == 0
        assert recorder.claude_text_count == 0
        assert recorder.claude_vision_count == 0


# ---------------------------------------------------------------------------
# Fixture 2 - scanned, exceeds MAX_VISION_PAGES
# ---------------------------------------------------------------------------

class TestFixture02HappyPath:
    def test_four_vision_calls_seven_pages_covered_once_no_review(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_vision_pages=2)
        path = synthetic_fixture_paths["fixture_02_multipage_scanned_exceeds_limit"]
        scenario = gt.FIXTURE_02
        responses = [
            pr.invoice_response_json_subset(scenario, slice(0, 2)),
            pr.invoice_response_json_subset(scenario, slice(2, 4)),
            pr.invoice_response_json_subset(scenario, slice(4, 6)),
            pr.invoice_response_json_subset(scenario, slice(6, 7)),
        ]
        recorder = pr.install_provider_seams(monkeypatch, cfg, gemini_vision=responses)
        rendered = pr.install_render_spy(monkeypatch)

        result = process_file(path, cfg, logger)

        assert result.document_classification == "image-only"
        assert result.extraction_method == "vision"
        assert result.provider == "gemini"
        assert result.model == cfg.gemini_vision_model
        assert result.image_pages == [1, 2, 3, 4, 5, 6, 7]
        assert result.failed_pages == []
        assert result.vision_chunk_count == 4
        assert len(result.invoice.line_items) == 7
        assert [li.description for li in result.invoice.line_items] == [
            li.description for li in scenario.expected_line_items
        ]
        assert_header_matches(result.invoice, scenario)
        assert result.needs_review is False
        # call accounting + exact page coverage
        assert recorder.gemini_vision_count == 4
        assert recorder.gemini_text_count == 0
        assert rendered == [[1, 2], [3, 4], [5, 6], [7]]
        flat = [n for chunk in rendered for n in chunk]
        assert sorted(flat) == list(range(1, 8))
        assert len(flat) == len(set(flat))  # no duplicate page


# ---------------------------------------------------------------------------
# Fixture 3 - mixed text/blank/image
# ---------------------------------------------------------------------------

class TestFixture03HappyPath:
    def test_one_text_one_vision_call_blank_excluded_mixed_no_review(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_vision_pages=2)
        path = synthetic_fixture_paths["fixture_03_mixed_text_scan_blank"]
        scenario = gt.FIXTURE_03
        recorder = pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_text=[pr.invoice_response_json_subset(scenario, slice(0, 1))],
            gemini_vision=[pr.invoice_response_json_subset(scenario, slice(1, 3))],
        )
        rendered = pr.install_render_spy(monkeypatch)

        result = process_file(path, cfg, logger)

        assert result.document_classification == "mixed"
        assert result.extraction_method == "mixed"
        assert result.provider == "gemini"
        assert result.text_pages == [1, 5]
        assert result.image_pages == [3, 4]
        assert result.blank_pages == [2]  # excluded from both routes but recorded
        assert result.failed_pages == []
        assert result.vision_chunk_count == 1
        # cross-route merge preserves page order: text route (page 1) first,
        # then vision route (pages 3-4)
        assert [li.description for li in result.invoice.line_items] == [
            li.description for li in scenario.expected_line_items
        ]
        assert_header_matches(result.invoice, scenario)
        assert result.needs_review is False
        assert recorder.gemini_text_count == 1
        assert recorder.gemini_vision_count == 1
        assert rendered == [[3, 4]]  # only the image pages, never page 2 or 5


# ---------------------------------------------------------------------------
# Fixture 4 - EUR European number formatting
# ---------------------------------------------------------------------------

class TestFixture04HappyPath:
    def test_eu_formatted_response_normalizes_to_correct_decimals(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_04_eur_european_number_format"]
        scenario = gt.FIXTURE_04
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.fixture_04_response_json()],
        )

        result = process_file(path, cfg, logger)

        assert result.extraction_method == "text"
        assert result.provider == "gemini"
        # The mocked response contained "1.234,56" / "234,57" / "1.469,13" -
        # normalization must still land on the correct canonical Decimals.
        assert result.invoice.subtotal == Decimal("1234.56")
        assert result.invoice.tax_amount == Decimal("234.57")
        assert result.invoice.total_amount == Decimal("1469.13")
        assert result.invoice.line_items[0].unit_price == Decimal("1000.00")
        assert_header_matches(result.invoice, scenario)
        assert result.needs_review is False
        assert recorder.gemini_text_count == 1


# ---------------------------------------------------------------------------
# Fixture 5 - GBP VAT invoice
# ---------------------------------------------------------------------------

class TestFixture05HappyPath:
    def test_gbp_symbol_response_normalizes_currency_to_gbp(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_05_gbp_vat_invoice"]
        scenario = gt.FIXTURE_05
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.fixture_05_response_json()],
        )

        result = process_file(path, cfg, logger)

        assert result.extraction_method == "text"
        # Mocked response returned "£" (raw symbol); normalize_currency must
        # map it to the ISO code.
        assert result.invoice.currency == "GBP"
        assert_header_matches(result.invoice, scenario)
        assert result.needs_review is False
        assert recorder.gemini_text_count == 1


# ---------------------------------------------------------------------------
# Fixture 6 - discount/freight -> inconclusive, needs_review
# ---------------------------------------------------------------------------

class TestFixture06HappyPath:
    def test_needs_review_totals_inconclusive_no_invented_charges(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_06_usd_discount_freight"]
        scenario = gt.FIXTURE_06
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.invoice_response_json(scenario)],
        )

        result = process_file(path, cfg, logger)

        assert result.extraction_method == "text"
        assert_header_matches(result.invoice, scenario)
        # discount/freight are not modeled fields and must not appear as
        # invented line items - exactly the scenario's 2 genuine items.
        assert len(result.invoice.line_items) == 2
        assert result.needs_review is True
        assert "totals inconclusive" in result.review_reason
        assert "discount/shipping/duties/rounding" in result.review_reason
        assert recorder.gemini_text_count == 1


# ---------------------------------------------------------------------------
# Fixture 7 - inclusive tax
# ---------------------------------------------------------------------------

class TestFixture07HappyPath:
    def test_subtotal_null_inclusive_rule_passes_no_review(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_07_inclusive_tax_invoice"]
        scenario = gt.FIXTURE_07
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.invoice_response_json(scenario)],
        )

        result = process_file(path, cfg, logger)

        assert result.invoice.subtotal is None
        line_sum = sum((li.amount for li in result.invoice.line_items), Decimal("0"))
        assert line_sum == result.invoice.total_amount  # rule 2: inclusive/zero tax
        assert result.needs_review is False
        assert recorder.gemini_text_count == 1


# ---------------------------------------------------------------------------
# Fixture 8 - repeated headers: clean vs header-hallucination variants
# ---------------------------------------------------------------------------

class TestFixture08HappyPath:
    def test_variant_a_clean_output_six_items_no_review(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_08_repeated_table_headers"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.fixture_08_clean_response_json()],
        )

        result = process_file(path, cfg, logger)

        assert len(result.invoice.line_items) == 6
        assert result.needs_review is False
        assert recorder.gemini_text_count == 1

    def test_variant_b_header_hallucination_known_gap_not_endorsed(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        # DOCUMENTS A KNOWN APPLICATION GAP - does not endorse it as correct.
        # See ground_truth.py's FIXTURE_08.corrupted.known_gap: a header-
        # shaped row with only `description` set survives
        # normalize_invoice's any()-based filter. Because its amount is
        # null, it's excluded from validate_invoice's sum, so there is no
        # arithmetic impact and no review trigger - 3 spurious rows pass
        # through completely undetected. This test locks down that CURRENT
        # behavior; it is not a statement that the behavior is desirable.
        path = synthetic_fixture_paths["fixture_08_repeated_table_headers"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.fixture_08_hallucination_response_json()],
        )

        result = process_file(path, cfg, logger)

        assert len(result.invoice.line_items) == 9  # 6 real + 3 spurious header rows
        spurious = [li for li in result.invoice.line_items if li.description == "Description"]
        assert len(spurious) == 3
        assert all(li.amount is None for li in spurious)
        assert result.needs_review is False  # <- the gap, locked down not fixed
        assert recorder.gemini_text_count == 1


# ---------------------------------------------------------------------------
# Fixture 9 - conflicting totals: chunk-setting-dependent behavior
# ---------------------------------------------------------------------------

class TestFixture09HappyPath:
    def test_setting_a_max_1_conflict_detected_total_650_retained(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_vision_pages=1)
        path = synthetic_fixture_paths["fixture_09_conflicting_totals"]
        responses = [
            pr.fixture_09_chunk_response_json([0, 1], "500.00"),
            pr.fixture_09_chunk_response_json([2], "650.00"),
        ]
        recorder = pr.install_provider_seams(monkeypatch, cfg, gemini_vision=responses)
        rendered = pr.install_render_spy(monkeypatch)

        result = process_file(path, cfg, logger)

        assert recorder.gemini_vision_count == 2
        assert rendered == [[1], [2]]
        assert result.invoice.total_amount == Decimal("650.00")  # last-page-wins
        assert result.needs_review is True
        assert "conflict in total_amount" in result.review_reason
        assert "500.00" in result.review_reason and "650.00" in result.review_reason

    def test_setting_b_max_2_single_chunk_no_cross_chunk_conflict_possible(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        # Only ONE vision call happens at this setting - aggregation never
        # sees two RouteResults, so its conflict-detection code cannot run
        # at all. This test asserts only what IS deterministic: one call,
        # one chunk, and whatever total the (single) scripted response
        # contains - it does NOT claim a conflict is (or isn't) detected,
        # since that would require two chunks to exist in the first place.
        cfg = make_config(max_vision_pages=2)
        path = synthetic_fixture_paths["fixture_09_conflicting_totals"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_vision=[pr.fixture_09_chunk_response_json([0, 1, 2], "650.00")],
        )
        rendered = pr.install_render_spy(monkeypatch)

        result = process_file(path, cfg, logger)

        assert recorder.gemini_vision_count == 1
        assert rendered == [[1, 2]]
        assert result.invoice.total_amount == Decimal("650.00")
        assert "conflict" not in (result.review_reason or "")


# ---------------------------------------------------------------------------
# Fixture 10 - likely two-invoice PDF: offline aggregation vs realistic pipeline
# ---------------------------------------------------------------------------

class TestFixture10OfflineAggregation:
    """10A: a lower-level aggregation-focused test. Constructs two
    RouteResults directly (via real normalize_invoice(), not hand-built
    Invoice objects) and calls aggregate() directly - NOT process_file().
    This is the documented simplification from ground_truth.py's
    FIXTURE_10.offline_expected: it proves aggregation's conflict-detection
    logic works IF two RouteResults ever arise, which is NOT what the
    realistic single-text-call pipeline path actually produces (see
    TestFixture10RealisticPipeline below)."""

    def test_two_route_results_conflicting_invoice_numbers_flagged(self):
        offline = gt.FIXTURE_10.offline_expected
        invoice_a = normalize_invoice(json.loads(pr.fixture_10_invoice_a_response_json()))
        invoice_b = normalize_invoice(json.loads(pr.fixture_10_invoice_b_response_json()))
        route_a = RouteResult("text", [1], invoice_a, "gemini", "test-model")
        route_b = RouteResult("text", [2], invoice_b, "gemini", "test-model")

        outcome = aggregate([route_a, route_b])

        assert outcome.invoice.invoice_number == offline.route_a_invoice_number  # first route wins (non-monetary)
        conflict_fields = {f for f, _ in outcome.conflicts}
        assert "invoice_number" in conflict_fields
        assert any("possible multiple invoices in one PDF" in n for n in outcome.notes)


class TestFixture10RealisticPipeline:
    """10B: the REALISTIC full-pipeline path. Both text pages are
    concatenated into ONE Gemini text call (process_file's actual routing
    behavior for a 2-page all-text document) - there is only ever ONE
    RouteResult, so aggregation's conflict detection is UNREACHABLE here.
    These tests do NOT claim the pipeline guarantees multi-invoice
    detection; they document which scripted outcomes lead to a review flag
    (for an unrelated reason - arithmetic, not "two invoices") and which
    expose the blind spot (a clean-looking single-invoice result)."""

    def test_outcome_first_invoice_only_exposes_blind_spot(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_10_two_invoice_numbers"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.fixture_10_invoice_a_response_json()],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_text_count == 1  # confirms ONE call saw both pages
        assert result.invoice.invoice_number == gt.FIXTURE_10.offline_expected.route_a_invoice_number
        # BLIND SPOT: a clean, internally-consistent single-invoice result -
        # nothing here signals that page 2 (invoice B) was silently dropped.
        assert result.needs_review is False

    def test_outcome_hybrid_record_triggers_review_for_arithmetic_not_detection(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_10_two_invoice_numbers"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.fixture_10_hybrid_response_json()],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_text_count == 1
        assert result.needs_review is True
        # Reviewed for a totals mismatch, NOT because "two invoices" was ever
        # detected - the review_reason must not claim multi-invoice detection.
        assert "totals inconclusive" in result.review_reason
        assert "possible multiple invoices" not in result.review_reason

    def test_outcome_malformed_response_fails_reviewably(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_10_two_invoice_numbers"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.malformed_json_text()],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_text_count == 1
        assert result.needs_review is True
        assert result.error is True  # hard failure: no structured result at all
