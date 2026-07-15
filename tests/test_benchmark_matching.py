"""M6 benchmark: invoice (source_file) matching + deterministic line-item
matching tiers with fuzzy OFF by default (tests F-I, O-U)."""

from decimal import Decimal

from invoice_extractor.benchmark.matching import (
    match_invoices,
    match_lines,
    norm_identifier,
    norm_string,
)
from invoice_extractor.benchmark.dataset import GroundTruthCase


def _case(case_id, source_file):
    return GroundTruthCase(
        case_id=case_id, source_file=source_file, document_type="text_single_page",
        expected_outcome="extracted", ground_truth_path="", invoice={}, line_items=[],
        expected_needs_review=False, accepted_review_categories=(), ignored_fields=(),
        field_tolerances={},
    )


def _line(**fields):
    return dict(fields)


# --- F/G/H/I: invoice matching ------------------------------------------------

def test_f_exact_source_file_match():
    cases = [_case("c1", "a.pdf")]
    matches, extra = match_invoices(cases, {"a.pdf": [{"invoice_id": "INV-1"}]})
    assert matches[0].status == "matched"
    assert extra == []


def test_f_case_differing_filename_does_not_match():
    cases = [_case("c1", "Invoice-A.pdf")]
    matches, extra = match_invoices(cases, {"invoice-a.pdf": [{"invoice_id": "INV-1"}]})
    assert matches[0].status == "missing_result"       # case differs -> no match
    assert extra == ["invoice-a.pdf"]                   # unexpected workbook file


def test_g_missing_workbook_case_reported():
    cases = [_case("c1", "a.pdf")]
    matches, extra = match_invoices(cases, {})
    assert matches[0].status == "missing_result"


def test_h_extra_workbook_case_reported():
    cases = [_case("c1", "a.pdf")]
    matches, extra = match_invoices(cases, {"a.pdf": [{"x": 1}], "b.pdf": [{"x": 2}]})
    assert extra == ["b.pdf"]


def test_i_duplicate_workbook_rows_flagged():
    cases = [_case("c1", "a.pdf")]
    matches, _ = match_invoices(cases, {"a.pdf": [{"x": 1}, {"x": 2}]})
    assert matches[0].status == "duplicate_result"
    assert matches[0].duplicate_count == 2


# --- O/P/Q: exact line-match tiers --------------------------------------------

def test_o_unique_line_no_match():
    exp = [_line(line_no="1", description="A"), _line(line_no="2", description="B")]
    act = [_line(line_no="2", description="different text"),
           _line(line_no="1", description="also different")]
    out = match_lines(exp, act)
    matched = {(p.expected_index, p.actual_index, p.method) for p in out.matched}
    assert matched == {(0, 1, "line_no"), (1, 0, "line_no")}
    assert out.missing == [] and out.extra == []


def test_p_unique_item_code_match():
    exp = [_line(item_code="31C207", description="A")]
    act = [_line(item_code=" 31c207 ", description="totally different")]  # trim+casefold
    out = match_lines(exp, act)
    assert len(out.matched) == 1 and out.matched[0].method == "item_code"


def test_q_strong_composite_match():
    exp = [_line(description="Ocean Freight", quantity="1", unit_price="100", amount="100")]
    act = [_line(description="ocean   freight", quantity="1", unit_price="100", amount="100")]
    out = match_lines(exp, act)
    assert len(out.matched) == 1 and out.matched[0].method == "composite"


# --- R: conflicting numbers not matched (fuzzy off AND on) --------------------

def test_r_similar_description_conflicting_numbers_not_matched_default():
    exp = [_line(description="Freight charge", quantity="1", unit_price="50", amount="50")]
    act = [_line(description="Freight charge", quantity="1", unit_price="80", amount="80")]
    out = match_lines(exp, act)  # fuzzy OFF: composite key differs, no match
    assert out.matched == []
    assert len(out.missing) == 1 and len(out.extra) == 1


def test_r_conflicting_numbers_still_not_matched_with_fuzzy_on():
    exp = [_line(description="Freight charge", amount="50")]
    act = [_line(description="Freight charge", amount="80")]
    out = match_lines(exp, act, fuzzy_enabled=True)  # numeric incompat blocks fuzzy
    assert out.matched == []


# --- Fuzzy default-off behavior -----------------------------------------------

def test_fuzzy_off_by_default_near_match_unmatched():
    exp = [_line(description="Ocean freight service")]
    act = [_line(description="Ocean freight services")]  # 1 char diff, no numerics
    out = match_lines(exp, act)  # default: no fuzzy tier
    assert out.matched == []
    assert len(out.missing) == 1 and len(out.extra) == 1


def test_fuzzy_on_matches_and_records_method_and_confidence():
    exp = [_line(description="Ocean freight service", amount="100")]
    act = [_line(description="Ocean freight services", amount="100")]
    out = match_lines(exp, act, fuzzy_enabled=True, fuzzy_threshold=Decimal("0.80"))
    assert len(out.matched) == 1
    p = out.matched[0]
    assert p.method == "fuzzy"
    assert Decimal("0.80") <= p.confidence <= Decimal("1")


# --- S: ambiguous reported, not forced ----------------------------------------

def test_s_ambiguous_fuzzy_match_reported_not_forced():
    exp = [_line(description="widget", amount="10")]
    act = [_line(description="widgel", amount="10"),   # equally close typos
           _line(description="widgez", amount="10")]
    out = match_lines(exp, act, fuzzy_enabled=True, fuzzy_threshold=Decimal("0.50"))
    assert out.matched == []                # not forced to either
    assert len(out.ambiguous) == 1
    assert out.ambiguous[0].method == "ambiguous"


# --- T/U: missing + extra -----------------------------------------------------

def test_t_missing_expected_line():
    exp = [_line(line_no="1", description="A"), _line(line_no="2", description="B")]
    act = [_line(line_no="1", description="A")]
    out = match_lines(exp, act)
    assert len(out.matched) == 1
    assert [p.expected_index for p in out.missing] == [1]


def test_u_extra_actual_line():
    exp = [_line(line_no="1", description="A")]
    act = [_line(line_no="1", description="A"), _line(line_no="9", description="Z")]
    out = match_lines(exp, act)
    assert [p.actual_index for p in out.extra] == [1]


def test_non_unique_key_falls_through_not_forced():
    # Two expected lines share line_no in ACTUAL (duplicate) -> tier-1 skips
    # that key on the actual side; composite still resolves what it can.
    exp = [_line(line_no="1", description="A", quantity="1", unit_price="1", amount="1")]
    act = [_line(line_no="1", description="A", quantity="1", unit_price="1", amount="1"),
           _line(line_no="1", description="B", quantity="2", unit_price="2", amount="2")]
    out = match_lines(exp, act)
    # line_no "1" is duplicated in actual -> not unique -> composite matches row 0.
    assert len(out.matched) == 1
    assert out.matched[0].method == "composite"
    assert len(out.extra) == 1


# --- normalization ------------------------------------------------------------

def test_norm_string_collapses_and_casefolds():
    assert norm_string("  Ocean   FREIGHT  ") == "ocean freight"


def test_norm_identifier_trims_casefolds_but_keeps_hyphens():
    assert norm_identifier(" INV-1001 ") == "inv-1001"
    assert norm_identifier("INV-1001") != norm_identifier("INV1001")  # hyphen kept
