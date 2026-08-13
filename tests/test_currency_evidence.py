"""M9.3: an extracted currency is accepted only when the SOURCE evidences it.

A model may return e.g. HKD for a document that prints a bare "$" simply
because the addresses are in Hong Kong. That is inference, not evidence -
and it must not let the document leave review. The check is deterministic,
offline, and never triggers another provider or vision attempt.
"""

from pathlib import Path

import pytest

from invoice_extractor import openrouter_client
from invoice_extractor.pipeline import process_file, safe_review_categories
from invoice_extractor.schema import currency_evidence_supports

from .conftest import invoice_json, make_config
from .test_openrouter_pipeline import Recorder, envelope
from .test_vision_fallback import fallback_cfg, text_calls, vision_calls

# A realistic Hong Kong sales-order body: prices carry ONLY a bare "$".
HK_BARE_DOLLAR_BODY = (
    "Sales Order JBX-2026-1 dated 2026-06-24 from the supplier. "
    "Billing Address: Example Trading Limited, 30/F One Island South, "
    "2 Heung Yip Road, Hong Kong. Shipping Address: Example Trading "
    "Limited, Kwai Chung, Hong Kong. Line Serum 30ml, quantity 1, unit "
    "price $136.50, discount 100 %, subtotal $0.00. Total $0.00."
)
US_BARE_DOLLAR_BODY = (
    "Invoice INV-77 dated 2026-06-24. Seller: Example Corp, 500 Market "
    "Street, San Francisco, California, United States. Freight $250.00. "
    "Total $250.00."
)


# --- the deterministic evidence rule ----------------------------------------

class TestCurrencyEvidenceRule:
    @pytest.mark.parametrize("text,currency", [
        ("Total 119.00 HKD", "HKD"),
        ("total hkd 119.00", "HKD"),                 # case-insensitive
        ("Amount due HK$119.00", "HKD"),
        ("Amount due hk$119.00", "HKD"),
        ("Total 250.00 USD", "USD"),
        ("Total US$250.00", "USD"),
        ("Betrag 119,00 EUR", "EUR"),
        ("Total €119.00", "EUR"),
        ("Total £119.00", "GBP"),
        ("Total S$119.00", "SGD"),
    ])
    def test_explicit_evidence_is_accepted(self, text, currency):
        assert currency_evidence_supports(text, currency) is True

    @pytest.mark.parametrize("text,currency", [
        ("Unit price $136.50. Total $0.00.", "HKD"),
        ("Unit price $136.50. Total $0.00.", "USD"),
        ("Unit price $136.50. Total $0.00.", "CAD"),
        ("Unit price $136.50. Total $0.00.", "AUD"),
        (HK_BARE_DOLLAR_BODY, "HKD"),                # HK address + "$"
        (US_BARE_DOLLAR_BODY, "USD"),                # US address + "$"
        ("Total ¥1200", "JPY"),                      # shared symbol too
        ("Total ¥1200", "CNY"),
    ])
    def test_ambiguous_symbol_never_supports_a_currency(self, text, currency):
        assert currency_evidence_supports(text, currency) is False

    @pytest.mark.parametrize("text,currency", [
        ("Delivered by USDA inspectors", "USD"),     # inside a word
        ("Order form reference FORM-12", "MYR"),     # 'RM' inside FORM
        ("Chkd by supervisor", "HKD"),
        ("EUROPEAN logistics services", "EUR"),
    ])
    def test_false_matches_inside_words_are_rejected(self, text, currency):
        assert currency_evidence_supports(text, currency) is False

    def test_empty_inputs_are_never_evidence(self):
        assert currency_evidence_supports("", "HKD") is False
        assert currency_evidence_supports("Total 119 HKD", None) is False
        assert currency_evidence_supports("Total 119 HKD", "") is False


# --- pipeline integration ----------------------------------------------------

def _pdf(pdf_factory, body, name):
    return Path(pdf_factory([("text", body)], name=name))


class TestPipelineEnforcement:
    def test_inferred_currency_rejected_other_fields_preserved(
            self, logger, pdf_factory, monkeypatch):
        """The real failure mode: the model returns HKD for a bare-'$'
        Hong Kong document. The currency is dropped and the row goes to
        review; everything else it extracted survives."""
        pdf = _pdf(pdf_factory, HK_BARE_DOLLAR_BODY, "hk-bare.pdf")
        rec = Recorder([envelope(invoice_json(currency="HKD",
                                              seller_name="AUTEUR"))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, fallback_cfg(), logger)

        assert len(rec.calls) == 1                   # no extra provider call
        assert vision_calls(rec) == []               # and no vision attempt
        assert result.invoice.currency is None       # inference rejected
        assert result.rejected_currency == "HKD"     # provenance kept
        assert result.invoice.seller_name == "AUTEUR"        # preserved
        assert result.invoice.invoice_date == "2026-07-01"   # preserved
        assert result.invoice.line_items                     # preserved
        assert result.needs_review is True
        assert "currency lacks explicit source evidence" in result.review_reason
        cats = safe_review_categories(result)
        assert cats == ("missing_required_fields",)
        assert "provider_failure" not in cats

    def test_us_address_with_bare_dollar_also_rejected(self, logger,
                                                       pdf_factory,
                                                       monkeypatch):
        pdf = _pdf(pdf_factory, US_BARE_DOLLAR_BODY, "us-bare.pdf")
        rec = Recorder([envelope(invoice_json(currency="USD"))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, fallback_cfg(), logger)

        assert result.invoice.currency is None
        assert result.rejected_currency == "USD"
        assert result.needs_review is True

    @pytest.mark.parametrize("body,currency", [
        ("Invoice INV-9 dated 2026-07-01. Seller Acme Ltd. Total 119.00 HKD.",
         "HKD"),
        ("Invoice INV-9 dated 2026-07-01. Seller Acme Ltd. Total HK$119.00.",
         "HKD"),
        ("Invoice INV-9 dated 2026-07-01. Seller Acme Ltd. Total US$119.00.",
         "USD"),
    ])
    def test_documents_with_explicit_evidence_are_unaffected(
            self, logger, pdf_factory, monkeypatch, body, currency):
        pdf = _pdf(pdf_factory, body, "explicit.pdf")
        rec = Recorder([envelope(invoice_json(currency=currency))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, fallback_cfg(), logger)

        assert result.invoice.currency == currency   # accepted unchanged
        assert result.rejected_currency is None
        assert result.needs_review is False

    def test_logo_recovery_plus_ambiguous_currency_ends_in_review(
            self, logger, pdf_factory, monkeypatch):
        """End-to-end shape of the live Sales Order: the vision fallback
        recovers the seller from the logo, the model's inferred currency is
        rejected, and the document lands in review - with exactly one
        vision attempt and no further calls."""
        pdf = _pdf(pdf_factory, HK_BARE_DOLLAR_BODY, "logo-doc.pdf")
        rec = Recorder([
            envelope(invoice_json(seller_name=None, currency=None)),
            envelope(invoice_json(seller_name="AUTEUR", currency="HKD")),
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, fallback_cfg(), logger)

        assert len(text_calls(rec)) == 1
        assert len(vision_calls(rec)) == 1           # exactly one fallback
        assert result.vision_fallback_used is True
        assert result.invoice.seller_name == "AUTEUR"
        assert result.invoice.currency is None
        assert result.rejected_currency == "HKD"
        assert result.needs_review is True
        assert safe_review_categories(result) == ("missing_required_fields",)

    def test_rejection_never_triggers_another_attempt(self, logger,
                                                      pdf_factory,
                                                      monkeypatch):
        """Rule 9: the evidence rejection happens AFTER extraction and must
        never cause a retry, escalation or second vision attempt."""
        pdf = _pdf(pdf_factory, HK_BARE_DOLLAR_BODY, "no-retry.pdf")
        rec = Recorder([envelope(invoice_json(currency="HKD"))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, fallback_cfg(), logger)

        assert len(rec.calls) == 1                   # exactly one, ever
        assert result.vision_fallback_used is False
        assert result.vision_chunk_count == 0

    def test_scanned_document_without_text_is_not_penalized(
            self, logger, pdf_factory, monkeypatch):
        """An image-only page exposes no text to verify against; rejecting
        every currency there would be guesswork of the opposite kind."""
        pdf = Path(pdf_factory([("image",)], name="scan-only.pdf"))
        rec = Recorder([envelope(invoice_json(currency="HKD"))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, fallback_cfg(), logger)

        assert result.invoice.currency == "HKD"      # unchanged
        assert result.rejected_currency is None
        assert result.needs_review is False


class TestNoDocumentSpecificRules:
    def test_no_hardcoded_document_values(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("invoice_extractor/schema.py",
                    "invoice_extractor/pipeline.py"):
            src = (root / rel).read_text(encoding="utf-8")
            for banned in ("AUTEUR", "JBAUT", "Heung Yip", "Sales Order"):
                assert banned not in src, f"{rel}: {banned}"

    def test_required_fields_not_weakened(self):
        from invoice_extractor.schema import REQUIRED_FIELDS
        assert "currency" in REQUIRED_FIELDS
        assert "seller_name" in REQUIRED_FIELDS
