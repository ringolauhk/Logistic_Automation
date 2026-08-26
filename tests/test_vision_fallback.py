"""M9.2: bounded automatic text -> vision fallback.

A TEXT-native page can hide its PRODUCT TABLE in an image (a scanned table
pasted into an otherwise text-native page), so every text model answers
correctly and still returns no usable product row. When - and only when -
the whole text ladder was rejected for that single reason, the pipeline
re-reads those pages ONCE through the existing vision chunk path.

M11 note: absent document metadata (seller, date, currency, total) no
longer rejects an attempt at all, so it can never trigger this fallback -
the pipeline does not buy a second model call to chase headers.

Everything here is offline: the only seam is
openrouter_client._chat_completion (same seam the other OpenRouter suites
use), and every test asserts CALL COUNTS so the one-attempt bound and the
exclusions are provable, not assumed.
"""

from pathlib import Path

import pytest

from invoice_extractor import openrouter_client
from invoice_extractor.pipeline import (
    _missing_required_from,
    _validation_only_exhaustion,
    process_file,
    safe_review_categories,
)
from invoice_extractor.provider import ProviderError
from invoice_extractor.usage import LadderExhaustedError, UsageRecord

from .conftest import TEXT_BODY, invoice_json, make_config
from .test_openrouter_pipeline import Recorder, envelope

ROUTE_TEXT = "text"
ROUTE_VISION = "vision"


def fallback_cfg(**overrides):
    """OpenRouter gateway with BOTH ladders configured (one model each, so
    call counts are unambiguous)."""
    base = dict(
        llm_gateway="openrouter",
        openrouter_api_key="test-or-key",
        openrouter_text_models=("test-vendor/text-1",),
        openrouter_vision_models=("test-vendor/vision-1",),
        max_retries=1,
    )
    base.update(overrides)
    return make_config(**base)


def text_calls(rec):
    """Requests whose messages carry no image part = the text route."""
    return [c for c in rec.calls if not _has_image(c)]


def vision_calls(rec):
    return [c for c in rec.calls if _has_image(c)]


def _has_image(call) -> bool:
    for message in call["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            if any(isinstance(part, dict) and part.get("type") == "image_url"
                   for part in content):
                return True
    return False


@pytest.fixture
def text_pdf(pdf_factory):
    return Path(pdf_factory([("text", TEXT_BODY)], name="text-native.pdf"))


@pytest.fixture
def two_page_text_pdf(pdf_factory):
    return Path(pdf_factory([("text", TEXT_BODY), ("text", TEXT_BODY)],
                            name="two-page.pdf"))


@pytest.fixture
def scan_pdf(pdf_factory):
    return Path(pdf_factory([("image",)], name="scan.pdf"))


def _record(category, *, accepted=False, route=ROUTE_TEXT):
    return UsageRecord(
        run_id="r", source_file="f.pdf", route=route, page_range="1",
        attempt_type="primary", ladder_index=0, requested_model="m",
        actual_model="m", structured_mode="json_object", input_tokens=1,
        output_tokens=1, reasoning_tokens=0, total_tokens=2, cost_usd=None,
        finish_reason="stop", native_finish_reason="stop",
        generation_id="g", latency_ms=1.0, accepted=accepted,
        rejection_category=category, http_status=None)


# --- the trigger predicate (structured, never string matching) --------------

class TestTriggerPredicate:
    def test_validation_only_exhaustion_true_for_missing_fields(self):
        exc = LadderExhaustedError(
            "m1: ExtractionError: missing required fields: seller_name",
            [_record("missing_required_fields"),
             _record("missing_required_fields")])
        assert _validation_only_exhaustion(exc) is True

    @pytest.mark.parametrize("category", [
        "transport", "malformed_envelope", "malformed_json", "truncated",
        "empty", "rate_limited",
    ])
    def test_any_non_validation_rejection_disqualifies(self, category):
        exc = LadderExhaustedError("mixed", [
            _record("missing_required_fields"), _record(category)])
        assert _validation_only_exhaustion(exc) is False

    def test_no_records_or_plain_exception_disqualifies(self):
        assert _validation_only_exhaustion(LadderExhaustedError("x", [])) \
            is False
        assert _validation_only_exhaustion(ValueError("boom")) is False

    def test_missing_fields_extracted_from_known_names_only(self):
        exc = LadderExhaustedError(
            "ExtractionError: missing required fields: seller_name, currency "
            "-- confidential vendor text that must not leak", [])
        assert _missing_required_from(exc) == ["currency", "seller_name"]


# --- the bounded fallback ----------------------------------------------------

class TestBoundedVisionFallback:
    def test_text_success_never_invokes_vision(self, logger, text_pdf,
                                               monkeypatch):
        rec = Recorder([envelope(invoice_json())])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert len(text_calls(rec)) == 1
        assert vision_calls(rec) == []
        assert result.vision_fallback_used is False
        assert result.needs_review is False

    def test_missing_seller_triggers_exactly_one_vision_attempt(
            self, logger, text_pdf, monkeypatch):
        rec = Recorder([
            envelope(invoice_json(line_items=[])),      # text: no rows
            envelope(invoice_json()),                   # vision: complete
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert len(text_calls(rec)) == 1
        assert len(vision_calls(rec)) == 1              # EXACTLY one
        assert result.vision_fallback_used is True
        assert result.vision_fallback_recovered is True
        assert result.vision_fallback_missing == []
        assert result.vision_fallback_pages == [1]
        assert result.invoice.seller_name == "Acme Logistics GmbH"
        assert result.needs_review is False
        assert result.error is False

    def test_multiple_missing_fields_still_one_attempt(self, logger,
                                                       text_pdf, monkeypatch):
        rec = Recorder([
            envelope(invoice_json(line_items=[])),
            envelope(invoice_json()),
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert len(vision_calls(rec)) == 1
        assert result.vision_fallback_missing == []

    def test_vision_recovers_rows_but_currency_stays_unresolved(
            self, logger, text_pdf, monkeypatch):
        """The fallback recovers the product rows, but a bare '$' is NOT
        resolvable - the record stays in review and no currency is
        invented."""
        rec = Recorder([
            envelope(invoice_json(line_items=[])),
            envelope(invoice_json(currency=None)),      # vision: still no ccy
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert len(vision_calls(rec)) == 1
        assert result.vision_fallback_used is True
        assert result.invoice.line_items                 # rows recovered
        assert result.invoice.currency is None          # never guessed
        assert result.needs_review is True
        assert "currency" in result.review_reason
        assert safe_review_categories(result) == ("missing_document_metadata",)

    def test_failed_vision_is_missing_fields_not_provider_failure(
            self, logger, text_pdf, monkeypatch):
        rec = Recorder([
            envelope(invoice_json(line_items=[])),      # text rejected
            envelope(invoice_json(line_items=[])),      # vision also none
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert len(vision_calls(rec)) == 1
        assert result.needs_review is True
        cats = safe_review_categories(result)
        assert "provider_failure" not in cats            # provider answered
        assert "no line items" in result.review_reason
        # partial data survives: the run keeps what WAS extracted rather
        # than discarding the document over one unavailable field
        assert result.invoice.invoice_number == "INV-1001"

    def test_accounting_counts_the_fallback_exactly_once(
            self, logger, text_pdf, monkeypatch):
        rec = Recorder([
            envelope(invoice_json(line_items=[])),
            envelope(invoice_json()),
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert result.vision_chunk_count == 1
        assert result.text_chunk_count == 1
        routes = [r.route for r in result.usage_records]
        assert routes.count(ROUTE_VISION) == 1
        assert routes.count(ROUTE_TEXT) == 1

    def test_provenance_records_text_route_trigger_and_fallback(
            self, logger, text_pdf, monkeypatch):
        rec = Recorder([
            envelope(invoice_json(line_items=[])),
            envelope(invoice_json()),
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert result.document_classification == "text-native"  # original route
        assert result.extraction_method == ROUTE_VISION         # final route
        assert result.vision_fallback_used is True
        assert result.vision_fallback_missing == []


# --- exclusions: everything that must NOT escalate --------------------------

class TestFallbackExclusions:
    def test_provider_transport_outage_never_escalates(self, logger,
                                                       text_pdf, monkeypatch):
        rec = Recorder([ProviderError("boom", category="transport")])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(max_retries=0), logger)

        assert vision_calls(rec) == []
        assert result.vision_fallback_used is False
        assert result.error is True
        assert "provider_failure" in safe_review_categories(result)

    @pytest.mark.parametrize("failure", [
        ProviderError("OpenRouter request failed (HTTP 401)",
                      category="client_error", http_status=401),
        ProviderError("OpenRouter request timed out", category="timeout"),
        ProviderError("OpenRouter request failed (HTTP 429)",
                      category="rate_limited", http_status=429),
    ])
    def test_auth_timeout_and_rate_limit_never_escalate(
            self, logger, text_pdf, monkeypatch, failure):
        rec = Recorder([failure])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(max_retries=0), logger)

        assert vision_calls(rec) == []
        assert result.vision_fallback_used is False

    def test_malformed_json_never_escalates(self, logger, text_pdf,
                                            monkeypatch):
        rec = Recorder([envelope("not json at all"),
                        envelope("still not json")])   # primary + repair
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert vision_calls(rec) == []
        assert result.vision_fallback_used is False

    def test_image_only_document_is_never_escalated_again(
            self, logger, scan_pdf, monkeypatch):
        """A document already on the vision route keeps its existing
        behavior: one vision ladder, no fallback layered on top."""
        rec = Recorder([envelope(invoice_json(line_items=[]))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(scan_pdf, fallback_cfg(), logger)

        assert len(vision_calls(rec)) == 1              # the native route
        assert result.vision_fallback_used is False     # no extra attempt

    def test_no_recursion_after_a_failed_fallback(self, logger, text_pdf,
                                                  monkeypatch):
        rec = Recorder([
            envelope(invoice_json(line_items=[])),
            envelope(invoice_json(line_items=[])),
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(text_pdf, fallback_cfg(), logger)

        assert len(vision_calls(rec)) == 1              # never a second one
        assert result.vision_chunk_count == 1

    def test_multi_page_beyond_the_chunk_bound_does_not_escalate(
            self, logger, two_page_text_pdf, monkeypatch):
        rec = Recorder([envelope(invoice_json(line_items=[]))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        cfg = fallback_cfg(max_text_pages=2, max_vision_pages=1)
        result = process_file(two_page_text_pdf, cfg, logger)

        assert vision_calls(rec) == []                  # bounded: 2 > 1
        assert result.vision_fallback_used is False

    def test_missing_vision_configuration_makes_no_call(self, logger,
                                                        text_pdf, monkeypatch):
        rec = Recorder([envelope(invoice_json(line_items=[]))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        cfg = fallback_cfg(openrouter_vision_models=())
        result = process_file(text_pdf, cfg, logger)

        assert vision_calls(rec) == []
        assert result.vision_fallback_used is False
        assert result.error is True


# --- classification taxonomy -------------------------------------------------

class TestFailureClassification:
    def test_genuine_provider_failure_keeps_its_category(self):
        from invoice_extractor.benchmark.scoring import (
            parse_review_categories,
        )
        cats, _ = parse_review_categories(
            "text chunk 1/1 (pages 1) failed on all configured models: "
            "ProviderError: HTTP 503")
        assert cats == ["provider_failure"]

    def test_provider_truncation_still_maps_to_provider_failure(self):
        from invoice_extractor.benchmark.scoring import (
            parse_review_categories,
        )
        cats, _ = parse_review_categories(
            "text chunk 1/1 (pages 1) failed on all configured models: "
            "OpenRouter response truncated before valid JSON completed")
        assert cats == ["provider_failure"]

    def test_summary_truncation_marker_is_not_a_provider_failure(self):
        """The logging summary's own '...[truncated]' marker must never be
        read as a provider truncation (the bug that mislabeled the real
        Sales Order as provider_failure)."""
        from invoice_extractor.benchmark.scoring import (
            parse_review_categories,
        )
        cats, _ = parse_review_categories(
            "anthropic/claude-sonnet-4.5: ExtractionError: missing "
            "requi...[truncated]")
        assert "provider_failure" not in cats

    def test_budget_exhaustion_keeps_its_own_reason(self, logger, text_pdf,
                                                    monkeypatch):
        """When a cost budget is exhausted, the run keeps its EXISTING
        failure message (the budget is the more actionable cause and must
        never be hidden behind a validation label) and spends nothing more
        on a fallback."""
        rec = Recorder([envelope(invoice_json(line_items=[]),
                                 cost=0.01)])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        cfg = fallback_cfg(max_cost_usd_per_file=0.0001)
        result = process_file(text_pdf, cfg, logger)

        assert vision_calls(rec) == []          # bound respected, no spend
        assert result.vision_fallback_used is False
        # keeps the pre-existing message (which carries the ladder's own
        # budget/exhaustion detail) instead of the validation-only rewrite
        assert "failed on all configured models" in result.review_reason

    def test_validation_only_null_row_reports_one_accurate_category(
            self, logger, text_pdf, monkeypatch):
        rec = Recorder([envelope(invoice_json(line_items=[]))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        cfg = fallback_cfg(openrouter_vision_models=())   # no fallback path
        result = process_file(text_pdf, cfg, logger)

        assert result.error is True
        assert safe_review_categories(result) == ("no_usable_product_rows",)
        assert "failed on all" not in result.review_reason


# --- no document-specific behavior ------------------------------------------

class TestNoHardcoding:
    def test_sources_carry_no_document_specific_values(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for rel in ("invoice_extractor/pipeline.py",
                    "invoice_extractor/benchmark/scoring.py"):
            src = (root / rel).read_text(encoding="utf-8")
            for banned in ("AUTEUR", "JBAUT", "HKD", "USD",
                           "Sales Order"):
                assert banned not in src, f"{rel}: {banned}"

    def test_currency_is_never_inferred_in_the_pipeline(self):
        """The pipeline may CLEAR an unevidenced currency but must never
        assign one: no country/locale/currency mapping lives here."""
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        src = (root / "invoice_extractor/pipeline.py").read_text(
            encoding="utf-8")
        assignments = re.findall(r"\.currency\s*=\s*(\S+)", src)
        assert assignments, "expected the evidence guard to clear currency"
        assert all(value == "None" for value in assignments), assignments
