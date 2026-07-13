"""Hand-authored ground truth for the synthetic invoice validation pack.

MILESTONE 1 SCOPE ONLY: this module defines expected-result records for ten
synthetic invoice scenarios. It does NOT generate PDFs, does NOT call
Gemini/Claude, and does NOT import invoice_extractor at all - not the
pipeline, not the schema, not pdf_utils. Every string constant below
("text-native", "gemini", "conflict in ...", etc.) was verified against the
actual source of invoice_extractor/{pdf_utils,schema,aggregation,pipeline,
excel_export}.py on 2026-07-13 (see the accompanying milestone report for the
full inspection), then typed in here BY HAND. None of these values were
produced by calling normalize_invoice(), validate_invoice(), aggregate(), or
process_file() - doing so would make the ground truth circular (the thing
under test would also be the thing defining what "correct" means).

Frozen dataclasses are used instead of Pydantic deliberately: a Pydantic
model would coerce/validate field values on construction, which could
silently "fix" an authoring mistake in this file. A frozen dataclass stores
exactly what was typed, so any inconsistency is caught by the separate
self-validation tests in test_synthetic_ground_truth.py, not hidden by the
ground-truth type itself.

Monetary and quantity values are always Decimal-compatible strings (e.g.
"1234.56"), never float/int/Decimal - see test_synthetic_ground_truth.py's
`test_no_money_or_quantity_value_is_a_float`.
"""

from dataclasses import dataclass
from typing import Literal

# --- Vocabulary -------------------------------------------------------------
# These Literal aliases intentionally DUPLICATE the string values used by
# invoice_extractor (pdf_utils.PAGE_TEXT etc.) rather than importing them, to
# keep this package free of any dependency on the runtime application (see
# module docstring and the package __init__.py). Spelling drift is guarded by
# the self-validation tests plus the Step-1 source inspection recorded above,
# not by a live import.

PageKind = Literal["text", "image", "blank"]
DocumentClassification = Literal["text-native", "image-only", "mixed", "error"]
ExtractionRoute = Literal["text", "vision"]
Provider = Literal["gemini", "claude", "mixed", "none"]

# Review-reason category vocabulary, restricted to substrings the running
# application actually emits today (schema.py:validate_invoice,
# aggregation.py:aggregate, pipeline.py:process_file). "none" means no review
# reason is expected at all.
ReviewCategory = Literal[
    "missing_required_fields",
    "no_line_items",
    "line_items_no_amounts",
    "totals_inconclusive",
    "conflict",
    "possible_multiple_invoices",
    "partial_extraction",
    "none",
]

# Benchmark-only interpretive label. THE RUNTIME NEVER STORES THIS FIELD - it
# only ever stores needs_review (bool) + review_reason (str | None). This
# label is metadata this ground-truth pack adds on top, for readability:
#   "reconciled"   -> validate_invoice would return no arithmetic-related
#                     reason (one of the three documented rules passes).
#   "inconclusive" -> validate_invoice's exact "totals inconclusive: ..."
#                     substring would be emitted (none of the three rules
#                     pass, and the schema has no discount/shipping/duties
#                     field that could explain the gap).
#   "mismatch"     -> NOT CURRENTLY DISTINGUISHABLE by any application code
#                     path. schema.py only ever produces "reconciled" (no
#                     reason) or "inconclusive" (the fixed substring above) -
#                     it never labels anything a definitive "mismatch". This
#                     value is reserved for a future/manual interpretation
#                     and is not used by any of the ten fixtures below.
ArithmeticBenchmarkLabel = Literal["reconciled", "inconclusive", "mismatch"]


# --- Shared building blocks --------------------------------------------------

@dataclass(frozen=True)
class ExpectedLineItem:
    """A line item expected to SURVIVE normalization and appear in the final
    output. All four fields are concrete (never null) - this type is not used
    to describe corrupted/partial model output; see ExpectedRawModelLineItem
    for that."""
    description: str
    quantity: str  # Decimal-compatible string, e.g. "1"
    unit_price: str
    amount: str


@dataclass(frozen=True)
class ExpectedRawModelLineItem:
    """A raw (unfiltered) row exactly as a mocked model response would carry
    it, BEFORE normalize_invoice's per-item null filter runs. Fields may be
    None here, unlike ExpectedLineItem. Used only by fixture 8's corrupted
    variant to describe the input to that filter."""
    description: str | None
    quantity: str | None
    unit_price: str | None
    amount: str | None


@dataclass(frozen=True)
class ExpectedPageLayout:
    page_number: int
    page_kind: PageKind
    business_content_summary: str


@dataclass(frozen=True)
class ExpectedVisionChunkPlan:
    max_pages_per_request: int
    chunks: tuple[tuple[int, ...], ...]  # ordered chunks of 1-based page numbers


@dataclass(frozen=True)
class ExpectedConflict:
    field: str
    category: Literal["conflict", "possible_multiple_invoices"]


@dataclass(frozen=True)
class ExpectedScenarioCore:
    """Structural facts shared by every fixture: document identity, page
    layout, and routing/chunking. Deliberately excludes arithmetic/review
    expectations, since fixtures 9 and 10 do not have a single fixed answer
    for those (they vary by MAX_VISION_PAGES setting, or by offline-vs-live
    framing, respectively) - see ExpectedConflictingTotalsScenario and
    ExpectedMultiInvoiceScenario below."""
    fixture_id: str
    filename: str
    scenario: str
    page_layout: tuple[ExpectedPageLayout, ...]
    expected_document_classification: DocumentClassification
    expected_text_pages: tuple[int, ...]
    expected_image_pages: tuple[int, ...]
    expected_blank_pages: tuple[int, ...]
    expected_chunk_plans: tuple[ExpectedVisionChunkPlan, ...]
    expected_extraction_routes: tuple[ExtractionRoute, ...]
    # Correction E: only ever included because these counts are deterministic
    # from routing/chunking configuration alone. NEVER includes provider
    # fallback calls, since fallback depends on runtime provider success or
    # failure, which this static ground truth cannot know in advance.
    expected_text_route_calls: int
    expected_vision_route_calls_by_chunk_limit: dict[int, int]  # {max_vision_pages: call_count}


@dataclass(frozen=True)
class ExpectedInvoiceScenario:
    """Full expectation for a 'standard' fixture (1-7): the structural core
    plus concrete header/line-item/arithmetic/review expectations that hold
    unconditionally (no per-setting or offline/live split needed)."""
    core: ExpectedScenarioCore

    expected_invoice_number: str | None
    expected_invoice_date: str | None
    expected_currency: str | None
    expected_seller_name: str | None
    expected_buyer_name: str | None
    expected_subtotal: str | None
    expected_subtotal_equals_line_sum: bool  # only meaningful when subtotal is not None
    expected_tax_amount: str | None
    expected_total_amount: str
    expected_payment_terms: str | None

    expected_line_items: tuple[ExpectedLineItem, ...]

    # Correction D: a category the app's review_reason substring maps to,
    # not a claim that the app exports a dedicated "validation category"
    # field. See ReviewCategory docstring above.
    expected_validation_category: ReviewCategory
    expected_review_reason_contains: tuple[str, ...]  # substrings that must ALL appear
    expected_arithmetic_status_benchmark_label: ArithmeticBenchmarkLabel

    expected_needs_review: bool
    expected_conflicts: tuple[ExpectedConflict, ...]

    offline_guarantees: tuple[str, ...]
    live_only_uncertainties: tuple[str, ...]
    notes: str = ""


# --- Fixture 8: repeated table headers --------------------------------------

@dataclass(frozen=True)
class ExpectedHeaderHallucinationVariant:
    """One simulated model-output variant for fixture 8."""
    variant_name: Literal["clean_output", "header_hallucination_output"]
    raw_model_line_items: tuple[ExpectedRawModelLineItem, ...]
    survives_normalization: bool  # per the CURRENT normalize_invoice any()-filter
    resulting_line_item_count: int
    expected_arithmetic_status_benchmark_label: ArithmeticBenchmarkLabel
    expected_needs_review: bool
    known_gap: str  # non-empty only when this variant exposes an application gap


@dataclass(frozen=True)
class ExpectedHeaderHallucinationScenario:
    clean: ExpectedInvoiceScenario  # the "clean_output" variant is a complete, valid scenario
    corrupted: ExpectedHeaderHallucinationVariant  # simulated bad-model-output case


# --- Fixture 9: conflicting totals -------------------------------------------

@dataclass(frozen=True)
class ExpectedChunkSettingOutcome:
    """Fixture 9's expected behavior differs by MAX_VISION_PAGES setting."""
    max_vision_pages_label: str  # e.g. "1" or "2_or_5" (both settings behave identically)
    chunk_plan: ExpectedVisionChunkPlan
    cross_chunk_conflict_possible: bool
    deterministic: bool  # False => real model behavior decides; not asserted here
    expected_needs_review: bool | None  # None when deterministic=False
    expected_review_reason_contains: tuple[str, ...]
    expected_chosen_total_amount: str | None  # None when not deterministic
    expected_arithmetic_status_benchmark_label: ArithmeticBenchmarkLabel | None
    notes: str


@dataclass(frozen=True)
class ExpectedConflictingTotalsScenario:
    core: ExpectedScenarioCore
    shared_line_items: tuple[ExpectedLineItem, ...]  # identical regardless of chunk setting
    shared_tax_amount: str | None
    per_setting: tuple[ExpectedChunkSettingOutcome, ...]  # exactly 2 entries


# --- Fixture 10: likely two-invoice PDF --------------------------------------

@dataclass(frozen=True)
class ExpectedMultiInvoiceOfflineAggregation:
    """Constructed conceptually from two independently-built RouteResults -
    a deliberate simplification to isolate aggregation.py's conflict-
    detection logic. Does NOT reflect how the real pipeline would actually
    process this exact two-text-page PDF (see ExpectedMultiInvoiceLiveBenchmark)."""
    route_a_invoice_number: str
    route_b_invoice_number: str
    also_conflicting_fields: tuple[str, ...]  # other fields that also differ
    expected_output_invoice_count: int  # always 1: one-invoice-per-PDF architecture
    expected_needs_review: bool
    expected_review_reason_contains: tuple[str, ...]
    expected_conflicts: tuple[ExpectedConflict, ...]
    is_realistic_pipeline_path: bool = False  # see docstring: this is NOT what really happens


@dataclass(frozen=True)
class ExpectedMultiInvoiceLiveOutcome:
    description: str
    plausible: bool


@dataclass(frozen=True)
class ExpectedMultiInvoiceLiveBenchmark:
    """The REALISTIC full-pipeline path: both text pages are concatenated
    into ONE Gemini text call, producing exactly one RouteResult. Because
    aggregation's conflict detection only compares MULTIPLE RouteResults,
    it never runs at all in this realistic case - the blind spot is that the
    single model response's behavior (and therefore needs_review) is
    genuinely unknown until measured live."""
    live_expected_outcome_set: tuple[ExpectedMultiInvoiceLiveOutcome, ...]
    needs_review_guaranteed: bool  # must be False per Correction C
    aggregation_conflict_detection_reachable: bool  # False: only one RouteResult exists
    notes: str


@dataclass(frozen=True)
class ExpectedMultiInvoiceScenario:
    core: ExpectedScenarioCore
    offline_expected: ExpectedMultiInvoiceOfflineAggregation
    live_expected: ExpectedMultiInvoiceLiveBenchmark


# =============================================================================
# Fixture 1 - Multi-page text-native invoice
# =============================================================================

FIXTURE_01 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_01_multipage_text_native",
        filename="multipage_text_native.pdf",
        scenario="Ordinary 3-page text-native freight invoice, two line items per page.",
        page_layout=(
            ExpectedPageLayout(1, "text", "Header block + items 1-2"),
            ExpectedPageLayout(2, "text", "Items 3-4"),
            ExpectedPageLayout(3, "text", "Items 5-6 + totals block"),
        ),
        expected_document_classification="text-native",
        expected_text_pages=(1, 2, 3),
        expected_image_pages=(),
        expected_blank_pages=(),
        expected_chunk_plans=(),
        expected_extraction_routes=("text",),
        expected_text_route_calls=1,
        expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
    ),
    expected_invoice_number="INV-3001",
    expected_invoice_date="2026-03-01",
    expected_currency="USD",
    expected_seller_name="Northbridge Freight Inc",
    expected_buyer_name="Delta Retail Co",
    expected_subtotal="600.00",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="48.00",
    expected_total_amount="648.00",
    expected_payment_terms="Net 30",
    expected_line_items=(
        ExpectedLineItem("Pallet handling", "1", "100.00", "100.00"),
        ExpectedLineItem("Warehouse storage", "1", "100.00", "100.00"),
        ExpectedLineItem("Local delivery", "1", "100.00", "100.00"),
        ExpectedLineItem("Fuel surcharge", "1", "100.00", "100.00"),
        ExpectedLineItem("Documentation fee", "1", "100.00", "100.00"),
        ExpectedLineItem("Insurance", "1", "100.00", "100.00"),
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "Multi-page text concatenation with '--- PAGE n ---' markers reaches a "
        "single Gemini text call.",
        "Line-item order is preserved across all 3 pages within that one route.",
    ),
    live_only_uncertainties=(
        "Whether a real model reliably reads all 6 items from a 3-page "
        "concatenated text block without dropping or reordering the "
        "'middle' page (page 2) - an offline mock cannot reveal a real "
        "attention/context-length quirk.",
    ),
)


# =============================================================================
# Fixture 2 - Multi-page scanned invoice exceeding MAX_VISION_PAGES
# =============================================================================

FIXTURE_02 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_02_multipage_scanned_exceeds_limit",
        filename="multipage_scanned_exceeds_limit.pdf",
        scenario="7-page scanned invoice, one line item per page, no failures.",
        page_layout=tuple(
            ExpectedPageLayout(n, "image", f"Scanned page with item {n}") for n in range(1, 8)
        ),
        expected_document_classification="image-only",
        expected_text_pages=(),
        expected_image_pages=(1, 2, 3, 4, 5, 6, 7),
        expected_blank_pages=(),
        expected_chunk_plans=(
            ExpectedVisionChunkPlan(1, ((1,), (2,), (3,), (4,), (5,), (6,), (7,))),
            ExpectedVisionChunkPlan(2, ((1, 2), (3, 4), (5, 6), (7,))),
            ExpectedVisionChunkPlan(5, ((1, 2, 3, 4, 5), (6, 7))),
        ),
        expected_extraction_routes=("vision",),
        expected_text_route_calls=0,
        expected_vision_route_calls_by_chunk_limit={1: 7, 2: 4, 5: 2},
    ),
    expected_invoice_number="INV-7007",
    expected_invoice_date="2026-04-15",
    expected_currency="USD",
    expected_seller_name="Pacific Container Lines",
    expected_buyer_name="Summit Imports LLC",
    expected_subtotal="700.00",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="0.00",
    expected_total_amount="700.00",
    expected_payment_terms="Due on receipt",
    expected_line_items=tuple(
        ExpectedLineItem(f"Container handling page {n}", "1", "100.00", "100.00")
        for n in range(1, 8)
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "Chunk sizing/ordering at MAX_VISION_PAGES=2 (4 chunks) and =5 (2 chunks) "
        "is pure Python logic - fully provable offline.",
        "No page is omitted or duplicated across chunk boundaries.",
    ),
    live_only_uncertainties=(
        "Whether real per-chunk latency/rate-limiting behaves acceptably across "
        "up to 4 sequential requests.",
        "Whether the model's line-item wording is consistent enough across chunk "
        "boundaries for a fuzzy live-benchmark matcher to align items 1:1.",
    ),
)


# =============================================================================
# Fixture 3 - Mixed text-native, scanned, and blank document
# =============================================================================

FIXTURE_03 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_03_mixed_text_scan_blank",
        filename="mixed_text_scan_blank.pdf",
        scenario=(
            "5-page document: text-native cover/summary page, blank separator, "
            "two scanned attachment pages, text-native footer with totals."
        ),
        page_layout=(
            ExpectedPageLayout(1, "text", "Cover/summary: header + item 1"),
            ExpectedPageLayout(2, "blank", "Blank separator page"),
            ExpectedPageLayout(3, "image", "Scanned attachment item A"),
            ExpectedPageLayout(4, "image", "Scanned attachment item B"),
            ExpectedPageLayout(5, "text", "Footer: totals + payment terms, no new items"),
        ),
        expected_document_classification="mixed",
        expected_text_pages=(1, 5),
        expected_image_pages=(3, 4),
        expected_blank_pages=(2,),
        expected_chunk_plans=(
            ExpectedVisionChunkPlan(2, ((3, 4),)),
            ExpectedVisionChunkPlan(5, ((3, 4),)),
        ),
        expected_extraction_routes=("text", "vision"),
        expected_text_route_calls=1,
        expected_vision_route_calls_by_chunk_limit={1: 2, 2: 1, 5: 1},
    ),
    expected_invoice_number="INV-5005",
    expected_invoice_date="2026-05-20",
    expected_currency="USD",
    expected_seller_name="Harborline Logistics",
    expected_buyer_name="Crestview Wholesale",
    expected_subtotal="300.00",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="24.00",
    expected_total_amount="324.00",
    expected_payment_terms="Net 15",
    expected_line_items=(
        ExpectedLineItem("Cover page service fee", "1", "100.00", "100.00"),
        ExpectedLineItem("Scanned attachment item A", "1", "100.00", "100.00"),
        ExpectedLineItem("Scanned attachment item B", "1", "100.00", "100.00"),
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "Independent per-page routing on one document with all three page "
        "kinds simultaneously.",
        "Blank page correctly excluded from both routes and recorded.",
        "Cross-route aggregation ordering by first-contributing-page (text "
        "route, pages 1+5, is ordered before vision route, pages 3-4).",
    ),
    live_only_uncertainties=(
        "Whether a real vision call (which only sees pages 3-4) correctly "
        "avoids fabricating a different header when it cannot see one - the "
        "text route (pages 1+5) is designed to carry the full header, but "
        "live testing is the only way to confirm the vision response doesn't "
        "also invent conflicting header values.",
    ),
)


# =============================================================================
# Fixture 4 - EUR European-number-format invoice
# =============================================================================

FIXTURE_04 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_04_eur_european_number_format",
        filename="eur_european_number_format.pdf",
        scenario=(
            "German vendor invoice using European decimal-comma/thousands-dot "
            "formatting throughout (e.g. '1.234,56')."
        ),
        page_layout=(
            ExpectedPageLayout(1, "text", "Header + 2 items + totals, EU number format"),
        ),
        expected_document_classification="text-native",
        expected_text_pages=(1,),
        expected_image_pages=(),
        expected_blank_pages=(),
        expected_chunk_plans=(),
        expected_extraction_routes=("text",),
        expected_text_route_calls=1,
        expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
    ),
    expected_invoice_number="RE-2026-0042",
    expected_invoice_date="2026-02-10",
    expected_currency="EUR",
    expected_seller_name="Rheinmetall Spedition GmbH",
    expected_buyer_name="Alpine Distribution AG",
    expected_subtotal="1234.56",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="234.57",
    expected_total_amount="1469.13",
    expected_payment_terms="Zahlbar innerhalb 30 Tagen",
    expected_line_items=(
        ExpectedLineItem("Seefracht Hamburg-Rotterdam", "1", "1000.00", "1000.00"),
        ExpectedLineItem("Zollabfertigung", "1", "234.56", "234.56"),
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "coerce_decimal's European-format branch ('1.234,56' -> "
        "Decimal('1234.56')) is exercised through the full process_file path, "
        "not just the isolated unit function.",
    ),
    live_only_uncertainties=(
        "Whether a real Gemini/Claude call, shown German-formatted numbers in "
        "the source text, reliably converts them to a plain JSON number (per "
        "the prompt's 'no thousands separators' rule) rather than passing "
        "'1.234,56' through verbatim or misreading it as 1.234 (truncating "
        "the decimal). The offline mock controls the model's JSON directly, "
        "so it can only prove the pipeline handles whatever the model "
        "actually returns - not that the model interprets the ambiguous "
        "formatting correctly in the first place.",
    ),
)


# =============================================================================
# Fixture 5 - GBP VAT invoice
# =============================================================================

FIXTURE_05 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_05_gbp_vat_invoice",
        filename="gbp_vat_invoice.pdf",
        scenario="UK vendor invoice with GBP amounts (£ symbol) and a VAT @ 20% line.",
        page_layout=(
            ExpectedPageLayout(1, "text", "Header (£ symbol) + 2 items + VAT @ 20% + total"),
        ),
        expected_document_classification="text-native",
        expected_text_pages=(1,),
        expected_image_pages=(),
        expected_blank_pages=(),
        expected_chunk_plans=(),
        expected_extraction_routes=("text",),
        expected_text_route_calls=1,
        expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
    ),
    expected_invoice_number="UK-INV-2211",
    expected_invoice_date="2026-06-05",
    expected_currency="GBP",
    expected_seller_name="Thames Logistics Ltd",
    expected_buyer_name="Kensington Retail Group",
    expected_subtotal="500.00",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="100.00",
    expected_total_amount="600.00",
    expected_payment_terms="Net 30",
    expected_line_items=(
        ExpectedLineItem("Road freight London-Manchester", "1", "350.00", "350.00"),
        ExpectedLineItem("Packaging materials", "1", "150.00", "150.00"),
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "normalize_currency's unambiguous-symbol mapping ('£' -> 'GBP') is "
        "reached end-to-end when the mocked model echoes the symbol into the "
        "currency field.",
        "'VAT' terminology maps onto the generic tax_amount field with no "
        "special-casing needed in the prompt or schema.",
    ),
    live_only_uncertainties=(
        "Whether a real model asked to extract currency from a £-prefixed "
        "invoice with no explicit 'GBP' text reliably infers GBP rather than "
        "leaving it null or guessing a different currency - a prompt-"
        "following question, not a coercion question (coercion only applies "
        "if the model itself echoes the symbol into the currency field).",
    ),
)


# =============================================================================
# Fixture 6 - USD invoice with discount and freight
# =============================================================================

FIXTURE_06 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_06_usd_discount_freight",
        filename="usd_discount_freight.pdf",
        scenario=(
            "US commercial invoice with a discount line and a freight charge, "
            "both outside the current schema's line-item/tax model."
        ),
        page_layout=(
            ExpectedPageLayout(
                1, "text",
                "Header + 2 items + subtotal/discount/freight/tax/total block",
            ),
        ),
        expected_document_classification="text-native",
        expected_text_pages=(1,),
        expected_image_pages=(),
        expected_blank_pages=(),
        expected_chunk_plans=(),
        expected_extraction_routes=("text",),
        expected_text_route_calls=1,
        expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
    ),
    expected_invoice_number="US-8800",
    expected_invoice_date="2026-07-01",
    expected_currency="USD",
    expected_seller_name="Continental Cargo Corp",
    expected_buyer_name="Lakeside Manufacturing",
    expected_subtotal="1000.00",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="95.00",
    expected_total_amount="1120.00",
    expected_payment_terms="Net 45",
    expected_line_items=(
        ExpectedLineItem("Freight brokerage services", "1", "600.00", "600.00"),
        ExpectedLineItem("Handling and dispatch", "1", "400.00", "400.00"),
    ),
    # Source invoice's true arithmetic: 1000.00 (subtotal) - 50.00 (discount)
    # + 75.00 (freight) + 95.00 (tax) = 1120.00 (total). The schema has no
    # `discount`/`freight` fields (HEADER_FIELDS in schema.py), so the
    # extractor only ever sees subtotal/tax/total/line_items - discount and
    # freight are simply absent from the structured output, not exported
    # elsewhere. This fixture does not pretend otherwise. A future schema
    # extension (explicit discount_amount/freight_amount fields, folded into
    # the reconciliation formula) would let this fixture reconcile cleanly -
    # that extension is NOT proposed for implementation in this milestone.
    expected_validation_category="totals_inconclusive",
    expected_review_reason_contains=("totals inconclusive", "discount/shipping/duties/rounding"),
    expected_arithmetic_status_benchmark_label="inconclusive",
    expected_needs_review=True,
    expected_conflicts=(),
    offline_guarantees=(
        "The inconclusive-not-wrong distinction from validate_invoice is "
        "proven with a concrete, realistic invoice shape (discount + "
        "freight) rather than an arbitrary mismatched number: rule 1 "
        "(sum+tax=1095.00 vs total=1120.00, diff 25.00 > tolerance 5.60) "
        "fails, rule 2 (sum=1000.00 vs total=1120.00) fails, rule 3's first "
        "leg (subtotal+tax=1095.00 vs total=1120.00) fails - so all three "
        "documented rules fail and the result is 'totals inconclusive', "
        "never a hard error.",
    ),
    live_only_uncertainties=(
        "Whether a real model faced with a discount/freight invoice correctly "
        "OMITS discount/freight from line_items (per the prompt rule "
        "excluding non-item rows) rather than inventing a 'Discount' or "
        "'Freight' line item. If it does invent one, sum(line_items) would "
        "change and the arithmetic status could shift away from "
        "inconclusive - the offline mock cannot reveal this since it "
        "controls the JSON directly.",
    ),
)


# =============================================================================
# Fixture 7 - Inclusive-tax invoice
# =============================================================================

FIXTURE_07 = ExpectedInvoiceScenario(
    core=ExpectedScenarioCore(
        fixture_id="fixture_07_inclusive_tax_invoice",
        filename="inclusive_tax_invoice.pdf",
        scenario=(
            "Vendor whose line-item prices are quoted tax-inclusive, with tax "
            "broken out informationally and no separate pre-tax subtotal."
        ),
        page_layout=(
            ExpectedPageLayout(1, "text", "Header + 2 tax-inclusive items + informational tax line"),
        ),
        expected_document_classification="text-native",
        expected_text_pages=(1,),
        expected_image_pages=(),
        expected_blank_pages=(),
        expected_chunk_plans=(),
        expected_extraction_routes=("text",),
        expected_text_route_calls=1,
        expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
    ),
    expected_invoice_number="INC-3300",
    expected_invoice_date="2026-08-12",
    expected_currency="EUR",
    expected_seller_name="Nordic Freight Solutions",
    expected_buyer_name="Baltic Traders OU",
    expected_subtotal=None,  # genuinely absent on this invoice style; not invented
    expected_subtotal_equals_line_sum=False,  # N/A: subtotal is None
    expected_tax_amount="38.02",  # informational only; already embedded in amounts
    expected_total_amount="228.10",
    expected_payment_terms="Net 20",
    expected_line_items=(
        ExpectedLineItem("Sea freight, tax incl.", "1", "150.00", "150.00"),
        ExpectedLineItem("Port handling, tax incl.", "1", "78.10", "78.10"),
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "Isolates rule 2 (sum(line_items) ~= total, inclusive/zero tax) as "
        "the PASSING rule: rule 1 would fail here (228.10 + 38.02 != "
        "228.10), which is exactly why rule 2 exists as a fallback.",
    ),
    live_only_uncertainties=(
        "Whether a real model correctly leaves subtotal null (rather than "
        "guessing a pre-tax figure) when the source document genuinely "
        "doesn't state one, and whether it reports tax_amount as the "
        "informational figure even though nothing is subtracted from it - "
        "both are prompt-following questions about restraint, not "
        "pipeline-logic questions.",
    ),
)


# =============================================================================
# Fixture 8 - Repeated table headers
# =============================================================================

_FIXTURE_08_CORE = ExpectedScenarioCore(
    fixture_id="fixture_08_repeated_table_headers",
    filename="repeated_table_headers.pdf",
    scenario=(
        "3-page text-native invoice where the line-item table's column header "
        "row is reprinted at the top of every page as a continuation aid."
    ),
    page_layout=(
        ExpectedPageLayout(1, "text", "Invoice header + column-header row + items 1-2"),
        ExpectedPageLayout(2, "text", "Column-header row (repeated) + items 3-4"),
        ExpectedPageLayout(3, "text", "Column-header row (repeated) + items 5-6 + totals"),
    ),
    expected_document_classification="text-native",
    expected_text_pages=(1, 2, 3),
    expected_image_pages=(),
    expected_blank_pages=(),
    expected_chunk_plans=(),
    expected_extraction_routes=("text",),
    expected_text_route_calls=1,
    expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
)

_FIXTURE_08_CLEAN = ExpectedInvoiceScenario(
    core=_FIXTURE_08_CORE,
    expected_invoice_number="REP-4400",
    expected_invoice_date="2026-09-09",
    expected_currency="USD",
    expected_seller_name="Statewide Trucking LLC",
    expected_buyer_name="Metro Grocers Inc",
    expected_subtotal="600.00",
    expected_subtotal_equals_line_sum=True,
    expected_tax_amount="0.00",
    expected_total_amount="600.00",
    expected_payment_terms="Net 30",
    expected_line_items=(
        ExpectedLineItem("Route A delivery", "1", "100.00", "100.00"),
        ExpectedLineItem("Route B delivery", "1", "100.00", "100.00"),
        ExpectedLineItem("Route C delivery", "1", "100.00", "100.00"),
        ExpectedLineItem("Route D delivery", "1", "100.00", "100.00"),
        ExpectedLineItem("Route E delivery", "1", "100.00", "100.00"),
        ExpectedLineItem("Route F delivery", "1", "100.00", "100.00"),
    ),
    expected_validation_category="none",
    expected_review_reason_contains=(),
    expected_arithmetic_status_benchmark_label="reconciled",
    expected_needs_review=False,
    expected_conflicts=(),
    offline_guarantees=(
        "Proves the pipeline does not ITSELF introduce header-row duplicates: "
        "the mocked model response simply never includes the header text as "
        "a line-item entry (6 items in, 6 items out).",
    ),
    live_only_uncertainties=(
        "This offline-clean variant says nothing about whether a real model "
        "would actually avoid hallucinating the repeated header as a row in "
        "the first place - see the header_hallucination_output variant and "
        "its known_gap below.",
    ),
    notes=(
        "This is the 'clean_output' variant of a two-variant fixture. See "
        "the sibling 'header_hallucination_output' variant "
        "(FIXTURE_08.corrupted) for the simulated-bad-model-output case."
    ),
)

# Simulated bad-model-output case: the mocked response DELIBERATELY includes
# 3 spurious header-row-shaped items (only `description` set, all numeric
# fields null) mixed in with the 6 real ones, as if the model had
# hallucinated the repeated column header as a line item on each page.
#
# CURRENT APPLICATION BEHAVIOR (verified against schema.py:normalize_invoice
# on 2026-07-13, NOT assumed): the per-item filter is
#     if any(v is not None for v in fields.values()):
#         items.append(LineItem(**fields))
# A row with description="Description" and quantity/unit_price/amount all
# None has `any(...)` = True (description alone is non-null), so THIS ROW
# SURVIVES the filter. It is not dropped.
#
# Consequence: normalize_invoice returns 9 line items (6 real + 3 spurious),
# not 6. Because each spurious row's amount is None, it is excluded from
# validate_invoice's `amounts = [it.amount for it in items if it.amount is
# not None]` list, so sum(line_items.amount) is still exactly 600.00 (only
# the 6 real amounts contribute) and rule 2 (sum ~= total, zero tax) still
# passes. No arithmetic reason is triggered, and validate_invoice has no
# separate "line-item count" sanity check - so needs_review stays False.
#
# This is also true for aggregation: fixture 8 is a SINGLE text route (all 3
# pages concatenated into one Gemini call), so aggregate() takes the
# `len(routes) == 1` early-return branch and applies NO filtering at all
# beyond what normalize_invoice already did - aggregation's dedup logic
# (which requires all four fields non-null to even consider two items
# "the same") never gets a chance to remove these spurious rows either,
# since it only compares items ACROSS separate RouteResults, not within one.
#
# NET RESULT: a corrupted extraction with 3 hallucinated header rows is
# currently indistinguishable, by any existing review trigger, from a clean
# one. This is a genuine, currently-unaddressed application gap. It is
# documented here, NOT fixed in this milestone.
_FIXTURE_08_CORRUPTED = ExpectedHeaderHallucinationVariant(
    variant_name="header_hallucination_output",
    raw_model_line_items=(
        ExpectedRawModelLineItem("Route A delivery", "1", "100.00", "100.00"),
        ExpectedRawModelLineItem("Description", None, None, None),  # hallucinated header row
        ExpectedRawModelLineItem("Route B delivery", "1", "100.00", "100.00"),
        ExpectedRawModelLineItem("Route C delivery", "1", "100.00", "100.00"),
        ExpectedRawModelLineItem("Description", None, None, None),  # hallucinated header row
        ExpectedRawModelLineItem("Route D delivery", "1", "100.00", "100.00"),
        ExpectedRawModelLineItem("Route E delivery", "1", "100.00", "100.00"),
        ExpectedRawModelLineItem("Description", None, None, None),  # hallucinated header row
        ExpectedRawModelLineItem("Route F delivery", "1", "100.00", "100.00"),
    ),
    survives_normalization=True,
    resulting_line_item_count=9,
    expected_arithmetic_status_benchmark_label="reconciled",  # sum of non-null amounts is unaffected
    expected_needs_review=False,  # <- the gap: no review trigger despite 3 spurious rows
    known_gap=(
        "normalize_invoice() keeps any row where at least one field is "
        "non-null. A header-shaped row with only `description` set survives "
        "and inflates line_item_count from 6 to 9 with zero effect on "
        "validate_invoice's arithmetic (its amount is null, so it is "
        "excluded from the sum) and zero effect on needs_review (there is "
        "no line-item-count sanity check). A corrupted extraction can "
        "therefore pass through completely undetected by every current "
        "review trigger."
    ),
)

FIXTURE_08 = ExpectedHeaderHallucinationScenario(
    clean=_FIXTURE_08_CLEAN,
    corrupted=_FIXTURE_08_CORRUPTED,
)


# =============================================================================
# Fixture 9 - Conflicting totals
# =============================================================================

_FIXTURE_09_CORE = ExpectedScenarioCore(
    fixture_id="fixture_09_conflicting_totals",
    filename="conflicting_totals.pdf",
    scenario=(
        "2-page scanned invoice where a correction was made between pages: "
        "page 1 shows a draft total, page 2 shows a corrected total."
    ),
    page_layout=(
        ExpectedPageLayout(1, "image", "Header + items 1-2, draft 'Total: 500.00'"),
        ExpectedPageLayout(2, "image", "Continuation: item 3, corrected 'Total: 650.00'"),
    ),
    expected_document_classification="image-only",
    expected_text_pages=(),
    expected_image_pages=(1, 2),
    expected_blank_pages=(),
    # The max=1 plan is the one that makes the conflict observable at all;
    # see per_setting below for why max=2/5 behave completely differently.
    expected_chunk_plans=(
        ExpectedVisionChunkPlan(1, ((1,), (2,))),
        ExpectedVisionChunkPlan(2, ((1, 2),)),
        ExpectedVisionChunkPlan(5, ((1, 2),)),
    ),
    expected_extraction_routes=("vision",),
    expected_text_route_calls=0,
    expected_vision_route_calls_by_chunk_limit={1: 2, 2: 1, 5: 1},
)

# Non-conflicting fields, identical across both pages/chunks by design (only
# total_amount is engineered to differ - see per_setting below).
_FIXTURE_09_LINE_ITEMS = (
    ExpectedLineItem("Terminal handling", "1", "250.00", "250.00"),
    ExpectedLineItem("Demurrage", "1", "250.00", "250.00"),
    ExpectedLineItem("Late correction fee", "1", "150.00", "150.00"),
)

FIXTURE_09 = ExpectedConflictingTotalsScenario(
    core=_FIXTURE_09_CORE,
    shared_line_items=_FIXTURE_09_LINE_ITEMS,
    shared_tax_amount="0.00",
    per_setting=(
        # MAX_VISION_PAGES=1: two SEPARATE RouteResults (chunk [1], chunk
        # [2]) are produced, so aggregate()'s cross-chunk conflict detection
        # DOES run and DOES find total_amount differs (500.00 vs 650.00).
        # Per aggregation.py's documented rule, monetary fields take the
        # value from the route containing the globally-last page (chunk
        # [2], page 2 -> "650.00"), and the conflict is ALWAYS flagged
        # regardless of which value is chosen.
        ExpectedChunkSettingOutcome(
            max_vision_pages_label="1",
            chunk_plan=ExpectedVisionChunkPlan(1, ((1,), (2,))),
            cross_chunk_conflict_possible=True,
            deterministic=True,
            expected_needs_review=True,
            expected_review_reason_contains=("conflict in total_amount", "kept '650.00'"),
            expected_chosen_total_amount="650.00",
            expected_arithmetic_status_benchmark_label="reconciled",  # 250+250+150=650=chosen total
            notes=(
                "Deterministic: two RouteResults exist, aggregation's "
                "conflict logic runs, last-page-wins-but-flagged applies."
            ),
        ),
        # MAX_VISION_PAGES=2 (identical outcome at =5, since 2 image pages
        # fit in one chunk either way): BOTH pages go into the SAME vision
        # call, producing exactly ONE RouteResult. aggregate() only ever
        # compares MULTIPLE RouteResults - with a single one, its early-
        # return path means cross-chunk conflict detection cannot run AT
        # ALL. Whatever single total_amount the real model decides to
        # report (500? 650? something else entirely?) becomes the only
        # value seen by the rest of the pipeline. This fixture MUST use
        # MAX_VISION_PAGES=1 specifically to exercise the conflict path -
        # at the default 5 (or at 2), it silently does not test what it
        # looks like it is designed to test.
        ExpectedChunkSettingOutcome(
            max_vision_pages_label="2_or_5",
            chunk_plan=ExpectedVisionChunkPlan(2, ((1, 2),)),
            cross_chunk_conflict_possible=False,
            deterministic=False,
            expected_needs_review=None,
            expected_review_reason_contains=(),
            expected_chosen_total_amount=None,
            expected_arithmetic_status_benchmark_label=None,
            notes=(
                "NOT deterministic: only one RouteResult exists at this "
                "setting, so aggregation's conflict detection never runs. "
                "The real model's single response decides total_amount, "
                "and therefore needs_review, and therefore arithmetic "
                "status - none of this can be asserted offline. Only a "
                "live benchmark run at this setting could reveal the "
                "actual outcome."
            ),
        ),
    ),
)


# =============================================================================
# Fixture 10 - Likely two-invoice PDF
# =============================================================================

_FIXTURE_10_CORE = ExpectedScenarioCore(
    fixture_id="fixture_10_two_invoice_numbers",
    filename="two_invoice_numbers.pdf",
    scenario=(
        "Single PDF likely containing two separate invoices concatenated "
        "together (e.g. a customer scanned two documents as one file), each "
        "with its own invoice number. Both pages are text-native so that "
        "classification/routing is not the variable under test."
    ),
    page_layout=(
        ExpectedPageLayout(1, "text", "Full invoice 'INV-A100' (header + items + total)"),
        ExpectedPageLayout(2, "text", "Full invoice 'INV-B200' (different header + items + total)"),
    ),
    expected_document_classification="text-native",
    expected_text_pages=(1, 2),
    expected_image_pages=(),
    expected_blank_pages=(),
    expected_chunk_plans=(),
    expected_extraction_routes=("text",),
    # This call count describes the REALISTIC routing outcome: both text
    # pages are concatenated into ONE Gemini text call. See live_expected
    # below for why this single-call reality is exactly what makes fixture
    # 10's live behavior uncertain.
    expected_text_route_calls=1,
    expected_vision_route_calls_by_chunk_limit={1: 0, 2: 0, 5: 0},
)

# --- Offline aggregation expectation (Correction C) -------------------------
# A deliberate SIMPLIFICATION: two RouteResults are constructed directly in
# the test (bypassing the single-call reality below) specifically to
# exercise aggregation.py's conflict-detection logic in isolation. This is
# NOT a claim about what the real pipeline does with this exact PDF.
FIXTURE_10_OFFLINE = ExpectedMultiInvoiceOfflineAggregation(
    route_a_invoice_number="INV-A100",
    route_b_invoice_number="INV-B200",
    also_conflicting_fields=("seller_name", "buyer_name", "total_amount"),
    expected_output_invoice_count=1,  # one-invoice-per-PDF architecture; no segmentation
    expected_needs_review=True,
    expected_review_reason_contains=(
        "possible multiple invoices in one PDF",
        "conflict in invoice_number",
    ),
    expected_conflicts=(
        ExpectedConflict("invoice_number", "possible_multiple_invoices"),
        ExpectedConflict("seller_name", "conflict"),
        ExpectedConflict("buyer_name", "conflict"),
        ExpectedConflict("total_amount", "conflict"),
    ),
    is_realistic_pipeline_path=False,
)

# --- Realistic live-pipeline expectation (Correction C) ---------------------
# What ACTUALLY happens: both text pages enter ONE Gemini text call as a
# single concatenated prompt. The model returns (at most) ONE JSON object
# per the fixed schema - there is no second RouteResult for aggregation to
# compare against, so aggregation's conflict detection is UNREACHABLE here.
# This is the pack's deliberate "blind spot" fixture: the offline simulation
# above proves the detection logic works IF two RouteResults ever arise, but
# cannot prove whether two RouteResults CAN arise from one concatenated-text
# call, nor what a real model does when shown two invoices in one prompt.
FIXTURE_10_LIVE = ExpectedMultiInvoiceLiveBenchmark(
    live_expected_outcome_set=(
        ExpectedMultiInvoiceLiveOutcome(
            "Model returns only the first invoice's data (INV-A100), "
            "silently dropping the second entirely. The pipeline would see "
            "one clean, internally-consistent result and needs_review "
            "would very likely be False - a genuine blind spot, since "
            "nothing there is inherently wrong from the schema's point of "
            "view.",
            plausible=True,
        ),
        ExpectedMultiInvoiceLiveOutcome(
            "Model returns only the second invoice's data (INV-B200), "
            "same blind-spot consequence as above.",
            plausible=True,
        ),
        ExpectedMultiInvoiceLiveOutcome(
            "Model returns a confused hybrid record (e.g. INV-A100's number "
            "with INV-B200's totals). Likely to fail check_required or the "
            "totals-reconciliation check, surfacing SOME review flag, but "
            "for the wrong underlying reason (arithmetic mismatch, not "
            "'two invoices detected').",
            plausible=True,
        ),
        ExpectedMultiInvoiceLiveOutcome(
            "Model attempts to represent both invoices (e.g. an array-like "
            "structure) that does not fit the fixed single-object schema, "
            "failing JSON parsing or Pydantic validation entirely - "
            "triggering the Claude text fallback path if enabled, or a "
            "hard extraction failure otherwise.",
            plausible=True,
        ),
    ),
    needs_review_guaranteed=False,
    aggregation_conflict_detection_reachable=False,
    notes=(
        "Do not assert needs_review=True for the live/realistic path. The "
        "offline aggregation test above is a valid, useful test of "
        "aggregation.py in isolation, but it does not describe what this "
        "exact PDF actually does when run through the real pipeline - only "
        "a live benchmark run can reveal which of the outcomes above (or "
        "another one entirely) actually occurs."
    ),
)

FIXTURE_10 = ExpectedMultiInvoiceScenario(
    core=_FIXTURE_10_CORE,
    offline_expected=FIXTURE_10_OFFLINE,
    live_expected=FIXTURE_10_LIVE,
)


# =============================================================================
# Registries for the self-validation tests
# =============================================================================

STANDARD_SCENARIOS: tuple[ExpectedInvoiceScenario, ...] = (
    FIXTURE_01, FIXTURE_02, FIXTURE_03, FIXTURE_04,
    FIXTURE_05, FIXTURE_06, FIXTURE_07,
)

ALL_CORES: tuple[ExpectedScenarioCore, ...] = tuple(
    s.core for s in STANDARD_SCENARIOS
) + (
    FIXTURE_08.clean.core,
    FIXTURE_09.core,
    FIXTURE_10.core,
)
