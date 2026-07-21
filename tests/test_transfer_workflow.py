"""Transfer Note Packing List workflow, Build 1 (feature flag, validation,
job persistence, and isolation from the invoice workflow). Offline; temp
dirs and synthetic in-memory PDFs only."""

import json
import re
from pathlib import Path

import fitz
import pytest

from apps.web import job_manager
from apps.web.transfer import jobs as tjobs
from apps.web.transfer import models as tm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "transfer-jobs"))
    monkeypatch.delenv("TRANSFER_WORKFLOW_ENABLED", raising=False)
    monkeypatch.delenv("TRANSFER_MAX_FILES", raising=False)
    monkeypatch.delenv("TRANSFER_MAX_FILE_MB", raising=False)
    monkeypatch.delenv("TRANSFER_MAX_PAGES", raising=False)
    return tmp_path


def pdf_bytes(pages: int = 1, marker: str = "TRANSFER NOTE") -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((50, 72), f"{marker} page {i + 1}", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


# --- configuration ----------------------------------------------------------------

class TestConfiguration:
    def test_feature_flag_defaults_off(self):
        assert tjobs.workflow_enabled() is False

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("1", True), ("yes", True), ("on", True),
        ("TRUE", True), ("false", False), ("0", False), ("no", False),
        ("off", False), ("nonsense", False), ("", False),
    ])
    def test_feature_flag_parses_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv("TRANSFER_WORKFLOW_ENABLED", raw)
        assert tjobs.workflow_enabled() is expected

    def test_limits_defaults(self):
        assert tjobs.transfer_limits() == {
            "max_files": 50, "max_file_mb": 50, "max_pages": 500}

    def test_limits_load_from_env(self, monkeypatch):
        monkeypatch.setenv("TRANSFER_MAX_FILES", "3")
        monkeypatch.setenv("TRANSFER_MAX_FILE_MB", "2")
        monkeypatch.setenv("TRANSFER_MAX_PAGES", "10")
        assert tjobs.transfer_limits() == {
            "max_files": 3, "max_file_mb": 2, "max_pages": 10}

    def test_bad_limit_values_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("TRANSFER_MAX_FILES", "zero")
        monkeypatch.setenv("TRANSFER_MAX_PAGES", "-5")
        limits = tjobs.transfer_limits()
        assert limits["max_files"] == 50 and limits["max_pages"] == 500


# --- workflow isolation (static + structural) --------------------------------------

class TestWorkflowIsolation:
    APP = (ROOT / "apps" / "web" / "app.py").read_text(encoding="utf-8")
    PAGE = (ROOT / "apps" / "web" / "transfer" / "page.py").read_text(
        encoding="utf-8")

    def test_invoice_workflow_remains_default(self):
        # Selector order: invoice first (the radio default), and the whole
        # selector is gated on the feature flag.
        assert "[WORKFLOW_INVOICE, WORKFLOW_TRANSFER]" in self.APP
        assert "if transfer_jobs.workflow_enabled():" in self.APP

    def test_transfer_page_only_rendered_behind_flag_and_stops(self):
        gated = self.APP.split("workflow_enabled():", 1)[1]
        assert "transfer_page.render()" in gated
        assert "st.stop()" in gated
        # Invoice page code still present after the selector block.
        assert 'st.title("Invoice Extractor Pilot")' in self.APP

    def test_transfer_session_keys_are_prefixed(self):
        # Every session_state key the transfer page touches must be
        # transfer-prefixed - no collisions with invoice keys.
        keys = set(re.findall(r'session_state\[["\']([^"\']+)["\']\]',
                              self.PAGE))
        keys |= set(re.findall(r'session_state\.get\(["\']([^"\']+)["\']',
                               self.PAGE))
        assert keys, "expected session keys in transfer page"
        assert all(k.startswith("transfer_") for k in keys), keys
        for invoice_key in ("job_id", "plans", "uploader_gen",
                            "new_batch_msg"):
            assert invoice_key not in keys

    def test_transfer_module_never_touches_invoice_roots_or_lock(self):
        src = (ROOT / "apps" / "web" / "transfer" / "jobs.py").read_text(
            encoding="utf-8")
        for forbidden in ("acquire_lock", "spawn_worker", "WEB_JOBS_DIR",
                          "status.json"):
            assert forbidden not in src, forbidden
        # Only generic helpers may be imported from the invoice job manager.
        imports = re.findall(r"from apps\.web\.job_manager import ([^\n]+)",
                             src)
        assert imports == ["JobError, sanitize_filename, utc_now"]

    def test_no_ai_api_or_excel_in_transfer_modules(self):
        for name in ("jobs.py", "models.py", "page.py"):
            src = (ROOT / "apps" / "web" / "transfer" / name).read_text(
                encoding="utf-8").lower()
            for forbidden in ("openrouter", "gemini", "claude", "httpx",
                              "openpyxl", "plulabel", "api_gateway",
                              "access_token"):
                assert forbidden not in src, f"{name} contains {forbidden}"


# --- file validation ---------------------------------------------------------------

def _codes(issues):
    return [i.code for i in issues]


class TestValidation:
    def test_valid_single_pdf(self):
        files, issues = tjobs.validate_transfer_uploads(
            [("note.pdf", pdf_bytes(2))])
        assert issues == []
        (f,) = files
        assert f.sequence == 1 and f.status == tm.FILE_VALIDATED
        assert f.page_count == 2 and f.sha256 and f.size_bytes > 0
        assert f.stored_name == "001-note.pdf"

    def test_valid_multiple_pdfs_preserve_upload_order(self):
        uploads = [("zebra.pdf", pdf_bytes(1, "z")),
                   ("alpha.pdf", pdf_bytes(1, "a")),
                   ("mid.pdf", pdf_bytes(1, "m"))]
        files, issues = tjobs.validate_transfer_uploads(uploads)
        assert issues == []
        # Explicit sequence follows UPLOAD order, never filename order.
        assert [(f.sequence, f.original_name) for f in files] == [
            (1, "zebra.pdf"), (2, "alpha.pdf"), (3, "mid.pdf")]
        assert [f.stored_name for f in files] == [
            "001-zebra.pdf", "002-alpha.pdf", "003-mid.pdf"]

    def test_no_files(self):
        files, issues = tjobs.validate_transfer_uploads([])
        assert files == [] and _codes(issues) == [tm.NO_FILES]

    def test_non_pdf_extension(self):
        files, issues = tjobs.validate_transfer_uploads(
            [("notes.txt", b"hello")])
        assert _codes(issues) == [tm.UNSUPPORTED_FILE_TYPE]
        assert files[0].status == tm.FILE_INVALID and files[0].messages

    def test_pdf_extension_with_non_pdf_content(self):
        files, issues = tjobs.validate_transfer_uploads(
            [("fake.pdf", b"not a pdf at all")])
        assert _codes(issues) == [tm.UNSUPPORTED_FILE_TYPE]

    def test_empty_file(self):
        _, issues = tjobs.validate_transfer_uploads([("empty.pdf", b"")])
        assert _codes(issues) == [tm.EMPTY_FILE]

    def test_malformed_pdf(self):
        broken = b"%PDF-1.7 then absolutely nothing valid"
        files, issues = tjobs.validate_transfer_uploads([("bad.pdf", broken)])
        assert _codes(issues) == [tm.INVALID_PDF]
        assert files[0].status == tm.FILE_INVALID

    def test_duplicate_content_detected_by_checksum(self):
        same = pdf_bytes(1)
        files, issues = tjobs.validate_transfer_uploads(
            [("a.pdf", same), ("b.pdf", same)])
        assert _codes(issues) == [tm.DUPLICATE_FILE]
        assert issues[0].sequence == 2          # the second copy is flagged
        assert files[0].status == tm.FILE_VALIDATED
        assert files[1].status == tm.FILE_INVALID

    def test_file_count_limit(self, monkeypatch):
        monkeypatch.setenv("TRANSFER_MAX_FILES", "2")
        uploads = [(f"n{i}.pdf", pdf_bytes(1, f"m{i}")) for i in range(3)]
        _, issues = tjobs.validate_transfer_uploads(uploads)
        assert tm.TOO_MANY_FILES in _codes(issues)

    def test_file_size_limit(self, monkeypatch):
        monkeypatch.setenv("TRANSFER_MAX_FILE_MB", "1")
        big = pdf_bytes(1) + b"0" * (1024 * 1024 + 1)
        _, issues = tjobs.validate_transfer_uploads([("big.pdf", big)])
        assert tm.FILE_TOO_LARGE in _codes(issues)

    def test_total_page_limit(self, monkeypatch):
        monkeypatch.setenv("TRANSFER_MAX_PAGES", "3")
        uploads = [("a.pdf", pdf_bytes(2, "a")), ("b.pdf", pdf_bytes(2, "b"))]
        _, issues = tjobs.validate_transfer_uploads(uploads)
        assert tm.TOO_MANY_PAGES in _codes(issues)

    def test_multiple_problems_all_reported(self):
        files, issues = tjobs.validate_transfer_uploads(
            [("ok.pdf", pdf_bytes(1)), ("nope.txt", b"x"), ("void.pdf", b"")])
        assert sorted(_codes(issues)) == sorted(
            [tm.UNSUPPORTED_FILE_TYPE, tm.EMPTY_FILE])
        assert [f.status for f in files] == [
            tm.FILE_VALIDATED, tm.FILE_INVALID, tm.FILE_INVALID]

    def test_issue_dicts_are_machine_readable(self):
        _, issues = tjobs.validate_transfer_uploads([("x.txt", b"data")])
        d = issues[0].as_dict()
        assert d["code"] in tm.VALIDATION_CODES
        assert d["sequence"] == 1 and d["message"]


# --- job creation + persistence ----------------------------------------------------

def _make_job(uploads=None):
    uploads = uploads or [("first.pdf", pdf_bytes(2, "one")),
                          ("second.pdf", pdf_bytes(3, "two"))]
    validated, issues = tjobs.validate_transfer_uploads(uploads)
    assert issues == []
    return tjobs.create_transfer_job(uploads, validated), uploads


class TestJobPersistence:
    def test_job_created_with_correct_type_and_status(self):
        job_id, _ = _make_job()
        assert tjobs.TJOB_ID_RE.match(job_id)
        job = tjobs.load_transfer_job(job_id)
        assert job.job_type == "transfer_packing"
        assert job.status == tm.JOB_READY_FOR_EXTRACTION
        assert job.total_pages == 5 and len(job.files) == 2

    def test_upload_sequence_and_original_names_persisted(self):
        job_id, _ = _make_job()
        job = tjobs.load_transfer_job(job_id)
        assert [(f.sequence, f.original_name, f.stored_name)
                for f in job.files] == [
            (1, "first.pdf", "001-first.pdf"),
            (2, "second.pdf", "002-second.pdf")]

    def test_stored_files_exist_under_job_input_only(self, roots):
        job_id, uploads = _make_job()
        job_dir = tjobs.transfer_job_dir_for(job_id)
        stored = sorted(p.name for p in (job_dir / "input").iterdir())
        assert stored == ["001-first.pdf", "002-second.pdf"]
        assert (job_dir / "input" / "001-first.pdf").read_bytes() == uploads[0][1]
        # everything stays inside the transfer root
        assert str(job_dir).startswith(str(tjobs.transfer_jobs_root()))

    def test_hostile_filename_cannot_escape_job_dir(self, roots):
        uploads = [("../../../evil.pdf", pdf_bytes(1))]
        validated, issues = tjobs.validate_transfer_uploads(uploads)
        assert issues == []
        job_id = tjobs.create_transfer_job(uploads, validated)
        job_dir = tjobs.transfer_job_dir_for(job_id)
        (stored,) = (job_dir / "input").iterdir()
        assert ".." not in stored.name and "/" not in stored.name
        assert not (roots / "evil.pdf").exists()

    def test_metadata_survives_reload(self):
        job_id, _ = _make_job()
        first = tjobs.load_transfer_job(job_id).as_dict()
        again = tjobs.load_transfer_job(job_id).as_dict()
        assert first == again
        raw = json.loads((tjobs.transfer_job_dir_for(job_id)
                          / "transfer_job.json").read_text())
        assert raw["schema_version"] == 1
        assert raw["summary"]["file_count"] == 2
        assert raw["extraction"] == {} and raw["outputs"] == {}

    def test_two_jobs_do_not_collide(self):
        a, _ = _make_job()
        b, _ = _make_job([("other.pdf", pdf_bytes(1, "x"))])
        assert a != b
        assert tjobs.load_transfer_job(a) and tjobs.load_transfer_job(b)

    def test_cannot_create_job_from_invalid_selection(self):
        uploads = [("bad.txt", b"nope")]
        validated, issues = tjobs.validate_transfer_uploads(uploads)
        assert issues
        with pytest.raises(job_manager.JobError):
            tjobs.create_transfer_job(uploads, validated)

    def test_newest_transfer_job_recovery(self):
        assert tjobs.newest_transfer_job_id() is None
        job_id, _ = _make_job()
        assert tjobs.newest_transfer_job_id() == job_id


class TestCrossWorkflowIsolation:
    def test_invoice_loader_rejects_transfer_job_ids(self):
        job_id, _ = _make_job()
        assert not job_manager.JOB_ID_RE.match(job_id)
        with pytest.raises(job_manager.JobError):
            job_manager.job_dir_for(job_id)

    def test_transfer_loader_rejects_invoice_job_ids(self):
        invoice_id = job_manager.new_job_id()
        assert not tjobs.TJOB_ID_RE.match(invoice_id)
        with pytest.raises(job_manager.JobError):
            tjobs.transfer_job_dir_for(invoice_id)
        assert tjobs.load_transfer_job(invoice_id) is None

    def test_roots_are_separate(self):
        job_id, _ = _make_job()
        invoice_root = job_manager.jobs_root()
        assert tjobs.transfer_jobs_root() != invoice_root
        assert not invoice_root.exists() or not any(
            e.name == job_id for e in invoice_root.iterdir())

    def test_transfer_loader_refuses_foreign_job_type(self):
        job_id, _ = _make_job()
        path = tjobs.transfer_job_dir_for(job_id) / "transfer_job.json"
        data = json.loads(path.read_text())
        data["job_type"] = "invoice_extraction"
        path.write_text(json.dumps(data))
        assert tjobs.load_transfer_job(job_id) is None

    def test_invoice_cleanup_never_sees_transfer_jobs(self, monkeypatch):
        from apps.web import cleanup
        job_id, _ = _make_job()
        monkeypatch.setenv("WEB_JOB_RETENTION_HOURS", "0")
        counts = cleanup.cleanup_expired()
        assert tjobs.load_transfer_job(job_id) is not None   # untouched
        assert counts["removed"] == 0
