"""Packing preparation, Build 6: input boundary, destination grouping,
carton identity/ordering/resequencing, same-carton consolidation, delivery
invoice numbers, persistence, states, and UI wiring. Fully offline."""

import json
from pathlib import Path

import pytest

from apps.web.job_manager import JobError
from apps.web.transfer import extraction
from apps.web.transfer import jobs as tjobs
from apps.web.transfer import models as tm
from apps.web.transfer import packing as pk
from apps.web.transfer import product_lookup as pl
from apps.web.transfer import review as rv
from tests.test_transfer_extraction import note_pdf
from tests.test_transfer_product_lookup import (
    FakeAuth,
    FakeTransport,
    envelope,
    wire_record,
)

ROOT = Path(__file__).resolve().parent.parent

# Items used across the synthetic fixtures.
EAN_1 = "0210116339257"     # item ZEHE380331E997 / E997 / S
EAN_2 = "0210116369698"     # item ZETF381237E085 / E085 / XS
EAN_3 = "0210116400001"     # item ZEAA111111E001 / E001 / M

ROW_1 = ("1", "ZEHE380331E997", EAN_1, "TOP - SQ NK BRA", "1400", "E997",
         "S", "1 PCS")
ROW_1B = ("2", "ZEHE380331E997", EAN_1, "TOP - SQ NK BRA", "1400", "E997",
          "S", "2 PCS")
ROW_2 = ("3", "ZETF381237E085", EAN_2, "SRT - JAZZ SHORTS", "1900", "E085",
         "XS", "2 PCS")
ROW_3 = ("1", "ZEAA111111E001", EAN_3, "A THING", "100", "E001", "M",
         "5 PCS")


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "transfer-jobs"))
    for name in ("PACKING_CARTON_START", "PACKING_CARTON_PAD_WIDTH",
                 "PACKING_INVOICE_PREFIX", "PRODUCT_LOOKUP_BATCH_SIZE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def record_for(ean, item, color, size, desc, price,
               location="ZZOHK101"):
    return wire_record(ean, location=location, item=item, color=color,
                       size=size, desc=desc, price=price)


ALL_RECORDS = [
    record_for(EAN_1, "ZEHE380331E997", "E997", "S", "TOP - SQ NK BRA",
               1400.00),
    record_for(EAN_2, "ZETF381237E085", "E085", "XS", "SRT - JAZZ SHORTS",
               1900.00),
]
ALL_RECORDS_B = [
    record_for(EAN_3, "ZEAA111111E001", "E001", "M", "A THING", 100.00,
               location="ZZOHK202"),
]


def two_destination_job(tmp_path):
    """File 1 -> ZZOHK101 (carton 001: ROW_1 + ROW_1B same product =
    consolidation; ROW_2 different product; carton 002: ROW_1 again =
    same product, other carton). File 2 -> ZZOHK202 with original carton
    001 REUSED (distinct source carton) containing ROW_3."""
    pdf_a = note_pdf(tmp_path / "src-a.pdf", [
        {"page_no": 1, "carton": "001", "rows": (ROW_1, ROW_1B, ROW_2),
         "carton_total": "5 UNIT"},
        {"page_no": 2, "carton": "002", "rows": (ROW_1,),
         "carton_total": "1 UNIT"},
    ])
    pdf_b = note_pdf(tmp_path / "src-b.pdf", [
        {"page_no": 1, "carton": "001", "rows": (ROW_3,),
         "carton_total": "5 UNIT",
         "to_loc_override": "ZZOHK202 Second Outlet",
         "header": {"dn": "ZZWHKM11-OZSO202606049999"}},
    ])
    uploads = [("first.pdf", Path(pdf_a).read_bytes()),
               ("second.pdf", Path(pdf_b).read_bytes())]
    validated, issues = tjobs.validate_transfer_uploads(uploads)
    assert issues == []
    job_id = tjobs.create_transfer_job(uploads, validated)
    extraction.run_extraction(job_id, use_default_adapter=False)
    review = rv.get_or_create_review(job_id)
    rv.save_review(job_id, review)
    rv.approve_review(job_id)
    return job_id


def enrich(job_id, extra_records=(), records=None):
    responses = [(200, envelope(
        (records if records is not None
         else ALL_RECORDS + ALL_RECORDS_B) + list(extra_records))),
        (200, envelope([]))]     # empty fallback stage when triggered
    pl.run_product_lookup(job_id, auth=FakeAuth(),
                          transport=FakeTransport(responses))


def prepared_job(tmp_path):
    job_id = two_destination_job(tmp_path)
    enrich(job_id)
    return job_id


# --- input boundary ---------------------------------------------------------------

class TestInputBoundary:
    def test_current_inputs_accepted(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        assert prepared["status"] == "complete"
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PACKING_PREPARATION_COMPLETE)

    def test_missing_enrichment_rejected(self, tmp_path):
        # lookup never ran: the job is still READY_FOR_PRODUCT_LOOKUP,
        # which is not a preparable state - refused before anything runs
        job_id = two_destination_job(tmp_path)
        with pytest.raises(JobError):
            pk.load_packing_inputs(job_id)

    def test_stale_product_result_rejected(self, tmp_path):
        job_id = prepared_job(tmp_path)
        review = rv.load_review(job_id)
        rv.apply_correction(review, "line", review.lines[0].entity_id,
                            "description", "EDITED AFTER LOOKUP")
        rv.save_review(job_id, review)
        with pytest.raises(JobError, match="stale"):
            pk.load_packing_inputs(job_id)

    def test_malformed_product_result_rejected(self, tmp_path):
        job_id = prepared_job(tmp_path)
        pl.result_path(job_id).write_text("{ nope")
        with pytest.raises(JobError):
            pk.load_packing_inputs(job_id)

    def test_blocking_product_issue_blocks_lines_not_run(self, tmp_path):
        # EAN_3 record missing -> its line gets PRODUCT_NOT_FOUND (blocking)
        job_id = two_destination_job(tmp_path)
        enrich(job_id, records=ALL_RECORDS + [record_for(
            "ZEAA111111E001E001M", "ZEAA111111E001", "E001", "M",
            "A THING", 100.00)][:0])   # no EAN_3, and no fallback record
        prepared = pk.prepare_packing(job_id)
        assert prepared["status"] == "complete_with_issues"
        codes = {i["code"] for i in prepared["issues"]}
        assert pk.PACKING_LINE_BLOCKED_BY_PRODUCT_ISSUE in codes
        assert pk.PACKING_DESTINATION_BLOCKED in codes
        blocked = [g for g in prepared["destinations"]
                   if g["destination_code"] == "ZZOHK202"]
        # ZZOHK202's only line is blocked -> no group entry for it, but the
        # issues carry the context and nothing vanished silently
        assert not blocked or blocked[0]["blocked"]
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PACKING_PREPARATION_WITH_ISSUES)

    def test_zero_eligible_lines_refused(self, tmp_path):
        job_id = two_destination_job(tmp_path)
        enrich(job_id, records=[])          # nothing matches -> all blocked
        with pytest.raises(JobError):
            pk.prepare_packing(job_id)
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PACKING_PREPARATION_FAILED)


# --- destination grouping ---------------------------------------------------------

class TestDestinations:
    def test_split_and_first_appearance_order(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        groups = prepared["destinations"]
        assert [g["destination_code"] for g in groups] == ["ZZOHK101",
                                                           "ZZOHK202"]
        assert [g["destination_sequence"] for g in groups] == [1, 2]
        assert groups[0]["destination_name"]        # reviewed effective name

    def test_same_destination_combines_across_files(self, tmp_path):
        pdf_a = note_pdf(tmp_path / "sa.pdf", [{"rows": (ROW_1,),
                                                "carton_total": None}])
        pdf_b = note_pdf(tmp_path / "sb.pdf", [
            {"rows": (ROW_2,), "carton_total": None, "carton": "001",
             "header": {"dn": "ZZWHKM11-OZSO202606049999"}}])
        uploads = [("a.pdf", Path(pdf_a).read_bytes()),
                   ("b.pdf", Path(pdf_b).read_bytes())]
        validated, _ = tjobs.validate_transfer_uploads(uploads)
        job_id = tjobs.create_transfer_job(uploads, validated)
        extraction.run_extraction(job_id, use_default_adapter=False)
        rv.save_review(job_id, rv.get_or_create_review(job_id))
        rv.approve_review(job_id)
        enrich(job_id, records=ALL_RECORDS)
        prepared = pk.prepare_packing(job_id)
        assert len(prepared["destinations"]) == 1
        group = prepared["destinations"][0]
        assert group["source_carton_count"] == 2    # one carton per file
        assert len(group["source_delivery_notes"]) == 2


# --- carton identity, ordering, resequencing --------------------------------------

class TestCartons:
    def test_reused_original_001_stays_distinct_and_resequences(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        g1, g2 = prepared["destinations"]
        assert [m["generated_carton_number"]
                for m in g1["carton_mappings"]] == ["001", "002"]
        assert [m["original_carton_number"]
                for m in g1["carton_mappings"]] == ["001", "002"]
        # second destination restarts at 001; original 001 from file B is a
        # DIFFERENT source carton than file A's 001
        assert [m["generated_carton_number"]
                for m in g2["carton_mappings"]] == ["001"]
        assert g2["carton_mappings"][0]["original_carton_number"] == "001"
        assert (g2["carton_mappings"][0]["source_carton_key"]["source_file"]
                == "second.pdf")
        keys = {m["source_carton_key"]["carton_entity_id"]
                for g in prepared["destinations"]
                for m in g["carton_mappings"]}
        assert len(keys) == 3                       # three distinct cartons

    def test_upload_and_page_order_control_carton_order(self, tmp_path):
        # name files so filename sorting would INVERT the order
        pdf_a = note_pdf(tmp_path / "zz.pdf", [
            {"page_no": 1, "carton": "010", "rows": (ROW_1,),
             "carton_total": None},
            {"page_no": 2, "carton": "003", "rows": (ROW_2,),
             "carton_total": None}])
        uploads = [("zz-last-name.pdf", Path(pdf_a).read_bytes())]
        validated, _ = tjobs.validate_transfer_uploads(uploads)
        job_id = tjobs.create_transfer_job(uploads, validated)
        extraction.run_extraction(job_id, use_default_adapter=False)
        rv.save_review(job_id, rv.get_or_create_review(job_id))
        rv.approve_review(job_id)
        enrich(job_id, records=ALL_RECORDS)
        prepared = pk.prepare_packing(job_id)
        mappings = prepared["destinations"][0]["carton_mappings"]
        # page order wins: original 010 (page 1) before 003 (page 2)
        assert [m["original_carton_number"] for m in mappings] == ["010",
                                                                  "003"]
        assert [m["generated_carton_number"] for m in mappings] == ["001",
                                                                    "002"]

    def test_deterministic_rerun(self, tmp_path):
        job_id = prepared_job(tmp_path)
        first = pk.prepare_packing(job_id)
        second = pk.prepare_packing(job_id)
        strip = lambda d: {k: v for k, v in d.items()  # noqa: E731
                           if k not in ("created_at", "updated_at")}
        assert strip(first) == strip(second)


class TestResequencing:
    def test_pad_and_growth_past_999(self):
        config = pk.PackingPreparationConfig()
        assert pk.format_carton_number(1, config) == "001"
        assert pk.format_carton_number(999, config) == "999"
        assert pk.format_carton_number(1000, config) == "1000"
        assert pk.format_carton_number(1001, config) == "1001"

    def test_configurable_start_and_width(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PACKING_CARTON_START", "5")
        monkeypatch.setenv("PACKING_CARTON_PAD_WIDTH", "4")
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        assert (prepared["destinations"][0]["carton_mappings"][0]
                ["generated_carton_number"] == "0005")

    @pytest.mark.parametrize("name,value", [
        ("PACKING_CARTON_START", "0"),
        ("PACKING_CARTON_PAD_WIDTH", "-3"),
        ("PACKING_CARTON_PAD_WIDTH", "abc"),
        ("PACKING_INVOICE_PREFIX", "a/b"),
        ("PACKING_INVOICE_PREFIX", "../x"),
    ])
    def test_invalid_configuration_rejected(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        assert pk.packing_config_problems()
        with pytest.raises(JobError):
            pk.load_packing_config()


# --- consolidation ----------------------------------------------------------------

class TestConsolidation:
    def test_same_carton_identical_lines_combine(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        g1 = prepared["destinations"][0]
        carton1 = [l for l in g1["prepared_lines"]
                   if l["generated_carton_number"] == "001"]
        merged = next(l for l in carton1
                      if l["product"]["ean"] == EAN_1)
        assert merged["quantity"] == 3              # 1 + 2 summed
        assert merged["source_rows"] == 2
        assert merged["source_line_ids"] == ["D001-C001-L001",
                                             "D001-C001-L002"]
        assert len(merged["sources"]) == 2          # full traceability
        other = next(l for l in carton1 if l["product"]["ean"] == EAN_2)
        assert other["quantity"] == 2 and other["source_rows"] == 1
        assert prepared["summary"]["consolidated_rows"] == 1

    def test_cross_carton_and_cross_destination_never_merge(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        g1 = prepared["destinations"][0]
        carton2 = [l for l in g1["prepared_lines"]
                   if l["generated_carton_number"] == "002"]
        assert len(carton2) == 1
        assert carton2[0]["product"]["ean"] == EAN_1   # same product...
        assert carton2[0]["quantity"] == 1             # ...kept separate
        # first-seen order preserved within carton 001
        carton1 = [l for l in g1["prepared_lines"]
                   if l["generated_carton_number"] == "001"]
        assert [l["product"]["ean"] for l in carton1] == [EAN_1, EAN_2]

    def test_different_color_or_size_do_not_combine(self, tmp_path):
        rows = (ROW_1,
                ("2", "ZEHE380331E997", "0210116339264", "TOP - SQ NK BRA",
                 "1400", "E997", "M", "1 PCS"))     # same item, size M
        pdf = note_pdf(tmp_path / "s.pdf", [{"rows": rows,
                                             "carton_total": None}])
        uploads = [("s.pdf", Path(pdf).read_bytes())]
        validated, _ = tjobs.validate_transfer_uploads(uploads)
        job_id = tjobs.create_transfer_job(uploads, validated)
        extraction.run_extraction(job_id, use_default_adapter=False)
        rv.save_review(job_id, rv.get_or_create_review(job_id))
        rv.approve_review(job_id)
        enrich(job_id, records=[
            record_for(EAN_1, "ZEHE380331E997", "E997", "S",
                       "TOP - SQ NK BRA", 1400.00),
            record_for("0210116339264", "ZEHE380331E997", "E997", "M",
                       "TOP - SQ NK BRA", 1400.00)])
        prepared = pk.prepare_packing(job_id)
        lines = prepared["destinations"][0]["prepared_lines"]
        assert len(lines) == 2                      # size differs: no merge

    def test_no_source_line_disappears(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        all_ids = [lid for g in prepared["destinations"]
                   for l in g["prepared_lines"]
                   for lid in l["source_line_ids"]]
        assert sorted(all_ids) == sorted(
            ["D001-C001-L001", "D001-C001-L002", "D001-C001-L003",
             "D001-C002-L001", "D002-C001-L001"])
        assert len(all_ids) == len(set(all_ids))    # each exactly once


# --- delivery invoice numbers -----------------------------------------------------

class TestInvoiceNumbers:
    def test_one_per_destination_unique_and_formatted(self, tmp_path):
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        numbers = [g["delivery_invoice_number"]
                   for g in prepared["destinations"]]
        date = prepared["invoice_date"]
        assert numbers == [f"PL-ZZOHK101-{date}-001",
                           f"PL-ZZOHK202-{date}-002"]
        assert len(set(numbers)) == 2

    def test_stable_across_unchanged_rerun_and_reload(self, tmp_path):
        job_id = prepared_job(tmp_path)
        first = pk.prepare_packing(job_id)
        numbers = [g["delivery_invoice_number"]
                   for g in first["destinations"]]
        again = pk.prepare_packing(job_id)          # unchanged rerun
        assert [g["delivery_invoice_number"]
                for g in again["destinations"]] == numbers
        reloaded = pk.load_preparation(job_id)      # browser refresh
        assert [g["delivery_invoice_number"]
                for g in reloaded["destinations"]] == numbers

    def test_prefix_configurable_and_filename_safe(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setenv("PACKING_INVOICE_PREFIX", "tn-9")
        job_id = prepared_job(tmp_path)
        prepared = pk.prepare_packing(job_id)
        group = prepared["destinations"][0]
        assert group["delivery_invoice_number"].startswith("TN-9-ZZOHK101-")
        name = group["suggested_workbook_filename"]
        assert name.startswith("Packing_List_ZZOHK101_TN-9-")
        assert name.endswith(".xlsx")
        assert "/" not in name and "\\" not in name and ".." not in name


# --- persistence + states ---------------------------------------------------------

class TestPersistence:
    def test_schema_checksums_atomic_reload(self, tmp_path):
        job_id = prepared_job(tmp_path)
        pk.prepare_packing(job_id)
        raw = json.loads(pk.result_path(job_id).read_text())
        assert raw["schema_version"] == 1
        for key in ("extraction_checksum", "review_checksum",
                    "product_lookup_checksum"):
            assert raw[key]
        assert not list(pk.result_path(job_id).parent.glob("*.tmp-*"))
        reloaded = pk.load_preparation(job_id)
        assert reloaded["stale"] is False
        blob = pk.result_path(job_id).read_text()
        for forbidden in ("access_token", "refresh_token", "Authorization",
                          "Bearer", "password"):
            assert forbidden not in blob

    def test_unchanged_rerun_single_result_no_duplication(self, tmp_path):
        job_id = prepared_job(tmp_path)
        pk.prepare_packing(job_id)
        pk.prepare_packing(job_id)
        files = list(pk.result_path(job_id).parent.glob("*.json"))
        assert len(files) == 1
        prepared = pk.load_preparation(job_id)
        assert prepared["summary"]["prepared_lines"] == 4   # not doubled

    def test_changed_source_archives_prior(self, tmp_path):
        job_id = prepared_job(tmp_path)
        pk.prepare_packing(job_id)
        # change the review (stale enrichment), rerun lookup, re-prepare
        review = rv.load_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L003", "quantity",
                            "4")
        rv.save_review(job_id, review)
        rv.approve_review(job_id)
        enrich(job_id)
        prepared = pk.prepare_packing(job_id)
        archived = list(pk.result_path(job_id).parent.glob(
            "result-stale-*.json"))
        assert len(archived) == 1                   # audit copy kept
        assert prepared["destinations"][0]["total_units"] == 8  # 3+4+1

    def test_no_workbook_or_zip_output(self, tmp_path):
        job_id = prepared_job(tmp_path)
        pk.prepare_packing(job_id)
        job_dir = tjobs.transfer_job_dir_for(job_id)
        assert not list(job_dir.rglob("*.xlsx"))
        assert not list(job_dir.rglob("*.zip"))

    def test_stranded_in_progress_retries(self, tmp_path):
        job_id = prepared_job(tmp_path)
        tjobs.update_job_status(job_id,
                                tm.JOB_PACKING_PREPARATION_IN_PROGRESS)
        prepared = pk.prepare_packing(job_id)
        assert prepared["status"] == "complete"

    def test_invalid_transition_rejected(self, tmp_path):
        job_id = two_destination_job(tmp_path)      # READY_FOR_PRODUCT_LOOKUP
        with pytest.raises(JobError):
            tjobs.update_job_status(
                job_id, tm.JOB_PACKING_PREPARATION_COMPLETE)


# --- UI wiring + boundaries (static) ----------------------------------------------

class TestUiAndBoundaries:
    RPAGE = (ROOT / "apps" / "web" / "transfer" / "review_page.py").read_text(
        encoding="utf-8")
    PK = (ROOT / "apps" / "web" / "transfer" / "packing.py").read_text(
        encoding="utf-8")

    def test_prepare_button_gated_by_states(self):
        assert "Prepare Packing Groups" in self.RPAGE
        assert "_PACKING_STATES" in self.RPAGE
        assert "disabled=run_disabled" in self.RPAGE

    def test_required_tables_present(self):
        for marker in ("Destination summary", "Carton mapping",
                       "Prepared lines", "Delivery invoice no.",
                       "Original carton"):
            assert marker in self.RPAGE, marker

    def test_no_excel_zip_or_api_in_packing(self):
        low = self.PK.lower()
        for forbidden in ("openpyxl", "zipfile", "httpx", "plulabel-get",
                          "auth/login", "ensure_access_token",
                          "requests."):
            assert forbidden not in low, forbidden
        # the ONLY xlsx reference is the suggested future filename string
        assert low.count(".xlsx") == 1
        assert "suggested_workbook_filename" in self.PK

    def test_no_download_controls_for_packing(self):
        packing_ui = self.RPAGE.split("_render_packing_section")[-1]
        assert "download_button" not in packing_ui

    def test_upstream_artifacts_never_written(self):
        # packing.py writes only inside packing/; it never opens upstream
        # artifacts for writing
        assert 'RESULT_DIR = "packing"' in self.PK
        for forbidden in ("review.json\", \"w", "extraction/result",
                          "write_review", "save_review", "_write_enrichment",
                          "_write_result", "_write_metadata"):
            assert forbidden not in self.PK, forbidden

    def test_invoice_workflow_untouched(self):
        for name in ("job_manager.py", "worker.py", "app.py"):
            src = (ROOT / "apps" / "web" / name).read_text(encoding="utf-8")
            assert "packing" not in src, name
