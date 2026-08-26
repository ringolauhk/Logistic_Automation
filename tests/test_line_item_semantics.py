"""M10: semantic validation of line items at the model-attempt boundary.

The confirmed live failure: a one-page text-native commercial invoice whose
PDF text layer scrambles the item-table column order (per row the stream
reads BARCODE, TOTAL PRICE, PRODUCT CODE, NAME, COUNTRY, HS CODE, QUANTITY,
UNIT PRICE). A text model mapped columns by stream position, returning each
row's TOTAL PRICE as quantity and amount = that total x unit price - a
structurally perfect, semantically impossible extraction that was accepted
and merely flagged for review.

These tests use a SYNTHETIC invoice with the same table pattern (13 rows,
quantity sum 360, line sum 18756, scrambled stream order) and fake vendor /
product / barcode values. Every pipeline test uses the fake OpenRouter
transport seam and asserts exact call counts.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_extractor import openrouter_client
from invoice_extractor.pipeline import (
    _validation_only_exhaustion,
    process_file,
    safe_review_categories,
)
from invoice_extractor.schema import (
    LINE_ITEM_FIELDS,
    LineItemSemanticError,
    check_line_item_semantics,
    line_item_semantic_finding,
    normalize_invoice,
)
from invoice_extractor.usage import LadderExhaustedError

from .conftest import build_pdf, invoice_dict, make_config
from .test_openrouter_pipeline import Recorder, envelope
from .test_vision_fallback import _record, text_calls, vision_calls

# --- synthetic fixture: same PATTERN as the live case, fake values -----------
# (code, barcode, name, qty, unit, total) - 13 rows, sum(qty)=360,
# sum(total)=18756, one legitimately large-quantity row (78) and varied
# magnitudes so nothing depends on absolute size.
ROWS = [
    ("APSER01", "4900000000017", "Alpha Serum 30ml", 12, 104, 1248),
    ("APDAY01", "4900000000024", "Alpha Day Cream 50ml", 6, 74, 444),
    ("BMASK15", "4900000000031", "Beta Face Mask Box", 78, 42, 3276),
    ("GSUPP02", "4900000000048", "Gamma Dietary Supplement", 18, 38, 684),
    ("DCRM50", "4900000000055", "Delta Cream 50ml", 6, 260, 1560),
    ("DSER30", "4900000000062", "Delta Serum 30ml", 12, 192, 2304),
    ("DEYE15", "4900000000079", "Delta Eye Cream 15ml", 18, 84, 1512),
    ("EBOX48", "4900000000086", "Epsilon Eye Box", 72, 36, 2592),
    ("DEMU50", "4900000000093", "Delta Emulsion 50ml", 6, 180, 1080),
    ("ZMASK22", "4900000000109", "Zeta Facial Mask Box", 24, 40, 960),
    ("EMASK36", "4900000000116", "Eta Eye Mask Boxed", 36, 34, 1224),
    ("EMASK50", "4900000000123", "Eta Facial Mask Boxed", 36, 40, 1440),
    ("ASPF15", "4900000000130", "Alpha Sunscreen 15ml", 36, 12, 432),
]
QTY_TOTAL = 360
LINE_TOTAL = 18756


def scrambled_invoice_text(row_limit: int | None = None) -> str:
    """The item table exactly as the live PDF's text layer presents it:
    header words split across lines, then per row BARCODE, TOTAL, CODE,
    NAME, COUNTRY, HS CODE, QUANTITY, UNIT PRICE. row_limit keeps the
    synthetic PDF page small enough for build_pdf's textbox (the fake
    transport never reads the page - the scrambled PATTERN is what the
    fixture documents)."""
    parts = [
        "Commercial Invoice",
        "Invoice: 1S9990001111",
        "Date: 7/23/2026",
        "Currency: GBP Pound Sterling",
        "Example Beauty Ltd",
        "PRODUCT CODE BARCODE",
        "ITEM NAME",
        "QUANTITY",
        "UNIT",
        "PRICE",
        "TOTAL",
        "PRICE",
        "COUNTRY",
        "OF ORIGIN",
        "HS CODE",
        "MANUFACTURER",
    ]
    for code, barcode, name, qty, unit, total in ROWS[:row_limit]:
        parts += [f" {barcode}", f"{total}.00", f" {code}", f" {name}",
                  "CH", " 3304990000", str(qty), f"{unit}.00"]
    parts += [f" {QTY_TOTAL}", f"{LINE_TOTAL}.00", "TOTALS"]
    return "\n".join(parts)


def good_response() -> str:
    """A correctly mapped model answer for the synthetic invoice."""
    return json.dumps(invoice_dict(
        invoice_number="1S9990001111", currency="GBP",
        seller_name="Example Beauty Ltd", subtotal=None, tax_amount=None,
        total_amount=LINE_TOTAL,
        line_items=[
            {"item_code": c, "barcode": b, "description": n,
             "quantity": q, "unit_price": u, "amount": t}
            for c, b, n, q, u, t in ROWS
        ]))


def shifted_response(*, copy_amount: bool = False) -> str:
    """The observed failure: TOTAL PRICE lands in quantity, product code is
    replaced by the barcode. amount is either recomputed (qty x unit, the
    live behavior) or copied unchanged from the printed total."""
    return json.dumps(invoice_dict(
        invoice_number="1S9990001111", currency="GBP",
        seller_name="Example Beauty Ltd", subtotal=None, tax_amount=None,
        total_amount=LINE_TOTAL,
        line_items=[
            {"item_code": b, "description": n, "quantity": t,
             "unit_price": u, "amount": (t if copy_amount else t * u)}
            for c, b, n, q, u, t in ROWS
        ]))


def semantic_cfg(n_text_models=2, **overrides):
    models = tuple(f"test-vendor/text-{i + 1}" for i in range(n_text_models))
    base = dict(
        llm_gateway="openrouter",
        openrouter_api_key="test-or-key",
        openrouter_text_models=models,
        openrouter_vision_models=("test-vendor/vision-1",),
        max_retries=1,
    )
    base.update(overrides)
    return make_config(**base)


@pytest.fixture
def scrambled_pdf(tmp_path):
    return Path(build_pdf(tmp_path / "scrambled.pdf",
                          [("text", scrambled_invoice_text(row_limit=3))]))


def _invoice(**overrides):
    return normalize_invoice(invoice_dict(**overrides))


# --- the deterministic finding function --------------------------------------

class TestSemanticFinding:
    def test_correct_extraction_passes(self):
        inv = normalize_invoice(json.loads(good_response()))
        assert line_item_semantic_finding(inv) is None
        check_line_item_semantics(inv)              # no raise

    def test_shifted_recomputed_amounts_detected(self):
        inv = normalize_invoice(json.loads(shifted_response()))
        finding = line_item_semantic_finding(inv)
        assert finding is not None
        assert "semantic mismatch" in finding
        with pytest.raises(LineItemSemanticError):
            check_line_item_semantics(inv)

    def test_shifted_copied_amounts_detected_without_totals(self):
        """quantity == printed amount rows are caught even when the chunk
        carries no invoice total at all."""
        inv = _invoice(total_amount=None, subtotal=None, line_items=[
            {"description": "A", "quantity": 500, "unit_price": 5,
             "amount": 500},
            {"description": "B", "quantity": 240, "unit_price": 12,
             "amount": 240},
        ])
        assert line_item_semantic_finding(inv) is not None

    def test_single_bad_row_never_systematic(self):
        """One inconsistent row can be a legitimate discount/bundle - it is
        left to the aggregate totals reconciliation, not attempt rejection."""
        inv = _invoice(total_amount=100, line_items=[
            {"description": "Bundle", "quantity": 400, "unit_price": 2,
             "amount": 400},
            {"description": "Normal", "quantity": 2, "unit_price": 10,
             "amount": 20},
        ])
        assert line_item_semantic_finding(inv) is None

    def test_legitimate_high_quantity_wholesale_passes(self):
        """No absolute quantity threshold: 50,000 units at 0.30 is fine
        because everything stays consistent with the document total."""
        inv = _invoice(total_amount=16000, subtotal=None, line_items=[
            {"description": "Bulk widget", "quantity": 50000,
             "unit_price": Decimal("0.30"), "amount": 15000},
            {"description": "Freight", "quantity": 1,
             "unit_price": 1000, "amount": 1000},
        ])
        assert line_item_semantic_finding(inv) is None

    def test_explicit_discount_and_foc_lines_do_not_trip(self):
        """Negative (discount/credit) and zero-price (free-of-charge) lines
        carry no shift evidence and never cause rejection."""
        inv = _invoice(total_amount=90, line_items=[
            {"description": "Product", "quantity": 10, "unit_price": 10,
             "amount": 100},
            {"description": "Trade discount", "quantity": 1,
             "unit_price": -10, "amount": -10},
            {"description": "FOC sample", "quantity": 500, "unit_price": 0,
             "amount": 0},
        ])
        assert line_item_semantic_finding(inv) is None

    def test_rounding_within_tolerance_passes(self):
        inv = _invoice(total_amount=Decimal("33.34"), line_items=[
            {"description": "A", "quantity": 3,
             "unit_price": Decimal("3.333"), "amount": Decimal("10.00")},
            {"description": "B", "quantity": 7,
             "unit_price": Decimal("3.334"), "amount": Decimal("23.34")},
        ])
        assert line_item_semantic_finding(inv) is None

    def test_material_systematic_differences_fail(self):
        """Two+ rows whose computed value dwarfs the stated total plus a
        grossly incompatible line sum = systematic, not a discount."""
        inv = _invoice(total_amount=100, subtotal=None, line_items=[
            {"description": "A", "quantity": 900, "unit_price": 9,
             "amount": 8100},
            {"description": "B", "quantity": 800, "unit_price": 7,
             "amount": 5600},
        ])
        assert line_item_semantic_finding(inv) is not None

    def test_missing_amounts_follow_existing_policy(self):
        """Rows without an amount are not failed for the missing amount
        itself (suspicious_line_item_rows owns that) - but their computed
        value still counts as shift evidence when it dwarfs the total."""
        ok = _invoice(total_amount=120, line_items=[
            {"description": "A", "quantity": 10, "unit_price": 10,
             "amount": None},
            {"description": "B", "quantity": 2, "unit_price": 10,
             "amount": 20},
        ])
        assert line_item_semantic_finding(ok) is None
        bad = _invoice(total_amount=100, subtotal=None, line_items=[
            {"description": "A", "quantity": 1300, "unit_price": 9,
             "amount": None},
            {"description": "B", "quantity": 1200, "unit_price": 7,
             "amount": None},
        ])
        assert line_item_semantic_finding(bad) is not None

    def test_no_totals_and_no_copy_pattern_stays_conservative(self):
        """Without a stated total the exceeds-total signature cannot fire:
        nothing is rejected on magnitude alone."""
        inv = _invoice(total_amount=None, subtotal=None, line_items=[
            {"description": "A", "quantity": 1248, "unit_price": 104,
             "amount": 129792},
            {"description": "B", "quantity": 444, "unit_price": 74,
             "amount": 32856},
        ])
        assert line_item_semantic_finding(inv) is None


# --- ladder behavior ----------------------------------------------------------

class TestLadderRecovery:
    def test_bad_first_model_rejected_good_second_accepted(
            self, logger, scrambled_pdf, monkeypatch):
        rec = Recorder([
            envelope(shifted_response()),           # model 1: column shift
            envelope(good_response()),              # model 2: correct
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(scrambled_pdf, semantic_cfg(), logger)

        assert len(text_calls(rec)) == 2            # exactly one escalation
        assert vision_calls(rec) == []
        assert result.error is False
        assert result.invoice.total_amount == LINE_TOTAL
        assert len(result.invoice.line_items) == 13
        assert sum(i.quantity for i in result.invoice.line_items) == QTY_TOTAL
        assert sum(i.amount for i in result.invoice.line_items) == LINE_TOTAL
        # product code and barcode both survive, in the right fields
        assert [i.item_code for i in result.invoice.line_items] == \
            [r[0] for r in ROWS]
        assert [i.barcode for i in result.invoice.line_items] == \
            [r[1] for r in ROWS]
        # accepted extraction reconciles - no totals-inconclusive review
        assert result.needs_review is False
        # provenance: 2 attempts recorded, first rejected semantically
        rejected = [r for r in result.usage_records if not r.accepted]
        assert [r.rejection_category for r in rejected] == \
            ["line_item_semantic_mismatch"]
        assert [r.accepted for r in result.usage_records] == [False, True]

    def test_semantic_rejection_is_not_provider_failure(
            self, logger, scrambled_pdf, monkeypatch):
        rec = Recorder([envelope(shifted_response())])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        cfg = semantic_cfg(1, openrouter_vision_models=())
        result = process_file(scrambled_pdf, cfg, logger)

        assert result.needs_review is True
        cats = safe_review_categories(result)
        assert "line_item_semantic_mismatch" in cats
        assert "provider_failure" not in cats
        # the corrupted rows were NOT published as accepted extraction
        assert result.invoice.line_items == []
        assert result.error is True

    def test_all_invalid_text_attempts_trigger_vision_exactly_once(
            self, logger, scrambled_pdf, monkeypatch):
        rec = Recorder([
            envelope(shifted_response()),           # text 1: shift
            envelope(shifted_response(copy_amount=True)),   # text 2: shift
            envelope(good_response()),              # vision fallback: correct
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(scrambled_pdf, semantic_cfg(), logger)

        assert len(text_calls(rec)) == 2
        assert len(vision_calls(rec)) == 1          # exactly one fallback
        assert result.vision_fallback_used is True
        assert result.error is False
        assert sum(i.quantity for i in result.invoice.line_items) == QTY_TOTAL

    def test_attempt_cap_four_means_no_fifth_request(
            self, logger, scrambled_pdf, monkeypatch):
        rec = Recorder([envelope(shifted_response())] * 10)
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        cfg = semantic_cfg(4, max_model_attempts_per_file=4)
        result = process_file(scrambled_pdf, cfg, logger)

        assert len(rec.calls) == 4                  # cap honored, no vision
        assert result.error is True

    def test_validation_only_exhaustion_accepts_semantic_category(self):
        exc = LadderExhaustedError("x", [
            _record("line_item_semantic_mismatch"),
            _record("missing_required_fields"),
        ])
        assert _validation_only_exhaustion(exc) is True
        exc = LadderExhaustedError("x", [
            _record("line_item_semantic_mismatch"), _record("transport")])
        assert _validation_only_exhaustion(exc) is False

    def test_attempt_and_cost_accounting_stays_accurate(
            self, logger, scrambled_pdf, monkeypatch):
        rec = Recorder([
            envelope(shifted_response(), cost=0.002),
            envelope(good_response(), cost=0.003),
        ])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(scrambled_pdf, semantic_cfg(), logger)

        assert len(result.usage_records) == 2
        assert [float(r.cost_usd) for r in result.usage_records] == \
            [0.002, 0.003]
        assert result.usage_records[0].requested_model == "test-vendor/text-1"
        assert result.usage_records[1].requested_model == "test-vendor/text-2"


# --- compatibility ------------------------------------------------------------

class TestCompatibility:
    def test_workbook_line_columns_gain_only_barcode(self):
        from invoice_extractor.excel_export import LINE_ITEM_COLUMNS
        assert LINE_ITEM_COLUMNS == [
            "invoice_id", "line_number", "source_file",
            "line_no", "item_code", "barcode", "description",
            "quantity", "unit_price", "amount",
            # M11 appends warning columns; earlier ones keep their order.
            "missing_fields", "validation_warnings",
        ]

    def test_barcode_optional_and_survives_normalization(self):
        inv = _invoice(line_items=[
            {"item_code": "A1", "barcode": "4900000000017",
             "description": "X", "quantity": 1, "unit_price": 2,
             "amount": 2},
            {"item_code": "B2", "description": "Y", "quantity": 1,
             "unit_price": 3, "amount": 3},
        ])
        assert inv.line_items[0].barcode == "4900000000017"
        assert inv.line_items[1].barcode is None    # never required

    def test_prompt_distinguishes_code_barcode_and_columns(self):
        from invoice_extractor.prompts import JSON_SCHEMA_BLOCK, RULES
        assert '"barcode"' in JSON_SCHEMA_BLOCK
        assert "COLUMN HEADER MEANING" in RULES
        assert "amount is NEVER the quantity" in RULES

    def test_totals_inconclusive_review_still_available(self, logger,
                                                        tmp_path,
                                                        monkeypatch):
        """Genuine shipping/tax/duty differences keep their existing review
        path - a modest unexplained difference is NOT a semantic failure."""
        pdf = Path(build_pdf(tmp_path / "plain.pdf",
                             [("text", scrambled_invoice_text(row_limit=3))]))
        rec = Recorder([envelope(json.dumps(invoice_dict(
            subtotal=None, tax_amount=None, total_amount=120,
            line_items=[{"description": "Goods", "quantity": 2,
                         "unit_price": 50, "amount": 100}])))])
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)

        result = process_file(pdf, semantic_cfg(1), logger)

        assert len(rec.calls) == 1                  # accepted, no escalation
        assert result.error is False
        assert result.needs_review is True
        assert "totals inconclusive" in result.review_reason

    def test_no_document_specific_hardcoding(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("invoice_extractor/schema.py",
                    "invoice_extractor/openrouter_client.py",
                    "invoice_extractor/pipeline.py",
                    "invoice_extractor/prompts.py"):
            src = (root / rel).read_text(encoding="utf-8")
            for banned in ("SAL2702459", "111SKIN", "111 Skin", "RESERSS",
                           "18756", "129792", "5060280"):
                assert banned not in src, f"{rel}: {banned}"

    def test_line_item_fields_shape(self):
        assert LINE_ITEM_FIELDS == ["line_no", "item_code", "barcode",
                                    "description", "quantity", "unit_price",
                                    "amount"]
