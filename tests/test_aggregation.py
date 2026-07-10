from decimal import Decimal

from invoice_extractor.aggregation import RouteResult, aggregate
from invoice_extractor.schema import normalize_invoice

from .conftest import invoice_dict


def route(route_name, pages, provider="gemini", model="m", **overrides):
    return RouteResult(
        route=route_name,
        pages=pages,
        invoice=normalize_invoice(invoice_dict(**overrides)),
        provider=provider,
        model=model,
    )


class TestSingleRoute:
    def test_passthrough(self):
        outcome = aggregate([route("text", [1])])
        assert outcome.invoice.invoice_number == "INV-1001"
        assert outcome.conflicts == [] and outcome.notes == []


class TestHeaderMerging:
    def test_prefer_non_null(self):
        text = route("text", [1], seller_address=None, payment_terms=None)
        vision = route("vision", [2], payment_terms="Net 60 from vision")
        # text route has payment_terms=None -> vision's non-null value wins, no conflict
        outcome = aggregate([text, vision])
        assert outcome.invoice.payment_terms == "Net 60 from vision"
        assert not any(f == "payment_terms" for f, _ in outcome.conflicts)

    def test_equal_values_after_normalization_no_conflict(self):
        a = route("text", [1], seller_name="Acme  Logistics GmbH")
        b = route("vision", [2], seller_name="ACME LOGISTICS GMBH")
        outcome = aggregate([a, b])
        assert not any(f == "seller_name" for f, _ in outcome.conflicts)

    def test_conflicting_values_flagged_with_both_values(self):
        a = route("text", [1], buyer_name="Alpha Ltd")
        b = route("vision", [2], buyer_name="Beta Ltd")
        outcome = aggregate([a, b])
        conflict = next(d for f, d in outcome.conflicts if f == "buyer_name")
        assert "Alpha Ltd" in conflict and "Beta Ltd" in conflict
        # non-monetary: first route (page order) wins, but flagged
        assert outcome.invoice.buyer_name == "Alpha Ltd"

    def test_monetary_conflict_prefers_last_page_route_and_flags(self):
        a = route("text", [1, 2], total_amount=100.0)
        b = route("vision", [3], total_amount=119.0)
        outcome = aggregate([a, b])
        assert outcome.invoice.total_amount == Decimal("119.0")
        assert any(f == "total_amount" for f, _ in outcome.conflicts)

    def test_invoice_number_conflict_flags_possible_multi_invoice(self):
        a = route("text", [1], invoice_number="INV-A")
        b = route("vision", [2], invoice_number="INV-B")
        outcome = aggregate([a, b])
        assert any("multiple invoices" in n for n in outcome.notes)


class TestLineItems:
    def test_order_preserved_by_first_contributing_page(self):
        vision = route("vision", [1], line_items=[
            {"description": "First page item", "quantity": 1, "unit_price": 10, "amount": 10},
        ])
        text = route("text", [2], line_items=[
            {"description": "Second page item", "quantity": 1, "unit_price": 20, "amount": 20},
        ])
        # pass out of order; aggregation must sort by first contributing page
        outcome = aggregate([text, vision])
        descriptions = [it.description for it in outcome.invoice.line_items]
        assert descriptions == ["First page item", "Second page item"]

    def test_exact_duplicates_dropped_with_note(self):
        dup = {"description": "Handling fee", "quantity": 1, "unit_price": 50, "amount": 50}
        a = route("text", [1], line_items=[dup])
        b = route("vision", [2], line_items=[dict(dup)])
        outcome = aggregate([a, b])
        assert len(outcome.invoice.line_items) == 1
        assert any("duplicate" in n for n in outcome.notes)

    def test_partially_null_items_are_never_deduped(self):
        # weak evidence (null unit_price) -> keep both
        item = {"description": "Misc", "quantity": None, "unit_price": None, "amount": 50}
        a = route("text", [1], line_items=[item])
        b = route("vision", [2], line_items=[dict(item)])
        outcome = aggregate([a, b])
        assert len(outcome.invoice.line_items) == 2
