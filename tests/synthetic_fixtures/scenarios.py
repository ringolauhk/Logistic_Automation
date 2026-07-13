"""Scenario registry: wires each approved fixture ID to its hand-authored
ground-truth record and a concrete PDF builder function.

MILESTONE 2 SCOPE: this module reads values OUT of ground_truth.py (already
committed, unchanged here) and uses them as controlled inputs to construct
each PDF's visible content via builders.py. It never calls
normalize_invoice(), validate_invoice(), aggregate(), or any pipeline/provider
code - the ground truth is not derived from what these builders produce, and
these builders are not verified against the ground truth by calling
invoice_extractor at all (that is milestone 3+'s job).

Where a fixture's ground-truth record does not pin down a particular visible
field (fixture 9's identifying header info, fixture 10's per-invoice line
items), a concrete scenario-authoring choice is made here and called out in a
comment - ground truth intentionally left those fields unconstrained because
only the arithmetic/conflict/routing behavior is under test for those two
fixtures, not header content.

Importing this module never writes a file. Building one fixture never builds
another (build_scenario() only ever touches its own output path).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import builders as b
from . import ground_truth as gt

# =============================================================================
# Registry types
# =============================================================================

BuilderFn = Callable[[Path], Path]


@dataclass(frozen=True)
class ScenarioSpec:
    fixture_id: str
    filename: str
    ground_truth: object  # one of gt.ExpectedInvoiceScenario / ExpectedHeaderHallucinationScenario / ...
    builder: BuilderFn


# =============================================================================
# Fixture 1 - Multi-page text-native invoice
# =============================================================================

def _build_fixture_01(output_path: Path) -> Path:
    gtr = gt.FIXTURE_01
    doc = b.new_document()

    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    table_header = b.table_header_line()
    items = [
        (li.description, li.quantity, li.unit_price, li.amount)
        for li in gtr.expected_line_items
    ]
    item_lines = b.line_item_lines(items)

    # Page 1: header + table header + items 1-2
    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=table_header, item_lines=item_lines[0:2],
    ))
    # Page 2: items 3-4 only (no header repeat - fixture 8 is the dedicated
    # repeated-header scenario)
    b.add_text_page(doc, item_lines[2:4])
    # Page 3: items 5-6 + totals + payment terms
    totals = b.totals_lines(
        subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
        total=gtr.expected_total_amount,
    )
    footer = b.footer_lines(gtr.expected_payment_terms)
    b.add_text_page(doc, b.compose_invoice_lines(
        item_lines=item_lines[4:6], totals=totals, footer=footer,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 2 - Multi-page scanned invoice exceeding MAX_VISION_PAGES
# =============================================================================

def _build_fixture_02(output_path: Path) -> Path:
    gtr = gt.FIXTURE_02
    doc = b.new_document()

    n_pages = len(gtr.expected_line_items)
    for i, li in enumerate(gtr.expected_line_items, start=1):
        # Header context repeated on every page so each vision chunk is
        # understandable on its own (special requirement for fixture 2).
        header = b.header_lines(
            invoice_number=gtr.expected_invoice_number,
            invoice_date=gtr.expected_invoice_date,
            currency_label=gtr.expected_currency,
            seller_name=gtr.expected_seller_name,
            buyer_name=gtr.expected_buyer_name,
        )
        item_lines = b.line_item_lines([(li.description, li.quantity, li.unit_price, li.amount)])
        extra_notes = [f"(page {i} of {n_pages})"]
        totals = footer = None
        if i == n_pages:  # totals/footer only on the last scanned page
            totals = b.totals_lines(
                subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
                total=gtr.expected_total_amount,
            )
            footer = b.footer_lines(gtr.expected_payment_terms)
        lines = b.compose_invoice_lines(
            header=header, table_header=b.table_header_line(), item_lines=item_lines,
            totals=totals, footer=footer, extra_notes=extra_notes,
        )
        b.add_rendered_image_page(doc, lines)

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 3 - Mixed text-native, scanned, and blank document
# =============================================================================

def _build_fixture_03(output_path: Path) -> Path:
    gtr = gt.FIXTURE_03
    doc = b.new_document()
    items = gtr.expected_line_items  # (cover item, scan item A, scan item B)

    # Page 1 (text): header + item 1
    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    item1_lines = b.line_item_lines([(items[0].description, items[0].quantity,
                                       items[0].unit_price, items[0].amount)])
    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item1_lines,
    ))

    # Page 2: genuinely blank separator
    b.add_blank_page(doc)

    # Page 3 (image): scanned attachment item A
    item2_lines = b.line_item_lines([(items[1].description, items[1].quantity,
                                       items[1].unit_price, items[1].amount)])
    b.add_rendered_image_page(doc, [f"Invoice {gtr.expected_invoice_number} - Attachment"]
                               + [""] + item2_lines)

    # Page 4 (image): scanned attachment item B
    item3_lines = b.line_item_lines([(items[2].description, items[2].quantity,
                                       items[2].unit_price, items[2].amount)])
    b.add_rendered_image_page(doc, [f"Invoice {gtr.expected_invoice_number} - Attachment"]
                               + [""] + item3_lines)

    # Page 5 (text): footer - totals + payment terms, no new items
    totals = b.totals_lines(
        subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
        total=gtr.expected_total_amount,
    )
    footer = b.footer_lines(gtr.expected_payment_terms)
    b.add_text_page(doc, b.compose_invoice_lines(totals=totals, footer=footer))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 4 - EUR European-number-format invoice
# =============================================================================

def _build_fixture_04(output_path: Path) -> Path:
    gtr = gt.FIXTURE_04
    doc = b.new_document()

    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    items = [
        (li.description, li.quantity, li.unit_price, li.amount)
        for li in gtr.expected_line_items
    ]
    # Visible text uses European decimal-comma/thousands-dot formatting
    # throughout - required substrings: "1.234,56", "234,57", "1.469,13".
    item_lines = b.line_item_lines(items, amount_formatter=b.format_amount_eu)
    totals = b.totals_lines(
        subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
        total=gtr.expected_total_amount,
        subtotal_label="Zwischensumme", tax_label="MwSt", total_label="Gesamtbetrag",
        amount_formatter=b.format_amount_eu,
    )
    footer = b.footer_lines(gtr.expected_payment_terms)

    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item_lines,
        totals=totals, footer=footer,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 5 - GBP VAT invoice
# =============================================================================

def _build_fixture_05(output_path: Path) -> Path:
    gtr = gt.FIXTURE_05
    doc = b.new_document()

    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    items = [
        (li.description, li.quantity, li.unit_price, li.amount)
        for li in gtr.expected_line_items
    ]
    item_lines = b.line_item_lines(items, amount_formatter=b.format_amount_gbp)
    # VAT @ 20% shown explicitly (special requirement), £ symbol used visibly.
    totals = b.totals_lines(
        subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
        total=gtr.expected_total_amount,
        tax_label="VAT @ 20%",
        amount_formatter=b.format_amount_gbp,
    )
    footer = b.footer_lines(gtr.expected_payment_terms)

    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item_lines,
        totals=totals, footer=footer,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 6 - USD invoice with discount and freight
# =============================================================================

def _build_fixture_06(output_path: Path) -> Path:
    gtr = gt.FIXTURE_06
    doc = b.new_document()

    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    items = [
        (li.description, li.quantity, li.unit_price, li.amount)
        for li in gtr.expected_line_items
    ]
    item_lines = b.line_item_lines(items)
    # Discount and freight are NOT genuine line items - they are visibly
    # separate charge lines (special requirement), never added to
    # gtr.expected_line_items. The true source-invoice arithmetic is
    # subtotal - discount + freight + tax = total (1000 - 50 + 75 + 95 = 1120);
    # these two extra charges are simply absent from the current schema, and
    # this builder does not pretend otherwise.
    charge_lines = ["Discount: -50.00", "Freight: 75.00"]
    totals = b.totals_lines(
        subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
        total=gtr.expected_total_amount,
    )
    footer = b.footer_lines(gtr.expected_payment_terms)

    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item_lines,
        charge_lines=charge_lines, totals=totals, footer=footer,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 7 - Inclusive-tax invoice
# =============================================================================

def _build_fixture_07(output_path: Path) -> Path:
    gtr = gt.FIXTURE_07
    doc = b.new_document()

    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    items = [
        (li.description, li.quantity, li.unit_price, li.amount)
        for li in gtr.expected_line_items
    ]
    item_lines = b.line_item_lines(items)
    # Prices are visibly tax-inclusive (special requirement); tax is
    # informational only, and NO pre-tax subtotal is stated anywhere -
    # gtr.expected_subtotal is None, so totals_lines is called without a
    # subtotal argument at all.
    assert gtr.expected_subtotal is None
    extra_notes = ["All prices include tax."]
    totals = b.totals_lines(
        tax=gtr.expected_tax_amount, total=gtr.expected_total_amount,
        tax_label="Tax (informational)",
    )
    footer = b.footer_lines(gtr.expected_payment_terms)

    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item_lines,
        extra_notes=extra_notes, totals=totals, footer=footer,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 8 - Repeated table headers
# =============================================================================

def _build_fixture_08(output_path: Path) -> Path:
    gtr = gt.FIXTURE_08.clean  # the source PDF represents the CLEAN 6-item document;
    # the "header_hallucination_output" ground-truth variant describes a
    # simulated MOCKED MODEL RESPONSE for a future extraction test, not this
    # PDF's actual content.
    doc = b.new_document()

    header = b.header_lines(
        invoice_number=gtr.expected_invoice_number,
        invoice_date=gtr.expected_invoice_date,
        currency_label=gtr.expected_currency,
        seller_name=gtr.expected_seller_name,
        buyer_name=gtr.expected_buyer_name,
    )
    items = [
        (li.description, li.quantity, li.unit_price, li.amount)
        for li in gtr.expected_line_items
    ]
    assert len(items) == 6  # exactly six genuine line items (special requirement)
    item_lines = b.line_item_lines(items)
    table_header = b.table_header_line()  # identical string reused on all 3 pages

    # Page 1: header + repeated table header + items 1-2
    b.add_text_page(doc, b.compose_invoice_lines(
        header=header, table_header=table_header, item_lines=item_lines[0:2],
    ))
    # Page 2: table header repeated (continuation aid) + items 3-4
    b.add_text_page(doc, b.compose_invoice_lines(
        table_header=table_header, item_lines=item_lines[2:4],
    ))
    # Page 3: table header repeated + items 5-6 + totals + payment terms
    totals = b.totals_lines(
        subtotal=gtr.expected_subtotal, tax=gtr.expected_tax_amount,
        total=gtr.expected_total_amount,
    )
    footer = b.footer_lines(gtr.expected_payment_terms)
    b.add_text_page(doc, b.compose_invoice_lines(
        table_header=table_header, item_lines=item_lines[4:6],
        totals=totals, footer=footer,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 9 - Conflicting totals
# =============================================================================

# Fixture 9's ground truth intentionally leaves header identity (invoice
# number, seller, buyer, date) unconstrained - only the total_amount
# conflict and its resolution are under test. These are a scenario-authoring
# choice, kept IDENTICAL across both pages so total_amount is the only
# thing that differs (as the fixture requires).
_FIXTURE_09_INVOICE_NUMBER = "INV-9900"
_FIXTURE_09_DATE = "2026-10-01"
_FIXTURE_09_CURRENCY = "USD"
_FIXTURE_09_SELLER = "Anchor Point Shipping"
_FIXTURE_09_BUYER = "Riverside Distributors"


def fixture_09_page_lines() -> tuple[list[str], list[str]]:
    """The exact line content rendered onto fixture 9's two image pages,
    BEFORE rasterization. Exposed as a pure function (no PDF/image bytes
    involved) so tests can assert '500.00' / '650.00' appear on the intended
    pages without an OCR dependency - the same lines are what
    render_lines_to_png actually draws, so this is equivalent to inspecting
    the rendered image's content, not a separate/duplicated claim about it.
    """
    gtr = gt.FIXTURE_09
    items = list(gtr.shared_line_items)

    header = b.header_lines(
        invoice_number=_FIXTURE_09_INVOICE_NUMBER, invoice_date=_FIXTURE_09_DATE,
        currency_label=_FIXTURE_09_CURRENCY, seller_name=_FIXTURE_09_SELLER,
        buyer_name=_FIXTURE_09_BUYER,
    )

    # per_setting's "chosen" total is the CORRECTED (page 2) value, "650.00" -
    # this is the only total ground_truth.py stores (only the final
    # chosen/conflicting values matter for aggregation testing). The draft
    # (page 1) value, "500.00", is not stored there at all; it is derived
    # here directly from the shared line items so it stays consistent with
    # them (items[0]+items[1]+tax = 250+250+0 = 500.00).
    from decimal import Decimal
    corrected_total_value = next(
        s.expected_chosen_total_amount for s in gtr.per_setting
        if s.max_vision_pages_label == "1"
    )  # "650.00"
    draft_total_value = str(Decimal(items[0].amount) + Decimal(items[1].amount)
                             + Decimal(gtr.shared_tax_amount))  # "500.00"

    # Page 1: items 1-2, DRAFT total 500.00. Builder does not attempt to
    # resolve the conflict - it simply renders what the source pages say.
    item12_lines = b.line_item_lines([(li.description, li.quantity, li.unit_price, li.amount)
                                       for li in items[:2]])
    totals_page1 = b.totals_lines(tax=gtr.shared_tax_amount, total=draft_total_value)
    page1_lines = b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item12_lines,
        totals=totals_page1, extra_notes=["(draft total)"],
    )

    # Page 2: item 3 (continuation) + CORRECTED total 650.00
    item3_lines = b.line_item_lines([(items[2].description, items[2].quantity,
                                       items[2].unit_price, items[2].amount)])
    totals_page2 = b.totals_lines(tax=gtr.shared_tax_amount, total=corrected_total_value)
    page2_lines = b.compose_invoice_lines(
        header=header, table_header=b.table_header_line(), item_lines=item3_lines,
        totals=totals_page2, extra_notes=["(corrected total)"],
    )

    return page1_lines, page2_lines


def _build_fixture_09(output_path: Path) -> Path:
    doc = b.new_document()
    page1_lines, page2_lines = fixture_09_page_lines()
    b.add_rendered_image_page(doc, page1_lines)
    b.add_rendered_image_page(doc, page2_lines)
    return b.save_document(doc, output_path)


# =============================================================================
# Fixture 10 - Likely two-invoice PDF
# =============================================================================

# Fixture 10's ground truth pins invoice_number ("INV-A100"/"INV-B200") and
# requires seller/buyer/total_amount to ALSO differ (also_conflicting_fields)
# - the concrete seller/buyer/date/line-item values below are a scenario-
# authoring choice consistent with that requirement.

def _build_fixture_10(output_path: Path) -> Path:
    gtr = gt.FIXTURE_10
    offline = gtr.offline_expected
    doc = b.new_document()

    # Invoice A - page 1
    header_a = b.header_lines(
        invoice_number=offline.route_a_invoice_number, invoice_date="2026-11-01",
        currency_label="USD", seller_name="Vanguard Shipping Co",
        buyer_name="Oakwood Retailers",
    )
    items_a = [("Warehousing", "1", "300.00", "300.00")]
    item_lines_a = b.line_item_lines(items_a)
    totals_a = b.totals_lines(tax="0.00", total="300.00")
    b.add_text_page(doc, b.compose_invoice_lines(
        header=header_a, table_header=b.table_header_line(), item_lines=item_lines_a,
        totals=totals_a,
    ))

    # Invoice B - page 2: a COMPLETE, SEPARATE, visually distinct invoice.
    # Builder does not segment or merge these two invoices in any way - each
    # page stands alone, by design (preserves the live-benchmark blind spot).
    header_b = b.header_lines(
        invoice_number=offline.route_b_invoice_number, invoice_date="2026-11-03",
        currency_label="USD", seller_name="Different Seller Corp",
        buyer_name="Different Buyer Inc",
    )
    items_b = [("Customs clearance", "1", "450.00", "450.00")]
    item_lines_b = b.line_item_lines(items_b)
    totals_b = b.totals_lines(tax="0.00", total="450.00")
    b.add_text_page(doc, b.compose_invoice_lines(
        header=header_b, table_header=b.table_header_line(), item_lines=item_lines_b,
        totals=totals_b,
    ))

    return b.save_document(doc, output_path)


# =============================================================================
# Registry
# =============================================================================

_SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(gt.FIXTURE_01.core.fixture_id, gt.FIXTURE_01.core.filename, gt.FIXTURE_01, _build_fixture_01),
    ScenarioSpec(gt.FIXTURE_02.core.fixture_id, gt.FIXTURE_02.core.filename, gt.FIXTURE_02, _build_fixture_02),
    ScenarioSpec(gt.FIXTURE_03.core.fixture_id, gt.FIXTURE_03.core.filename, gt.FIXTURE_03, _build_fixture_03),
    ScenarioSpec(gt.FIXTURE_04.core.fixture_id, gt.FIXTURE_04.core.filename, gt.FIXTURE_04, _build_fixture_04),
    ScenarioSpec(gt.FIXTURE_05.core.fixture_id, gt.FIXTURE_05.core.filename, gt.FIXTURE_05, _build_fixture_05),
    ScenarioSpec(gt.FIXTURE_06.core.fixture_id, gt.FIXTURE_06.core.filename, gt.FIXTURE_06, _build_fixture_06),
    ScenarioSpec(gt.FIXTURE_07.core.fixture_id, gt.FIXTURE_07.core.filename, gt.FIXTURE_07, _build_fixture_07),
    ScenarioSpec(gt.FIXTURE_08.clean.core.fixture_id, gt.FIXTURE_08.clean.core.filename, gt.FIXTURE_08, _build_fixture_08),
    ScenarioSpec(gt.FIXTURE_09.core.fixture_id, gt.FIXTURE_09.core.filename, gt.FIXTURE_09, _build_fixture_09),
    ScenarioSpec(gt.FIXTURE_10.core.fixture_id, gt.FIXTURE_10.core.filename, gt.FIXTURE_10, _build_fixture_10),
)


def _check_registry_integrity(specs: tuple[ScenarioSpec, ...]) -> None:
    ids = [s.fixture_id for s in specs]
    dupe_ids = {i for i in ids if ids.count(i) > 1}
    if dupe_ids:
        raise ValueError(f"duplicate fixture_id(s) in scenario registry: {sorted(dupe_ids)}")

    names = [s.filename for s in specs]
    dupe_names = {n for n in names if names.count(n) > 1}
    if dupe_names:
        raise ValueError(f"duplicate filename(s) in scenario registry: {sorted(dupe_names)}")


_check_registry_integrity(_SPECS)  # fail fast at import time; no files written


def list_scenarios() -> tuple[ScenarioSpec, ...]:
    """All ten registered scenarios, in fixture order."""
    return _SPECS


def get_scenario(fixture_id: str) -> ScenarioSpec:
    for spec in _SPECS:
        if spec.fixture_id == fixture_id:
            return spec
    known = ", ".join(s.fixture_id for s in _SPECS)
    raise KeyError(f"unknown fixture_id {fixture_id!r}; known fixture IDs: {known}")


def build_scenario(fixture_id: str, output_path: str | Path) -> Path:
    """Build exactly one fixture's PDF at output_path. Does not touch any
    other fixture's output."""
    spec = get_scenario(fixture_id)
    return spec.builder(Path(output_path))


def build_all_scenarios(output_dir: str | Path) -> list[Path]:
    """Build all ten fixtures' PDFs into output_dir, one file per fixture,
    using each spec's own filename."""
    output_dir = Path(output_dir)
    return [build_scenario(spec.fixture_id, output_dir / spec.filename) for spec in _SPECS]
