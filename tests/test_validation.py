from decimal import Decimal

from invoice_extractor.schema import (
    missing_identifier,
    normalize_invoice,
    suspicious_line_item_rows,
    validate_invoice,
)

from .conftest import invoice_dict


def validate(**overrides):
    inv = normalize_invoice(invoice_dict(**overrides))
    return validate_invoice(inv)


class TestTotalsReconciliation:
    def test_exclusive_tax_within_tolerance(self):
        assert validate() is None  # 100 + 19 == 119

    def test_within_absolute_tolerance(self):
        assert validate(total_amount=119.01) is None  # off by 0.01 <= 0.02

    def test_outside_tolerance_is_inconclusive_flag(self):
        reason = validate(total_amount=150.0)
        assert reason is not None
        assert "totals inconclusive" in reason
        assert "discount/shipping/duties/rounding" in reason

    def test_inclusive_tax_pattern_passes(self):
        # line amounts already include tax: sum(lines) == total
        assert validate(
            line_items=[{"description": "Freight incl. VAT", "quantity": 1,
                         "unit_price": 119.0, "amount": 119.0}],
        ) is None

    def test_subtotal_chain_passes(self):
        # sum(lines) == subtotal, subtotal + tax == total
        assert validate() is None

    def test_zero_tax_exact_match(self):
        assert validate(tax_amount=0, total_amount=100.0) is None

    def test_missing_tax_not_invented(self):
        # tax null, lines == total -> passes via inclusive/zero-tax check
        assert validate(tax_amount=None, total_amount=100.0) is None
        # tax null, unexplained 15 gap -> inconclusive, not "wrong"
        reason = validate(tax_amount=None, total_amount=115.0)
        assert reason is not None and "inconclusive" in reason

    def test_configurable_tolerances(self):
        inv = normalize_invoice(invoice_dict(total_amount=125.0))  # off by 6.00
        assert validate_invoice(inv) is not None  # default tolerances flag it
        assert validate_invoice(
            inv, abs_tolerance=Decimal("10.00"), rel_tolerance=Decimal("0.005")
        ) is None  # looser absolute tolerance accepts it

    def test_relative_tolerance_scales_with_total(self):
        # 0.5% of 100000 = 500 tolerance; diff of 300 passes
        inv = normalize_invoice(invoice_dict(
            subtotal=None, tax_amount=0,
            line_items=[{"description": "Bulk", "quantity": 1,
                         "unit_price": 100000.0, "amount": 100000.0}],
            total_amount=100300.0,
        ))
        assert validate_invoice(inv) is None


class TestStructuralValidation:
    def test_missing_required_fields_flagged(self):
        # invoice_number is deliberately excluded from REQUIRED_FIELDS (see
        # TestMissingIdentifier below) - only currency is a hard-required
        # field missing here.
        reason = validate(currency=None)
        assert "missing required fields" in reason
        assert "currency" in reason

    def test_no_line_items_flagged(self):
        assert "no line items" in validate(line_items=[])

    def test_line_items_without_amounts_flagged(self):
        reason = validate(line_items=[{"description": "Freight", "quantity": 1,
                                       "unit_price": None, "amount": None}])
        assert "line items have no amounts" in reason


class TestMissingIdentifier:
    """invoice_number alone is not hard-required (see TestRequiredFields in
    test_schema.py) - real commercial/customs invoices sometimes have no
    true invoice number, only a PO number or other reference. This is only
    flagged for review when NONE of the three identifiers are present."""

    def test_invoice_number_present_needs_no_alternative(self):
        assert missing_identifier(normalize_invoice(invoice_dict())) is False
        assert validate() is None  # invoice_dict() defaults include invoice_number

    def test_missing_invoice_number_with_po_number_is_not_flagged(self):
        inv = normalize_invoice(invoice_dict(invoice_number=None, po_number="PO-4471"))
        assert missing_identifier(inv) is False
        assert validate_invoice(inv) is None

    def test_missing_invoice_number_with_reference_is_not_flagged(self):
        inv = normalize_invoice(
            invoice_dict(invoice_number=None, reference="SHIP-REF-882")
        )
        assert missing_identifier(inv) is False
        assert validate_invoice(inv) is None

    def test_missing_invoice_number_and_no_alternative_is_flagged(self):
        inv = normalize_invoice(invoice_dict(invoice_number=None))
        assert missing_identifier(inv) is True
        reason = validate_invoice(inv)
        assert reason is not None
        assert (
            "missing invoice_number and no alternative PO/reference identifier"
            in reason
        )

    def test_missing_invoice_number_and_no_alternative_does_not_block_otherwise_usable_invoice(
        self,
    ):
        # The rest of the invoice (dates, totals, line items) is fine - this
        # is needs_review, never a hard failure, and other valid data is
        # still returned normally.
        inv = normalize_invoice(invoice_dict(invoice_number=None))
        assert inv.total_amount == Decimal("119.0")
        assert len(inv.line_items) == 1


class TestSuspiciousLineItemRows:
    """Guardrail: line items with no amount are flagged as likely
    hallucinated header/label rows, even when arithmetic reconciles."""

    def test_no_suspicious_rows_for_clean_items(self):
        inv = normalize_invoice(invoice_dict())
        assert suspicious_line_item_rows(inv.line_items) == []
        assert validate_invoice(inv) is None

    def test_header_shaped_row_mixed_with_valid_rows_is_flagged(self):
        # Real rows keep the arithmetic reconciled; the spurious row has no
        # amount at all, so it does not affect the sum but is still flagged.
        reason = validate(
            subtotal=None,
            tax_amount=0,
            total_amount=100.0,
            line_items=[
                {"description": "Ocean freight", "quantity": 1,
                 "unit_price": 100.0, "amount": 100.0},
                {"description": "Description", "quantity": None,
                 "unit_price": None, "amount": None},  # hallucinated header row
            ],
        )
        assert reason is not None
        assert "totals inconclusive" not in reason  # arithmetic still reconciles
        assert "1 line item(s) missing an amount (row(s) 2)" in reason
        assert "possible hallucinated header/label row" in reason

    def test_multiple_suspicious_rows_report_all_1_based_indices(self):
        inv = normalize_invoice(invoice_dict(
            subtotal=None,
            tax_amount=0,
            total_amount=100.0,
            line_items=[
                {"description": "Description", "quantity": None,
                 "unit_price": None, "amount": None},
                {"description": "Ocean freight", "quantity": 1,
                 "unit_price": 100.0, "amount": 100.0},
                {"description": "Qty", "quantity": None,
                 "unit_price": None, "amount": None},
            ],
        ))
        assert suspicious_line_item_rows(inv.line_items) == [1, 3]
        reason = validate_invoice(inv)
        assert "row(s) 1, 3" in reason

    def test_rows_are_not_dropped_only_flagged(self):
        # normalize_invoice's any()-based filter is unchanged: a
        # description-only row still survives normalization.
        inv = normalize_invoice(invoice_dict(
            line_items=[
                {"description": "Ocean freight", "quantity": 1,
                 "unit_price": 100.0, "amount": 100.0},
                {"description": "Description", "quantity": None,
                 "unit_price": None, "amount": None},
            ],
        ))
        assert len(inv.line_items) == 2
