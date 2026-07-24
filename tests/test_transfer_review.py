"""Transfer Note review, Build 3: review model, corrections/effective
values, exclusion cascades, deterministic issue resolution, approval gates,
staleness protection, persistence, and UI wiring. Fully offline; synthetic
fixtures only - no product API exists anywhere in this build."""

import json
from pathlib import Path

import pytest

from apps.web.job_manager import JobError
from apps.web.transfer import extraction
from apps.web.transfer import extraction_models as em
from apps.web.transfer import jobs as tjobs
from apps.web.transfer import models as tm
from apps.web.transfer import review as rv
from apps.web.transfer.review_models import (
    CLEAR,
    REVIEW_APPROVED,
    REVIEW_IN_PROGRESS,
    REVIEW_STALE,
    TransferReviewResult,
)

from tests.test_transfer_extraction import ROW_A, ROW_B, note_pdf, plain_pdf

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "transfer-jobs"))
    return tmp_path


def make_job(tmp_path, page_specs=None, name="note.pdf", extract=True):
    pdf = note_pdf(tmp_path / f"src-{name}", page_specs or [{}])
    uploads = [(name, Path(pdf).read_bytes())]
    validated, issues = tjobs.validate_transfer_uploads(uploads)
    assert issues == []
    job_id = tjobs.create_transfer_job(uploads, validated)
    if extract:
        extraction.run_extraction(job_id, use_default_adapter=False)
    return job_id


def get_review(job_id):
    review = rv.get_or_create_review(job_id)
    assert review is not None
    return review


def ev_of(job_id, review):
    return rv.evaluate(extraction.load_result(job_id), review)


# Rows for issue-bearing documents ---------------------------------------------------

ROW_NO_SIZE = ("1", "ZEAA111111E001", "0210000000011", "A THING", "100",
               "E001", "", "1 PCS")
ROW_NO_COLOR = ("2", "ZEAA111111E002", "0210000000012", "B THING", "100",
                "", "M", "1 PCS")
ROW_BAD_QTY = ("3", "ZEAA111111E003", "0210000000013", "C THING", "100",
               "E003", "S", "N/A")
ROW_NO_IDS = ("4", "ZEXXBROKEN99", "", "", "", "", "", "")


# --- review model -----------------------------------------------------------------

class TestReviewModel:
    def test_extraction_converts_to_initial_review(self, tmp_path):
        job_id = make_job(tmp_path, [{"rows": (ROW_A, ROW_B)}])
        review = get_review(job_id)
        assert len(review.headers) == 1
        assert len(review.cartons) == 1
        assert len(review.lines) == 2
        assert review.schema_version == 1
        assert review.status == REVIEW_IN_PROGRESS
        assert review.reviewed_by == "local-user"

    def test_extraction_checksum_saved_and_source_untouched(self, tmp_path):
        job_id = make_job(tmp_path)
        before = extraction.result_path(job_id).read_bytes()
        review = get_review(job_id)
        assert review.extraction_checksum == rv.extraction_checksum(job_id)
        assert extraction.result_path(job_id).read_bytes() == before

    def test_original_and_corrected_stored_separately(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        line = review.lines[0]
        assert line.original["size_code"] == "S"
        rv.apply_correction(review, "line", line.entity_id, "size_code", "M")
        assert line.original["size_code"] == "S"        # frozen
        assert line.corrections["size_code"] == "M"
        assert line.effective("size_code") == "M"

    def test_effective_value_resolution_and_revert(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        line = review.lines[0]
        assert line.effective("color_code") == "E997"   # unchanged
        rv.apply_correction(review, "line", line.entity_id, "color_code",
                            "E001")
        assert line.effective("color_code") == "E001"   # corrected
        # correcting back to the original removes the correction entirely
        rv.apply_correction(review, "line", line.entity_id, "color_code",
                            "E997")
        assert "color_code" not in line.corrections
        assert line.effective("color_code") == "E997"

    def test_explicit_clear_semantics(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        line = review.lines[0]
        rv.apply_correction(review, "line", line.entity_id, "description",
                            CLEAR)
        assert line.effective("description") is None    # deliberately cleared
        assert line.original["description"] == "TOP - SQ NK BRA"
        assert line.corrections["description"] is None
        assert review.changes[-1].cleared is True

    def test_stable_ids_survive_round_trip(self, tmp_path):
        job_id = make_job(tmp_path, [{"rows": (ROW_A, ROW_B)}])
        review = get_review(job_id)
        ids = ([h.entity_id for h in review.headers]
               + [c.entity_id for c in review.cartons]
               + [ln.entity_id for ln in review.lines])
        assert ids == ["D001", "D001-C001", "D001-C001-L001",
                       "D001-C001-L002"]
        reloaded = rv.load_review(job_id)
        assert [ln.entity_id for ln in reloaded.lines] == ids[2:]

    def test_schema_round_trip(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.apply_correction(review, "line", review.lines[0].entity_id,
                            "size_code", "M")
        rv.set_exclusion(review, "line", review.lines[1].entity_id, True,
                         "damaged")
        rv.save_review(job_id, review)
        reloaded = rv.load_review(job_id)
        assert reloaded.as_dict() == review.as_dict()


# --- header review ----------------------------------------------------------------

class TestHeaderReview:
    def test_destination_and_dn_and_date_corrections(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        header = review.headers[0]
        rv.apply_correction(review, "document", header.entity_id,
                            "to_location_code", "zzohk202")
        rv.apply_correction(review, "document", header.entity_id,
                            "delivery_note_number", " DN-NEW-1 ")
        rv.apply_correction(review, "document", header.entity_id,
                            "delivery_date", "2026-07-01")
        assert header.effective("to_location_code") == "ZZOHK202"  # uppercased
        assert header.effective("delivery_note_number") == "DN-NEW-1"  # trimmed
        assert header.effective("delivery_date") == "2026-07-01"
        assert header.original["to_location_code"] == "ZZOHK101"

    def test_document_exclusion_cascades(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.set_exclusion(review, "document", "D001", True, "wrong batch")
        ev = ev_of(job_id, review)
        assert ev.excluded_documents == 1
        assert ev.included_cartons == 0 and ev.excluded_cartons == 1
        assert ev.included_lines == 0 and ev.excluded_lines == 2
        # originals intact; carton/line records still stored
        assert review.cartons[0].excluded is False    # own flag unchanged
        assert extraction.load_result(job_id).documents[0].cartons

    def test_missing_required_header_fields_block_approval(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.apply_correction(review, "document", "D001",
                            "delivery_note_number", CLEAR)
        ev = ev_of(job_id, review)
        assert not ev.can_approve
        assert any("D/N#" in p or "delivery-note" in p
                   for p in ev.approval_problems)

    def test_exclusion_requires_reason(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        with pytest.raises(JobError):
            rv.set_exclusion(review, "document", "D001", True, "  ")


# --- carton review ----------------------------------------------------------------

class TestCartonReview:
    def test_carton_number_correction_keeps_extracted_value(self, tmp_path):
        job_id = make_job(tmp_path, [{"carton": None}])
        review = get_review(job_id)
        carton = review.cartons[0]
        assert carton.original["original_carton_number"] is None
        rv.apply_correction(review, "carton", carton.entity_id,
                            "original_carton_number", "007")
        assert carton.effective("original_carton_number") == "007"
        assert carton.original["original_carton_number"] is None   # audit

    def test_carton_exclusion_cascades_to_lines(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.set_exclusion(review, "carton", "D001-C001", True, "damaged box")
        ev = ev_of(job_id, review)
        assert ev.excluded_cartons == 1
        assert ev.included_lines == 0 and ev.excluded_lines == 2
        assert review.lines[0].excluded is False       # own flag unchanged

    def test_carton_order_unchanged_and_not_reorderable(self, tmp_path):
        job_id = make_job(tmp_path, [
            {"page_no": 1, "carton": "003"},
            {"page_no": 2, "carton": "001"},
        ])
        review = get_review(job_id)
        # order = page order, NOT carton-number order; model has no
        # reordering operation at all
        assert [c.original["original_carton_number"]
                for c in review.cartons] == ["003", "001"]
        assert not hasattr(rv, "reorder_cartons")

    def test_totals_recalculate_from_included_lines(self, tmp_path):
        job_id = make_job(tmp_path)     # qty 1 + 2 = 3
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert ev.carton_effective_totals["D001-C001"] == 3
        rv.set_exclusion(review, "line", "D001-C001-L002", True, "not found")
        ev = ev_of(job_id, review)
        assert ev.carton_effective_totals["D001-C001"] == 1
        assert ev.total_effective_units == 1


# --- line review ------------------------------------------------------------------

class TestLineReview:
    def test_correct_missing_size_and_color(self, tmp_path):
        job_id = make_job(tmp_path,
                          [{"rows": (ROW_NO_SIZE, ROW_NO_COLOR),
                            "carton_total": None}])
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        warn_codes = {w["code"] for w in ev.unresolved_warnings}
        assert em.MISSING_SIZE in warn_codes
        assert em.MISSING_COLOR in warn_codes
        rv.apply_correction(review, "line", "D001-C001-L001", "size_code",
                            "s")
        rv.apply_correction(review, "line", "D001-C001-L002", "color_code",
                            "e002")
        ev = ev_of(job_id, review)
        warn_codes = {w["code"] for w in ev.unresolved_warnings}
        assert em.MISSING_SIZE not in warn_codes
        assert em.MISSING_COLOR not in warn_codes
        assert review.lines[0].effective("size_code") == "S"

    def test_ean_correction_preserves_leading_zeros(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "ean",
                            "0099887766554")
        line = review.lines[0]
        assert line.effective("ean") == "0099887766554"
        assert isinstance(line.effective("ean"), str)
        assert rv.valid_ean(line.effective("ean"))

    def test_lookup_ready_via_ean_or_fallback(self, tmp_path):
        job_id = make_job(tmp_path,
                          [{"rows": (ROW_NO_SIZE,), "carton_total": None}])
        review = get_review(job_id)
        line = review.lines[0]
        ev = ev_of(job_id, review)
        assert ev.lines[line.entity_id].lookup_ready       # valid EAN
        # break the EAN -> not ready (fallback lacks size)
        rv.apply_correction(review, "line", line.entity_id, "ean", "BAD-EAN")
        ev = ev_of(job_id, review)
        assert not ev.lines[line.entity_id].lookup_ready
        # supply the missing size -> fallback Item+Color+Size becomes valid
        rv.apply_correction(review, "line", line.entity_id, "size_code", "S")
        ev = ev_of(job_id, review)
        assert ev.lines[line.entity_id].lookup_ready

    def test_invalid_quantity_blocks_until_corrected(self, tmp_path):
        job_id = make_job(tmp_path,
                          [{"rows": (ROW_BAD_QTY,), "carton_total": None}])
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert not ev.can_approve
        assert any(b["code"] == em.INVALID_QUANTITY
                   for b in ev.unresolved_blocking)
        rv.apply_correction(review, "line", "D001-C001-L001", "quantity", "4")
        ev = ev_of(job_id, review)
        assert not any(b["code"] == em.INVALID_QUANTITY
                       for b in ev.unresolved_blocking)
        assert ev.total_effective_units == 4

    def test_quantity_correction_updates_totals(self, tmp_path):
        job_id = make_job(tmp_path)     # 1 + 2 = 3, printed total 3
        review = get_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "quantity", "5")
        ev = ev_of(job_id, review)
        assert ev.carton_effective_totals["D001-C001"] == 7
        assert ev.document_effective_totals["D001"] == 7

    def test_exclusion_requires_reason_and_is_reversible(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        with pytest.raises(JobError):
            rv.set_exclusion(review, "line", "D001-C001-L001", True, None)
        rv.set_exclusion(review, "line", "D001-C001-L001", True, "torn label")
        assert review.lines[0].exclusion_reason == "torn label"
        rv.set_exclusion(review, "line", "D001-C001-L001", False)
        assert review.lines[0].excluded is False
        assert review.lines[0].exclusion_reason is None

    def test_malformed_row_retained_and_duplicates_stay_separate(self, tmp_path):
        dup = ("2", "ZEAA111111E001", "0210000000011", "A THING", "100",
               "E001", "S", "1 PCS")
        job_id = make_job(tmp_path, [{
            "rows": (ROW_NO_SIZE, dup, ROW_NO_IDS), "carton_total": None}])
        review = get_review(job_id)
        assert len(review.lines) == 3                   # nothing dropped
        # duplicate-looking rows are separate reviewable entities
        assert review.lines[0].entity_id != review.lines[1].entity_id
        ev = ev_of(job_id, review)
        assert not ev.lines[review.lines[0].entity_id].effective_excluded
        assert not ev.lines[review.lines[1].entity_id].effective_excluded


# --- issue resolution -------------------------------------------------------------

class TestIssueResolution:
    def test_save_alone_resolves_nothing(self, tmp_path):
        job_id = make_job(tmp_path,
                          [{"rows": (ROW_BAD_QTY,), "carton_total": None}])
        review = get_review(job_id)
        before = ev_of(job_id, review)
        rv.save_review(job_id, review)
        rv.save_review(job_id, rv.load_review(job_id))
        after = ev_of(job_id, rv.load_review(job_id))
        assert len(after.unresolved_blocking) == len(before.unresolved_blocking)

    def test_missing_destination_resolved_by_correction(self, tmp_path):
        job_id = make_job(tmp_path, [{"to_loc_override": ""}])
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert any(b["code"] == em.MISSING_DESTINATION
                   for b in ev.unresolved_blocking)
        rv.apply_correction(review, "document", "D001", "to_location_code",
                            "ZZOHK101")
        ev = ev_of(job_id, review)
        assert not any(b["code"] == em.MISSING_DESTINATION
                       for b in ev.unresolved_blocking)
        assert ev.resolved_issue_count >= 1

    def test_missing_carton_no_resolved_by_correction_or_exclusion(self, tmp_path):
        job_id = make_job(tmp_path, [{"carton": None}])
        review = get_review(job_id)
        carton_id = review.cartons[0].entity_id
        ev = ev_of(job_id, review)
        assert any(b["code"] == em.MISSING_CARTON_NO
                   for b in ev.unresolved_blocking)
        rv.apply_correction(review, "carton", carton_id,
                            "original_carton_number", "005")
        ev = ev_of(job_id, review)
        assert not any(b["code"] == em.MISSING_CARTON_NO
                       for b in ev.unresolved_blocking)

    def test_unrecognized_document_resolved_only_by_exclusion(self, tmp_path):
        pdf = plain_pdf(tmp_path / "x.pdf")
        uploads = [("x.pdf", Path(pdf).read_bytes())]
        validated, issues = tjobs.validate_transfer_uploads(uploads)
        job_id = tjobs.create_transfer_job(uploads, validated)
        extraction.run_extraction(job_id, use_default_adapter=False)
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert any(b["code"] == em.UNRECOGNIZED_DOCUMENT
                   for b in ev.unresolved_blocking)
        rv.set_exclusion(review, "document", "D001", True, "not a note")
        ev = ev_of(job_id, review)
        assert not any(b["code"] == em.UNRECOGNIZED_DOCUMENT
                       for b in ev.unresolved_blocking)

    def test_carton_total_mismatch_recalculates(self, tmp_path):
        job_id = make_job(tmp_path, [{"carton_total": "9 UNIT"}])  # calc 3
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert any(b["code"] == em.CARTON_TOTAL_MISMATCH
                   for b in ev.unresolved_blocking)
        # correcting a quantity so included lines sum to the printed total
        rv.apply_correction(review, "line", "D001-C001-L002", "quantity", "8")
        ev = ev_of(job_id, review)
        assert ev.carton_effective_totals["D001-C001"] == 9
        assert not any(b["code"] == em.CARTON_TOTAL_MISMATCH
                       for b in ev.unresolved_blocking)

    def test_document_total_mismatch_recalculates(self, tmp_path):
        job_id = make_job(tmp_path, [{"grand_total": "9 UNIT"}])   # calc 3
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert any(b["code"] == em.DOCUMENT_TOTAL_MISMATCH
                   for b in ev.unresolved_blocking)
        rv.apply_correction(review, "line", "D001-C001-L001", "quantity", "7")
        ev = ev_of(job_id, review)
        assert ev.document_effective_totals["D001"] == 9
        assert not any(b["code"] == em.DOCUMENT_TOTAL_MISMATCH
                       for b in ev.unresolved_blocking)

    def test_warnings_remain_visible_but_do_not_block(self, tmp_path):
        job_id = make_job(tmp_path,
                          [{"rows": (ROW_NO_SIZE,), "carton_total": None}])
        review = get_review(job_id)
        ev = ev_of(job_id, review)
        assert any(w["code"] == em.MISSING_SIZE
                   for w in ev.unresolved_warnings)
        # documented rule: valid EAN => lookup-ready; missing size stays a
        # warning and never blocks approval
        assert not any("size" in p.lower() for p in ev.approval_problems)


# --- approval ---------------------------------------------------------------------

class TestApproval:
    def test_valid_review_approves_to_ready_for_product_lookup(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.save_review(job_id, review)
        approved = rv.approve_review(job_id)
        assert approved.status == REVIEW_APPROVED
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_READY_FOR_PRODUCT_LOOKUP)
        # persists across reload
        assert rv.load_review(job_id).status == REVIEW_APPROVED

    def test_unresolved_blocker_prevents_approval(self, tmp_path):
        job_id = make_job(tmp_path,
                          [{"rows": (ROW_BAD_QTY,), "carton_total": None}])
        get_review(job_id)
        with pytest.raises(JobError):
            rv.approve_review(job_id)
        assert (tjobs.load_transfer_job(job_id).status
                != tm.JOB_READY_FOR_PRODUCT_LOOKUP)

    def test_zero_included_lines_prevents_approval(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        for ln in review.lines:
            rv.set_exclusion(review, "line", ln.entity_id, True, "all bad")
        rv.save_review(job_id, review)
        with pytest.raises(JobError):
            rv.approve_review(job_id)

    def test_reopen_returns_to_review_in_progress(self, tmp_path):
        job_id = make_job(tmp_path)
        rv.save_review(job_id, get_review(job_id))
        rv.approve_review(job_id)
        rv.reopen_review(job_id)
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_REVIEW_IN_PROGRESS)
        assert rv.load_review(job_id).status == REVIEW_IN_PROGRESS

    def test_reject_requires_reason_and_sets_state(self, tmp_path):
        job_id = make_job(tmp_path)
        rv.save_review(job_id, get_review(job_id))
        rv.begin_review(job_id)
        with pytest.raises(JobError):
            rv.reject_review(job_id, "")
        rv.reject_review(job_id, "wrong shipment")
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_REVIEW_REJECTED)

    def test_no_api_client_exists_in_build3(self):
        for name in ("review.py", "review_models.py", "review_page.py"):
            src = (ROOT / "apps" / "web" / "transfer" / name).read_text(
                encoding="utf-8").lower()
            for forbidden in ("plulabel", "auth/login", "auth/refresh",
                              "access_token", "httpx", "requests.",
                              "openpyxl", "openrouter", "gemini", "claude"):
                assert forbidden not in src, f"{name}: {forbidden}"


# --- staleness --------------------------------------------------------------------

class TestStaleness:
    def test_changed_extraction_marks_review_stale(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        old_checksum = review.extraction_checksum
        extraction.run_extraction(job_id, use_default_adapter=False)  # rerun
        assert rv.extraction_checksum(job_id) != old_checksum
        reloaded = rv.load_review(job_id)
        assert reloaded.status == REVIEW_STALE

    def test_stale_review_cannot_be_approved(self, tmp_path):
        job_id = make_job(tmp_path)
        rv.save_review(job_id, get_review(job_id))
        extraction.run_extraction(job_id, use_default_adapter=False)
        with pytest.raises(JobError, match="stale"):
            rv.approve_review(job_id)

    def test_rebuild_archives_previous_review(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "size_code",
                            "M")
        rv.save_review(job_id, review)
        extraction.run_extraction(job_id, use_default_adapter=False)
        assert rv.load_review(job_id).status == REVIEW_STALE
        fresh = rv.rebuild_review(job_id)
        assert fresh.status == REVIEW_IN_PROGRESS
        assert "size_code" not in fresh.lines[0].corrections
        archived = list(rv.review_path(job_id).parent.glob(
            "review-stale-*.json"))
        assert len(archived) == 1                      # audit copy kept
        old = json.loads(archived[0].read_text())
        assert old["lines"][0]["corrections"] == {"size_code": "M"}

    def test_retry_after_stale_is_safe(self, tmp_path):
        job_id = make_job(tmp_path)
        rv.save_review(job_id, get_review(job_id))
        extraction.run_extraction(job_id, use_default_adapter=False)
        rv.rebuild_review(job_id)
        rv.save_review(job_id, rv.load_review(job_id))
        approved = rv.approve_review(job_id)
        assert approved.status == REVIEW_APPROVED


# --- persistence ------------------------------------------------------------------

class TestPersistence:
    def test_atomic_write_and_refresh_recovery(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "size_code",
                            "XL")
        rv.save_review(job_id, review)
        assert not list(rv.review_path(job_id).parent.glob("*.tmp-*"))
        # fresh load (a browser refresh) sees the correction
        recovered = rv.get_or_create_review(job_id)
        assert recovered.lines[0].effective("size_code") == "XL"

    def test_repeated_save_does_not_duplicate_history(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "size_code",
                            "M")
        assert not rv.apply_correction(review, "line", "D001-C001-L001",
                                       "size_code", "M")   # no-op
        rv.save_review(job_id, review)
        rv.save_review(job_id, rv.load_review(job_id))
        final = rv.load_review(job_id)
        size_changes = [c for c in final.changes if c.field == "size_code"]
        assert len(size_changes) == 1

    def test_concurrent_stale_form_save_rejected(self, tmp_path):
        job_id = make_job(tmp_path)
        review_a = get_review(job_id)
        stamp_a = review_a.updated_at
        review_b = rv.load_review(job_id)
        rv.save_review(job_id, review_b,
                       expected_updated_at=review_b.updated_at)
        with pytest.raises(JobError, match="another save"):
            rv.save_review(job_id, review_a, expected_updated_at=stamp_a)

    def test_malformed_review_fails_safely(self, tmp_path):
        job_id = make_job(tmp_path)
        get_review(job_id)
        rv.review_path(job_id).write_text("{ not json !")
        assert rv.load_review(job_id) is None           # safe failure
        # extraction data untouched and a fresh review can be built
        assert extraction.load_result(job_id) is not None
        assert rv.get_or_create_review(job_id) is not None

    def test_invoice_isolation_intact(self, tmp_path):
        from apps.web import job_manager
        job_id = make_job(tmp_path)
        rv.save_review(job_id, get_review(job_id))
        assert not job_manager.JOB_ID_RE.match(job_id)
        with pytest.raises(job_manager.JobError):
            job_manager.job_dir_for(job_id)


# --- editor-row application (UI convention) ---------------------------------------

class TestEditorRows:
    def test_empty_cell_never_clears(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rows = [{"entity_id": "D001-C001-L001", "size_code": ""}]
        changed = rv.apply_editor_rows(review, "line", rows, ("size_code",))
        assert changed == 0
        assert review.lines[0].effective("size_code") == "S"

    def test_clear_token_clears_and_edit_corrects(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rows = [{"entity_id": "D001-C001-L001", "size_code": "<clear>",
                 "color_code": "E123"}]
        changed = rv.apply_editor_rows(review, "line", rows,
                                       ("size_code", "color_code"))
        assert changed == 2
        assert review.lines[0].effective("size_code") is None
        assert review.lines[0].effective("color_code") == "E123"

    def test_editor_exclusion_requires_reason(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rows = [{"entity_id": "D001-C001-L001", "excluded": True,
                 "exclusion_reason": ""}]
        with pytest.raises(JobError):
            rv.apply_editor_rows(review, "line", rows, ())

    def test_unchanged_effective_cell_is_noop(self, tmp_path):
        job_id = make_job(tmp_path)
        review = get_review(job_id)
        rows = [{"entity_id": "D001-C001-L001", "size_code": "S",
                 "excluded": False, "exclusion_reason": ""}]
        assert rv.apply_editor_rows(review, "line", rows,
                                    ("size_code",)) == 0


# --- UI wiring (static) -----------------------------------------------------------

class TestUiWiring:
    PAGE = (ROOT / "apps" / "web" / "transfer" / "page.py").read_text(
        encoding="utf-8")
    RPAGE = (ROOT / "apps" / "web" / "transfer" / "review_page.py").read_text(
        encoding="utf-8")

    def test_review_shown_for_reviewable_states(self):
        assert "REVIEWABLE_JOB_STATUSES" in self.PAGE
        assert "render_review_section" in self.PAGE

    def test_original_and_effective_visible_and_filters(self):
        assert "Original To Loc." in self.RPAGE
        assert "Original size" in self.RPAGE
        assert '"Changed", "Blocking issues", "Warnings", "Excluded"' in \
            self.RPAGE.replace("\n", " ") or "Blocking issues" in self.RPAGE

    def test_save_and_gated_approve_present(self):
        assert "Save Review" in self.RPAGE
        assert "Approve for Product Lookup" in self.RPAGE
        assert "disabled=approve_disabled" in self.RPAGE

    def test_no_api_or_excel_controls(self):
        # Build 7 added a sanctioned workbook/download section; the review
        # sections themselves must stay free of API/Excel controls.
        review_part = self.RPAGE.split("def _render_workbook_section")[0]
        low = (self.PAGE + review_part).lower()
        for forbidden in ("plulabel", "access_token", "auth/login",
                          "openpyxl", "download_button", "resequenc"):
            assert forbidden not in low, forbidden

    def test_session_keys_remain_transfer_prefixed(self):
        import re
        keys = set()
        for src in (self.PAGE, self.RPAGE):
            keys |= set(re.findall(r'session_state\[["\']([^"\']+)["\']\]',
                                   src))
            keys |= set(re.findall(r'session_state\.get\(["\']([^"\']+)["\']',
                                   src))
        assert keys and all(k.startswith("transfer_") for k in keys), keys
