"""M6 benchmark: manifest + ground-truth loading and strict validation
(tests A-E, plus tax-alias and not-extractable field handling)."""

import json

import pytest

from invoice_extractor.benchmark.dataset import (
    BenchmarkConfigError,
    load_manifest,
    normalize_basename,
)

from .benchmark_helpers import gt, manifest_entry, write_manifest


# --- A: valid load ------------------------------------------------------------

def test_a_valid_manifest_and_ground_truth_load(tmp_path):
    m = write_manifest(
        tmp_path,
        [manifest_entry("c1", "a.pdf"), manifest_entry("c2", "b.pdf", "mixed")],
        {
            "c1": gt("c1", invoice={"invoice_number": "INV-1", "total_amount": "100.00"},
                     line_items=[{"line_no": "1", "amount": "100.00"}]),
            "c2": gt("c2", invoice={"tax": "5.00", "ship_to": "Rotterdam"}),
        },
    )
    ds = load_manifest(m)
    assert [c.case_id for c in ds.cases] == ["c1", "c2"]  # sorted, deterministic
    assert ds.cases[0].invoice["total_amount"] == __import__("decimal").Decimal("100.00")
    # not-extractable field validated but surfaced separately.
    assert ds.cases[1].not_extractable_header_fields() == ("ship_to",)


# --- B: duplicate case id -----------------------------------------------------

def test_b_duplicate_case_id_rejected(tmp_path):
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    (gt_dir / "c1.json").write_text(json.dumps(gt("c1")), encoding="utf-8")
    manifest = {"cases": [
        {"case_id": "c1", "source_file": "a.pdf", "document_type": "text_single_page",
         "expected_outcome": "extracted", "ground_truth": "ground_truth/c1.json"},
        {"case_id": "c1", "source_file": "b.pdf", "document_type": "text_single_page",
         "expected_outcome": "extracted", "ground_truth": "ground_truth/c1.json"},
    ]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkConfigError, match="duplicate case_id"):
        load_manifest(path)


# --- C: missing ground-truth file --------------------------------------------

def test_c_missing_ground_truth_file_rejected(tmp_path):
    manifest = {"cases": [
        {"case_id": "c1", "source_file": "a.pdf", "document_type": "text_single_page",
         "expected_outcome": "extracted", "ground_truth": "ground_truth/nope.json"},
    ]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkConfigError, match="not found"):
        load_manifest(path)


# --- D: unsupported field -----------------------------------------------------

def test_d_unsupported_header_field_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"vendor_secret": "x"})})
    with pytest.raises(BenchmarkConfigError, match="unsupported header field"):
        load_manifest(m)


def test_d_unsupported_line_field_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", line_items=[{"weight_kg": "5"}])})
    with pytest.raises(BenchmarkConfigError, match="unsupported line-item field"):
        load_manifest(m)


# --- E: invalid decimal / date -----------------------------------------------

def test_e_invalid_decimal_rejected_safely(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"total_amount": "twelve"})})
    with pytest.raises(BenchmarkConfigError) as exc:
        load_manifest(m)
    assert "not a valid decimal" in str(exc.value)
    assert "twelve" not in str(exc.value)  # raw value not echoed


def test_e_invalid_date_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"invoice_date": "07/01/2026"})})
    with pytest.raises(BenchmarkConfigError, match="ISO date"):
        load_manifest(m)


def test_e_impossible_calendar_date_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"invoice_date": "2026-13-40"})})
    with pytest.raises(BenchmarkConfigError, match="calendar date"):
        load_manifest(m)


# --- tax alias + tax_amount rejection -----------------------------------------

def test_tax_alias_accepted_and_stored(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"tax": "19.00"})})
    ds = load_manifest(m)
    assert ds.cases[0].invoice["tax"] == __import__("decimal").Decimal("19.00")


def test_tax_amount_extractor_name_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"tax_amount": "19.00"})})
    with pytest.raises(BenchmarkConfigError, match="use ground-truth alias 'tax'"):
        load_manifest(m)


def test_both_tax_and_tax_amount_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", invoice={"tax": "1.00", "tax_amount": "1.00"})})
    with pytest.raises(BenchmarkConfigError, match="both 'tax' and its alias"):
        load_manifest(m)


# --- duplicate line identifiers, bad structure --------------------------------

def test_duplicate_line_no_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf")],
                       {"c1": gt("c1", line_items=[{"line_no": "1"}, {"line_no": "1"}])})
    with pytest.raises(BenchmarkConfigError, match="duplicate line_no"):
        load_manifest(m)


def test_malformed_json_rejected_safely(tmp_path):
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    (gt_dir / "c1.json").write_text("{not valid json", encoding="utf-8")
    manifest = {"cases": [
        {"case_id": "c1", "source_file": "a.pdf", "document_type": "text_single_page",
         "expected_outcome": "extracted", "ground_truth": "ground_truth/c1.json"},
    ]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkConfigError, match="malformed JSON"):
        load_manifest(path)


def test_invalid_document_type_rejected(tmp_path):
    m = write_manifest(tmp_path, [manifest_entry("c1", "a.pdf", document_type="weird")],
                       {"c1": gt("c1")})
    with pytest.raises(BenchmarkConfigError, match="document_type"):
        load_manifest(m)


def test_committed_examples_are_valid(tmp_path):
    # The committed synthetic example manifest must always load cleanly.
    from pathlib import Path
    ds = load_manifest(Path("benchmark/examples/manifest_example.json"))
    assert {c.case_id for c in ds.cases} == {"syn_text_single", "syn_mixed"}


# --- basename normalization (no casefold) -------------------------------------

def test_normalize_basename_strips_dirs_both_separators():
    assert normalize_basename("a/b/c.pdf") == "c.pdf"
    assert normalize_basename("a\\b\\c.pdf") == "c.pdf"


def test_normalize_basename_preserves_case_and_punctuation():
    assert normalize_basename("Invoice-A.pdf") == "Invoice-A.pdf"
    assert normalize_basename("Invoice-A.pdf") != normalize_basename("invoice-a.pdf")
