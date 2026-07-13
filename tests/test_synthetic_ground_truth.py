"""Self-validation tests for the synthetic invoice ground-truth records.

These tests validate the GROUND TRUTH DATA ITSELF
(tests/synthetic_fixtures/ground_truth.py) - internal consistency,
uniqueness, and coverage. They do NOT exercise the application pipeline:

  * no PDFs are generated
  * no provider clients are imported
  * no network calls are made
  * nothing in `invoice_extractor` is imported
  * no .env is required
  * no files are left behind

The one exception is `_reconciles()` below, a small INDEPENDENT
reimplementation of the three documented reconciliation rules (see
schema.py:validate_invoice's docstring), used only to sanity-check that this
ground truth's own numbers are internally consistent. It does not import
invoice_extractor.schema - a bug in the real validate_invoice would not be
able to make a wrong ground-truth number look "validated".
"""

from decimal import Decimal

from .synthetic_fixtures import ground_truth as gt

STANDARD = gt.STANDARD_SCENARIOS
ALL_CORES = gt.ALL_CORES


def _reconciles(
    line_sum: Decimal,
    tax: Decimal | None,
    subtotal: Decimal | None,
    total: Decimal,
    abs_tol: Decimal = Decimal("0.02"),
    rel_tol: Decimal = Decimal("0.005"),
) -> bool:
    tol = max(abs_tol, rel_tol * abs(total))

    def close(a: Decimal, b: Decimal) -> bool:
        return abs(a - b) <= tol

    return (
        (tax is not None and close(line_sum + tax, total))
        or close(line_sum, total)
        or (
            subtotal is not None
            and close(subtotal + (tax or Decimal("0")), total)
            and close(line_sum, subtotal)
        )
    )


def _line_sum(items) -> Decimal:
    return sum((Decimal(it.amount) for it in items), Decimal("0"))


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------

class TestUniqueness:
    def test_ten_fixtures_total(self):
        assert len(ALL_CORES) == 10

    def test_fixture_ids_unique(self):
        ids = [c.fixture_id for c in ALL_CORES]
        assert len(ids) == len(set(ids)) == 10

    def test_filenames_unique(self):
        names = [c.filename for c in ALL_CORES]
        assert len(names) == len(set(names)) == 10


# ---------------------------------------------------------------------------
# Page layout integrity
# ---------------------------------------------------------------------------

class TestPageLayoutIntegrity:
    def test_page_numbers_contiguous_from_one(self):
        for core in ALL_CORES:
            numbers = [p.page_number for p in core.page_layout]
            assert numbers == list(range(1, len(numbers) + 1)), core.fixture_id

    def test_every_page_present_exactly_once_across_kind_sets(self):
        for core in ALL_CORES:
            text_set = set(core.expected_text_pages)
            image_set = set(core.expected_image_pages)
            blank_set = set(core.expected_blank_pages)
            declared_total = len(text_set) + len(image_set) + len(blank_set)
            union = text_set | image_set | blank_set
            assert declared_total == len(union), (
                f"{core.fixture_id}: a page number appears in more than one kind set"
            )
            layout_pages = {p.page_number for p in core.page_layout}
            assert union == layout_pages, core.fixture_id

    def test_page_kind_matches_layout(self):
        for core in ALL_CORES:
            kind_sets = {
                "text": set(core.expected_text_pages),
                "image": set(core.expected_image_pages),
                "blank": set(core.expected_blank_pages),
            }
            for p in core.page_layout:
                assert p.page_number in kind_sets[p.page_kind], (
                    f"{core.fixture_id}: page {p.page_number} declared as "
                    f"'{p.page_kind}' but not present in the matching set"
                )


# ---------------------------------------------------------------------------
# Chunk plan integrity
# ---------------------------------------------------------------------------

def _all_chunk_plans():
    """Yield (fixture_id, image_pages, chunk_plan) for every declared plan,
    across the standard cores AND fixture 9's per-setting plans."""
    for core in ALL_CORES:
        for plan in core.expected_chunk_plans:
            yield core.fixture_id, set(core.expected_image_pages), plan
    for setting in gt.FIXTURE_09.per_setting:
        yield gt.FIXTURE_09.core.fixture_id, set(gt.FIXTURE_09.core.expected_image_pages), setting.chunk_plan


class TestChunkPlanIntegrity:
    def test_chunks_cover_every_image_page_exactly_once(self):
        for fixture_id, image_pages, plan in _all_chunk_plans():
            flat = [n for chunk in plan.chunks for n in chunk]
            assert len(flat) == len(set(flat)), (
                f"{fixture_id} @max={plan.max_pages_per_request}: duplicate page in chunks"
            )
            assert set(flat) == image_pages, (
                f"{fixture_id} @max={plan.max_pages_per_request}: chunk pages "
                f"{sorted(flat)} != image pages {sorted(image_pages)}"
            )

    def test_chunks_contain_no_blank_or_text_page(self):
        for core in ALL_CORES:
            forbidden = set(core.expected_text_pages) | set(core.expected_blank_pages)
            for plan in core.expected_chunk_plans:
                flat = {n for chunk in plan.chunks for n in chunk}
                assert not (flat & forbidden), (
                    f"{core.fixture_id} @max={plan.max_pages_per_request}: "
                    f"chunk contains a non-image page"
                )

    def test_chunk_sizes_do_not_exceed_declared_maximum(self):
        for fixture_id, _image_pages, plan in _all_chunk_plans():
            for chunk in plan.chunks:
                assert len(chunk) <= plan.max_pages_per_request, (
                    f"{fixture_id}: chunk {chunk} exceeds max_pages_per_request="
                    f"{plan.max_pages_per_request}"
                )

    def test_chunks_are_ordered_ascending_within_and_across(self):
        for fixture_id, _image_pages, plan in _all_chunk_plans():
            flat = [n for chunk in plan.chunks for n in chunk]
            assert flat == sorted(flat), f"{fixture_id}: chunk pages not in ascending order"


# ---------------------------------------------------------------------------
# Money/quantity typing
# ---------------------------------------------------------------------------

def _all_money_and_quantity_strings():
    """Yield every monetary/quantity value across all 10 fixtures that
    should be a Decimal-compatible string (or None), never a float."""
    for s in STANDARD:
        yield s.expected_subtotal
        yield s.expected_tax_amount
        yield s.expected_total_amount
        for li in s.expected_line_items:
            yield li.quantity
            yield li.unit_price
            yield li.amount

    yield gt.FIXTURE_08.clean.expected_subtotal
    yield gt.FIXTURE_08.clean.expected_tax_amount
    yield gt.FIXTURE_08.clean.expected_total_amount
    for li in gt.FIXTURE_08.clean.expected_line_items:
        yield li.quantity
        yield li.unit_price
        yield li.amount
    for raw in gt.FIXTURE_08.corrupted.raw_model_line_items:
        yield raw.quantity
        yield raw.unit_price
        yield raw.amount

    yield gt.FIXTURE_09.shared_tax_amount
    for li in gt.FIXTURE_09.shared_line_items:
        yield li.quantity
        yield li.unit_price
        yield li.amount
    for setting in gt.FIXTURE_09.per_setting:
        yield setting.expected_chosen_total_amount


class TestMoneyAndQuantityTyping:
    def test_no_value_is_a_float(self):
        for value in _all_money_and_quantity_strings():
            assert not isinstance(value, float), f"found a float ground-truth value: {value!r}"

    def test_every_non_null_value_is_a_string(self):
        for value in _all_money_and_quantity_strings():
            if value is not None:
                assert isinstance(value, str), f"expected str or None, got {type(value)}: {value!r}"

    def test_every_non_null_value_parses_as_decimal(self):
        for value in _all_money_and_quantity_strings():
            if value is not None:
                Decimal(value)  # raises InvalidOperation if not parseable


# ---------------------------------------------------------------------------
# Line-item arithmetic
# ---------------------------------------------------------------------------

class TestLineItemArithmetic:
    def test_amount_equals_quantity_times_unit_price(self):
        all_items = list(gt.FIXTURE_08.clean.expected_line_items) + list(gt.FIXTURE_09.shared_line_items)
        for s in STANDARD:
            all_items.extend(s.expected_line_items)
        for li in all_items:
            expected = Decimal(li.quantity) * Decimal(li.unit_price)
            assert expected == Decimal(li.amount), (
                f"{li.description}: {li.quantity} * {li.unit_price} != {li.amount}"
            )

    def test_subtotal_equals_line_sum_where_declared(self):
        for s in STANDARD:
            if s.expected_subtotal_equals_line_sum:
                assert s.expected_subtotal is not None, s.core.fixture_id
                assert Decimal(s.expected_subtotal) == _line_sum(s.expected_line_items), (
                    s.core.fixture_id
                )
        clean = gt.FIXTURE_08.clean
        if clean.expected_subtotal_equals_line_sum:
            assert Decimal(clean.expected_subtotal) == _line_sum(clean.expected_line_items)


# ---------------------------------------------------------------------------
# Arithmetic-status self-consistency
# ---------------------------------------------------------------------------

class TestArithmeticStatusConsistency:
    def test_reconciled_standard_scenarios_actually_reconcile(self):
        for s in STANDARD:
            if s.expected_arithmetic_status_benchmark_label != "reconciled":
                continue
            line_sum = _line_sum(s.expected_line_items)
            tax = Decimal(s.expected_tax_amount) if s.expected_tax_amount is not None else None
            subtotal = Decimal(s.expected_subtotal) if s.expected_subtotal is not None else None
            total = Decimal(s.expected_total_amount)
            assert _reconciles(line_sum, tax, subtotal, total), (
                f"{s.core.fixture_id} labeled 'reconciled' but the independent "
                f"check disagrees: line_sum={line_sum} tax={tax} "
                f"subtotal={subtotal} total={total}"
            )

    def test_inconclusive_standard_scenarios_actually_fail_all_rules(self):
        for s in STANDARD:
            if s.expected_arithmetic_status_benchmark_label != "inconclusive":
                continue
            line_sum = _line_sum(s.expected_line_items)
            tax = Decimal(s.expected_tax_amount) if s.expected_tax_amount is not None else None
            subtotal = Decimal(s.expected_subtotal) if s.expected_subtotal is not None else None
            total = Decimal(s.expected_total_amount)
            assert not _reconciles(line_sum, tax, subtotal, total), (
                f"{s.core.fixture_id} labeled 'inconclusive' but the independent "
                f"check finds it actually reconciles"
            )

    def test_fixture_06_is_intentionally_inconclusive_due_to_unmodeled_charges(self):
        f6 = gt.FIXTURE_06
        assert f6.expected_arithmetic_status_benchmark_label == "inconclusive"
        assert f6.expected_needs_review is True
        assert "discount/shipping/duties/rounding" in f6.expected_review_reason_contains
        assert "discount" in f6.core.scenario.lower() and "freight" in f6.core.scenario.lower()

    def test_header_hallucination_variant_arithmetic_matches_reimplementation(self):
        # Only the 6 real (non-null-amount) rows contribute to the sum -
        # matches schema.py's `amounts = [it.amount for it in items if
        # it.amount is not None]` behavior exactly.
        corrupted = gt.FIXTURE_08.corrupted
        real_amounts = [Decimal(r.amount) for r in corrupted.raw_model_line_items if r.amount is not None]
        line_sum = sum(real_amounts, Decimal("0"))
        clean = gt.FIXTURE_08.clean
        tax = Decimal(clean.expected_tax_amount)
        total = Decimal(clean.expected_total_amount)
        assert _reconciles(line_sum, tax, None, total)
        assert corrupted.expected_arithmetic_status_benchmark_label == "reconciled"

    def test_fixture_09_setting_1_conflict_reconciles_against_chosen_total(self):
        setting = next(s for s in gt.FIXTURE_09.per_setting if s.deterministic)
        line_sum = _line_sum(gt.FIXTURE_09.shared_line_items)
        tax = Decimal(gt.FIXTURE_09.shared_tax_amount)
        total = Decimal(setting.expected_chosen_total_amount)
        assert _reconciles(line_sum, tax, None, total)
        assert setting.expected_arithmetic_status_benchmark_label == "reconciled"


# ---------------------------------------------------------------------------
# Fixture-specific structural corrections (8, 9, 10)
# ---------------------------------------------------------------------------

class TestFixture08Corrections:
    def test_two_variants_present(self):
        assert gt.FIXTURE_08.clean.expected_needs_review is False
        assert gt.FIXTURE_08.corrupted.variant_name == "header_hallucination_output"

    def test_corrupted_variant_does_not_assume_rows_are_dropped(self):
        # This is the required correction: the ground truth must reflect
        # that the current filter KEEPS a description-only row, not drop it.
        corrupted = gt.FIXTURE_08.corrupted
        assert corrupted.survives_normalization is True
        assert corrupted.resulting_line_item_count == 9
        header_rows = [r for r in corrupted.raw_model_line_items if r.description == "Description"]
        assert len(header_rows) == 3
        for row in header_rows:
            assert row.quantity is None and row.unit_price is None and row.amount is None

    def test_known_gap_is_documented(self):
        assert gt.FIXTURE_08.corrupted.known_gap  # non-empty
        assert "survive" in gt.FIXTURE_08.corrupted.known_gap.lower()

    def test_clean_variant_has_exactly_six_items(self):
        assert len(gt.FIXTURE_08.clean.expected_line_items) == 6


class TestFixture09Corrections:
    def test_two_chunk_settings_defined(self):
        assert len(gt.FIXTURE_09.per_setting) == 2

    def test_max_1_deterministic_conflict(self):
        setting = next(s for s in gt.FIXTURE_09.per_setting if s.max_vision_pages_label == "1")
        assert setting.cross_chunk_conflict_possible is True
        assert setting.deterministic is True
        assert setting.expected_needs_review is True
        assert setting.chunk_plan.chunks == ((1,), (2,))

    def test_max_2_or_5_not_deterministic(self):
        setting = next(s for s in gt.FIXTURE_09.per_setting if s.max_vision_pages_label == "2_or_5")
        assert setting.cross_chunk_conflict_possible is False
        assert setting.deterministic is False
        assert setting.expected_needs_review is None
        assert setting.expected_chosen_total_amount is None
        assert setting.chunk_plan.chunks == ((1, 2),)

    def test_settings_are_distinguishable(self):
        s1, s2 = gt.FIXTURE_09.per_setting
        assert s1.cross_chunk_conflict_possible != s2.cross_chunk_conflict_possible
        assert s1.deterministic != s2.deterministic

    def test_does_not_claim_conflict_independent_of_chunk_setting(self):
        # i.e. at least one setting must NOT guarantee a conflict
        assert any(not s.deterministic for s in gt.FIXTURE_09.per_setting)


class TestFixture10Corrections:
    def test_offline_and_live_are_distinct_objects(self):
        assert gt.FIXTURE_10.offline_expected is not gt.FIXTURE_10.live_expected

    def test_offline_aggregation_is_deterministic_and_flagged(self):
        offline = gt.FIXTURE_10.offline_expected
        assert offline.expected_output_invoice_count == 1
        assert offline.expected_needs_review is True
        assert offline.is_realistic_pipeline_path is False
        assert "possible multiple invoices in one PDF" in offline.expected_review_reason_contains
        assert offline.route_a_invoice_number != offline.route_b_invoice_number

    def test_live_outcome_is_not_guaranteed_review(self):
        live = gt.FIXTURE_10.live_expected
        assert live.needs_review_guaranteed is False
        assert live.aggregation_conflict_detection_reachable is False
        assert len(live.live_expected_outcome_set) >= 2

    def test_no_segmentation_proposed(self):
        # Ground truth may acknowledge segmentation is NOT supported, but must
        # not describe it as something this pack implements or proposes.
        forbidden_phrases = ("implement segmentation", "auto-segment", "will segment")
        haystacks = (
            gt.FIXTURE_10.offline_expected.expected_review_reason_contains
            + tuple(o.description for o in gt.FIXTURE_10.live_expected.live_expected_outcome_set)
            + (gt.FIXTURE_10.live_expected.notes,)
        )
        joined = " ".join(haystacks).lower()
        for phrase in forbidden_phrases:
            assert phrase not in joined


# ---------------------------------------------------------------------------
# Review-flag / conflict consistency
# ---------------------------------------------------------------------------

class TestReviewFlagConsistency:
    def test_every_review_required_scenario_has_a_category_or_reason(self):
        for s in STANDARD:
            if s.expected_needs_review:
                assert (
                    s.expected_validation_category != "none"
                    or len(s.expected_review_reason_contains) > 0
                ), s.core.fixture_id

    def test_every_no_review_scenario_has_no_contradictory_conflict(self):
        for s in STANDARD:
            if not s.expected_needs_review:
                assert s.expected_conflicts == (), (
                    f"{s.core.fixture_id}: needs_review=False but conflicts are declared"
                )

    def test_fixture_08_clean_has_no_conflicts(self):
        assert gt.FIXTURE_08.clean.expected_conflicts == ()

    def test_fixture_09_conflict_setting_has_conflict_category_in_reason(self):
        setting = next(s for s in gt.FIXTURE_09.per_setting if s.deterministic)
        assert any("conflict" in r for r in setting.expected_review_reason_contains)

    def test_fixture_10_offline_conflicts_nonempty(self):
        assert len(gt.FIXTURE_10.offline_expected.expected_conflicts) > 0
