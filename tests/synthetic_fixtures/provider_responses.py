"""Deterministic provider-response test doubles for the synthetic pack.

Converts hand-authored ground-truth records (ground_truth.py) into the JSON
STRINGS a real Gemini/Claude call would return, plus a call-recording seam
that intercepts ONLY the provider boundary
(gemini_client._generate / claude_client._request) so the rest of the
pipeline - PDF analysis, page classification, rendering, chunking,
aggregation, schema normalization, validation, provenance - runs for real,
unmocked.

Ground truth defines expected BUSINESS VALUES; the functions here are the
"test response adapter" that convert those values into provider-shaped JSON
text. The pipeline still has to parse, normalize, coerce Decimals, validate,
and aggregate this text itself - nothing here bypasses that by constructing
an already-normalized Invoice object.

Imports no provider SDKs (no google.genai, no anthropic) and makes no
network calls - exception objects for failure-mode tests are constructed by
the test files themselves (which already need the real SDK exception types
for pytest.raises), not here. Contains no application-behavior assertions -
this module produces data and records calls; the test files decide what is
correct.
"""

import json
from dataclasses import dataclass, field

from . import builders as b
from . import ground_truth as gt

# =============================================================================
# Generic ground-truth -> provider JSON adapter
# =============================================================================


def _line_item_dict(li: gt.ExpectedLineItem) -> dict:
    return {
        "description": li.description,
        "quantity": li.quantity,
        "unit_price": li.unit_price,
        "amount": li.amount,
    }


def _raw_line_item_dict(r: gt.ExpectedRawModelLineItem) -> dict:
    return {
        "description": r.description,
        "quantity": r.quantity,
        "unit_price": r.unit_price,
        "amount": r.amount,
    }


def invoice_response_json(
    scenario: gt.ExpectedInvoiceScenario,
    *,
    line_items: list[dict] | None = None,
    **field_overrides,
) -> str:
    """Serialize an ExpectedInvoiceScenario's header + line items into the
    JSON string a well-behaved Gemini/Claude call would return. By default
    uses ALL of the scenario's line items unchanged; pass `line_items` to
    return only a subset (for per-chunk/per-route responses) or a
    deliberately corrupted list. `**field_overrides` replaces individual
    header fields (e.g. a currency symbol instead of the ISO code, or a
    European-formatted number string) to exercise a specific normalization
    path end to end.
    """
    data = {
        "invoice_number": scenario.expected_invoice_number,
        "invoice_date": scenario.expected_invoice_date,
        "currency": scenario.expected_currency,
        "seller_name": scenario.expected_seller_name,
        "seller_address": None,
        "buyer_name": scenario.expected_buyer_name,
        "buyer_address": None,
        "subtotal": scenario.expected_subtotal,
        "tax_amount": scenario.expected_tax_amount,
        "total_amount": scenario.expected_total_amount,
        "payment_terms": scenario.expected_payment_terms,
        "line_items": (
            line_items if line_items is not None
            else [_line_item_dict(li) for li in scenario.expected_line_items]
        ),
    }
    data.update(field_overrides)
    return json.dumps(data)


def invoice_response_json_subset(
    scenario: gt.ExpectedInvoiceScenario, item_slice: slice, **field_overrides,
) -> str:
    """A response covering only some of the scenario's line items - for a
    single chunk or a single route in a multi-route/multi-chunk fixture."""
    items = [_line_item_dict(li) for li in scenario.expected_line_items[item_slice]]
    return invoice_response_json(scenario, line_items=items, **field_overrides)


# =============================================================================
# Fixture 4 - European number formatting echoed by the (mocked) model
# =============================================================================

def fixture_04_response_json() -> str:
    """Puts EU-formatted STRINGS ('1.234,56') into the numeric fields, as a
    real model might echo the source document's formatting verbatim - this
    is what exercises coerce_decimal's European-format branch end to end via
    the real pipeline, rather than the isolated unit test."""
    scenario = gt.FIXTURE_04
    return invoice_response_json(
        scenario,
        line_items=[
            {"description": li.description, "quantity": li.quantity,
             "unit_price": b.format_amount_eu(li.unit_price),
             "amount": b.format_amount_eu(li.amount)}
            for li in scenario.expected_line_items
        ],
        subtotal=b.format_amount_eu(scenario.expected_subtotal),
        tax_amount=b.format_amount_eu(scenario.expected_tax_amount),
        total_amount=b.format_amount_eu(scenario.expected_total_amount),
    )


# =============================================================================
# Fixture 5 - GBP symbol echoed as currency by the (mocked) model
# =============================================================================

def fixture_05_response_json() -> str:
    """Returns '£' (the raw symbol) as the currency value instead of the ISO
    code 'GBP', exercising normalize_currency's symbol-mapping branch."""
    return invoice_response_json(gt.FIXTURE_05, currency="£")


# =============================================================================
# Fixture 8 - clean vs header-hallucination model output
# =============================================================================

def fixture_08_clean_response_json() -> str:
    return invoice_response_json(gt.FIXTURE_08.clean)


def fixture_08_hallucination_response_json() -> str:
    """Simulates a model that hallucinated the repeated column header as a
    line item on each page: 6 genuine rows + 3 header-shaped rows (only
    `description` set, all numeric fields null). See ground_truth.py's
    FIXTURE_08.corrupted for the documented known gap this exercises."""
    items = [_raw_line_item_dict(r) for r in gt.FIXTURE_08.corrupted.raw_model_line_items]
    return invoice_response_json(gt.FIXTURE_08.clean, line_items=items)


# =============================================================================
# Fixture 9 - conflicting totals across chunks
# =============================================================================
# Ground truth intentionally leaves header identity unconstrained for this
# fixture (only the total_amount conflict is under test). These values
# mirror scenarios.py's own scenario-authoring choice for the same fixture,
# for consistency with what is visually in the generated PDF - duplicated
# here deliberately since scenarios.py's constants are module-private and
# this file must not depend on that module's internals.

_FIXTURE_09_INVOICE_NUMBER = "INV-9900"
_FIXTURE_09_DATE = "2026-10-01"
_FIXTURE_09_CURRENCY = "USD"
_FIXTURE_09_SELLER = "Anchor Point Shipping"
_FIXTURE_09_BUYER = "Riverside Distributors"


def _fixture_09_base_fields() -> dict:
    return {
        "invoice_number": _FIXTURE_09_INVOICE_NUMBER,
        "invoice_date": _FIXTURE_09_DATE,
        "currency": _FIXTURE_09_CURRENCY,
        "seller_name": _FIXTURE_09_SELLER,
        "seller_address": None,
        "buyer_name": _FIXTURE_09_BUYER,
        "buyer_address": None,
        "payment_terms": None,
    }


def fixture_09_chunk_response_json(item_indices: list[int], total_amount: str) -> str:
    """One chunk's mocked response: only the items at item_indices, this
    chunk's own (possibly conflicting) total_amount, shared tax."""
    items = list(gt.FIXTURE_09.shared_line_items)
    data = _fixture_09_base_fields()
    data.update(
        subtotal=None,
        tax_amount=gt.FIXTURE_09.shared_tax_amount,
        total_amount=total_amount,
        line_items=[_line_item_dict(items[i]) for i in item_indices],
    )
    return json.dumps(data)


# =============================================================================
# Fixture 10 - two independent, complete invoices
# =============================================================================
# Mirrors scenarios.py's own scenario-authoring choice of seller/buyer/date
# per invoice (also not part of ground_truth.py, by design - only the
# invoice_number values and the "also_conflicting_fields" list are pinned
# there). Duplicated here for the same reason as fixture 9 above.

def fixture_10_invoice_a_response_json() -> str:
    offline = gt.FIXTURE_10.offline_expected
    return json.dumps({
        "invoice_number": offline.route_a_invoice_number,
        "invoice_date": "2026-11-01",
        "currency": "USD",
        "seller_name": "Vanguard Shipping Co",
        "seller_address": None,
        "buyer_name": "Oakwood Retailers",
        "buyer_address": None,
        "subtotal": None,
        "tax_amount": "0.00",
        "total_amount": "300.00",
        "payment_terms": None,
        "line_items": [{"description": "Warehousing", "quantity": "1",
                        "unit_price": "300.00", "amount": "300.00"}],
    })


def fixture_10_invoice_b_response_json() -> str:
    offline = gt.FIXTURE_10.offline_expected
    return json.dumps({
        "invoice_number": offline.route_b_invoice_number,
        "invoice_date": "2026-11-03",
        "currency": "USD",
        "seller_name": "Different Seller Corp",
        "seller_address": None,
        "buyer_name": "Different Buyer Inc",
        "buyer_address": None,
        "subtotal": None,
        "tax_amount": "0.00",
        "total_amount": "450.00",
        "payment_terms": None,
        "line_items": [{"description": "Customs clearance", "quantity": "1",
                        "unit_price": "450.00", "amount": "450.00"}],
    })


def fixture_10_hybrid_response_json() -> str:
    """A single, confused response mixing invoice A's number with invoice
    B's totals - one plausible live outcome per FIXTURE_10.live_expected."""
    offline = gt.FIXTURE_10.offline_expected
    return json.dumps({
        "invoice_number": offline.route_a_invoice_number,
        "invoice_date": "2026-11-01",
        "currency": "USD",
        "seller_name": "Vanguard Shipping Co",
        "seller_address": None,
        "buyer_name": "Oakwood Retailers",
        "buyer_address": None,
        "subtotal": None,
        "tax_amount": "0.00",
        "total_amount": "450.00",  # invoice B's total, mismatched with A's items
        "payment_terms": None,
        "line_items": [{"description": "Warehousing", "quantity": "1",
                        "unit_price": "300.00", "amount": "300.00"}],
    })


# =============================================================================
# Deliberately broken responses
# =============================================================================

def malformed_json_text() -> str:
    """Not parseable as JSON at all."""
    return '{"invoice_number": "INV-X", "totally broken json here'


def missing_required_fields_json() -> str:
    """Valid JSON, but every REQUIRED_FIELDS entry is null - passes JSON
    parsing, fails schema.check_required()."""
    return json.dumps({
        "invoice_number": None, "invoice_date": None, "currency": None,
        "seller_name": None, "seller_address": None, "buyer_name": None,
        "buyer_address": None, "subtotal": None, "tax_amount": None,
        "total_amount": None, "payment_terms": None, "line_items": [],
    })


# =============================================================================
# Call-recording provider seams
# =============================================================================

@dataclass
class CallRecord:
    route: str  # "gemini_text" | "gemini_vision" | "claude_text" | "claude_vision"
    model: str
    image_count: int  # 0 for text calls; number of image parts for vision calls


@dataclass
class CallRecorder:
    """Records every provider-boundary call, in order, across BOTH Gemini
    and Claude seams. Recording only - asserting correctness is the test
    file's job, not this class's."""

    records: list[CallRecord] = field(default_factory=list)

    @property
    def gemini_text_count(self) -> int:
        return sum(1 for r in self.records if r.route == "gemini_text")

    @property
    def gemini_vision_count(self) -> int:
        return sum(1 for r in self.records if r.route == "gemini_vision")

    @property
    def claude_text_count(self) -> int:
        return sum(1 for r in self.records if r.route == "claude_text")

    @property
    def claude_vision_count(self) -> int:
        return sum(1 for r in self.records if r.route == "claude_vision")

    @property
    def route_order(self) -> list[str]:
        return [r.route for r in self.records]

    @property
    def gemini_vision_image_counts(self) -> list[int]:
        """Number of images in each Gemini vision call, in call order."""
        return [r.image_count for r in self.records if r.route == "gemini_vision"]

    @property
    def claude_vision_image_counts(self) -> list[int]:
        return [r.image_count for r in self.records if r.route == "claude_vision"]


def install_provider_seams(
    monkeypatch,
    cfg,
    *,
    gemini_text: list = (),
    gemini_vision: list = (),
    claude_text: list = (),
    claude_vision: list = (),
) -> CallRecorder:
    """Monkeypatch gemini_client._generate and claude_client._request with
    call-recording fakes. Each queue holds, in call order, either a response
    string or a BaseException instance to raise. Distinguishes text vs
    vision calls the same way the real client functions differ in shape:
    Gemini text is `contents == [prompt_string]` (len 1); Gemini vision is
    `contents == [prompt_string, *image_parts]` (len > 1). Claude text is a
    plain string `content`; Claude vision is a list of content blocks.

    Returns the CallRecorder so tests can assert exact counts/order/models
    afterwards. Import the two modules being patched from the SAME place the
    test file does (invoice_extractor.gemini_client / .claude_client), not
    this module, since this module must not import provider SDKs.

    A provider is only patched if at least one response was queued for it
    (text or vision). This matters for tests exercising a missing API key:
    the key check lives INSIDE the real _generate/_request functions (via
    _get_client/_get_model), so if a provider is never expected to be
    called, leaving its real function in place lets that check - or, if the
    call is unexpectedly reached anyway, the autouse network-blocking
    fixture - fire naturally instead of a misleading "unexpected extra
    call" from an unconditionally-installed fake.
    """
    recorder = CallRecorder()
    gemini_queue = list(gemini_text), list(gemini_vision)
    claude_queue = list(claude_text), list(claude_vision)

    def gemini_fake(cfg_, model, contents):
        is_vision = len(contents) > 1
        route = "gemini_vision" if is_vision else "gemini_text"
        image_count = len(contents) - 1 if is_vision else 0
        recorder.records.append(CallRecord(route, model, image_count))
        queue = gemini_queue[1] if is_vision else gemini_queue[0]
        if not queue:
            raise AssertionError(f"unexpected extra call to {route} ({model})")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def claude_fake(cfg_, model, content):
        is_vision = isinstance(content, list)
        route = "claude_vision" if is_vision else "claude_text"
        image_count = (
            sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image")
            if is_vision else 0
        )
        recorder.records.append(CallRecord(route, model, image_count))
        queue = claude_queue[1] if is_vision else claude_queue[0]
        if not queue:
            raise AssertionError(f"unexpected extra call to {route} ({model})")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    from invoice_extractor import claude_client, gemini_client

    if gemini_queue[0] or gemini_queue[1]:
        monkeypatch.setattr(gemini_client, "_generate", gemini_fake)
    if claude_queue[0] or claude_queue[1]:
        monkeypatch.setattr(claude_client, "_request", claude_fake)
    return recorder


def install_render_spy(monkeypatch) -> list[list[int]]:
    """Wraps (does not replace) pdf_utils.render_pages_png to record the
    exact page_numbers list passed to each call, while still calling
    through to the REAL renderer - page rendering itself is never mocked,
    per this milestone's constraints. Returns the list that gets appended
    to, in call order."""
    from invoice_extractor import pdf_utils

    recorded: list[list[int]] = []
    real_render = pdf_utils.render_pages_png

    def spy(path, page_numbers, dpi=200):
        recorded.append(list(page_numbers))
        return real_render(path, page_numbers, dpi)

    monkeypatch.setattr(pdf_utils, "render_pages_png", spy)
    return recorded
