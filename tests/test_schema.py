from decimal import Decimal

import pytest

from invoice_extractor.schema import (
    ExtractionError,
    Invoice,
    check_required,
    coerce_decimal,
    missing_required_fields,
    normalize_currency,
    normalize_invoice,
    unknown_keys,
)

from .conftest import invoice_dict


class TestCoerceDecimal:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1,234.56", Decimal("1234.56")),
            ("1.234,56", Decimal("1234.56")),
            ("€1,547.00", Decimal("1547.00")),
            ("$99", Decimal("99")),
            ("-5", Decimal("-5")),
            ("(123.45)", Decimal("-123.45")),
            ("(1,000.00)", Decimal("-1000.00")),
            ("123,45", Decimal("123.45")),
            ("12,345", Decimal("12345")),
            (0, Decimal("0")),
            ("0", Decimal("0")),
            ("0.00", Decimal("0.00")),
            (42, Decimal("42")),
            (19.5, Decimal("19.5")),
            (Decimal("7.77"), Decimal("7.77")),
            (None, None),
            ("", None),
            ("   ", None),
            ("n/a", None),
            (True, None),
            (["1"], None),
        ],
    )
    def test_coercion(self, value, expected):
        assert coerce_decimal(value) == expected

    def test_zero_is_preserved_not_nulled(self):
        result = coerce_decimal(0)
        assert result is not None
        assert result == Decimal("0")

    def test_returns_decimal_type(self):
        assert isinstance(coerce_decimal("1,234.56"), Decimal)


class TestCurrency:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("eur", "EUR"),
            (" USD ", "USD"),
            ("€", "EUR"),
            ("£", "GBP"),
            (None, None),
            ("", None),
            ("Euro", "Euro"),  # not invented/guessed - kept for the reviewer
        ],
    )
    def test_normalize(self, value, expected):
        assert normalize_currency(value) == expected


class TestNormalizeInvoice:
    def test_full_invoice(self):
        inv = normalize_invoice(invoice_dict())
        assert inv.invoice_number == "INV-1001"
        assert inv.total_amount == Decimal("119.0")
        assert isinstance(inv.total_amount, Decimal)
        assert len(inv.line_items) == 1
        assert inv.line_items[0].quantity == Decimal("1")

    def test_unknown_keys_dropped_and_reported(self):
        raw = invoice_dict(notes="model commentary", confidence=0.9)
        assert unknown_keys(raw) == ["confidence", "notes"]
        inv = normalize_invoice(raw)
        assert not hasattr(inv, "notes")

    def test_empty_strings_become_null(self):
        inv = normalize_invoice(invoice_dict(seller_name="  ", payment_terms=""))
        assert inv.seller_name is None
        assert inv.payment_terms is None

    def test_zero_tax_survives(self):
        inv = normalize_invoice(invoice_dict(tax_amount=0))
        assert inv.tax_amount == Decimal("0")

    def test_quantity_supports_decimals(self):
        raw = invoice_dict(line_items=[
            {"description": "Fuel surcharge", "quantity": "2.5",
             "unit_price": "10.00", "amount": "25.00"},
        ])
        inv = normalize_invoice(raw)
        assert inv.line_items[0].quantity == Decimal("2.5")

    def test_non_dict_raises(self):
        with pytest.raises(ExtractionError):
            normalize_invoice(["not", "a", "dict"])

    def test_pydantic_models_forbid_extras(self):
        with pytest.raises(Exception):
            Invoice(bogus_field="x")


class TestRequiredFields:
    def test_missing_required(self):
        inv = normalize_invoice({"seller_name": "X"})
        missing = missing_required_fields(inv)
        assert "invoice_number" in missing
        assert "total_amount" in missing
        with pytest.raises(ExtractionError):
            check_required(inv)

    def test_complete_passes(self):
        check_required(normalize_invoice(invoice_dict()))
