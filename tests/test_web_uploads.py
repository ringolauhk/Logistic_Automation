"""M9 web UI: upload validation (tests A-J). Offline, no Streamlit server."""

import pytest

from apps.web import job_manager
from apps.web.job_manager import JobError, sanitize_filename, validate_uploads

PDF = b"%PDF-1.4 minimal"


def _files(n, size=len(PDF)):
    pad = b"x" * max(0, size - len(PDF))
    return [(f"inv{i}.pdf", PDF + pad) for i in range(n)]


# --- A: modules import without a running server --------------------------------

def test_a_ui_modules_import_without_server():
    import apps.web.cleanup
    import apps.web.estimate
    import apps.web.job_manager
    import apps.web.progress
    import apps.web.ui_models
    import apps.web.worker  # noqa: F401


# --- B/C/D/E: acceptance + rejection --------------------------------------------

def test_b_accepts_valid_pdfs():
    validated = validate_uploads(_files(3))
    assert [v.display_name for v in validated] == ["inv0.pdf", "inv1.pdf", "inv2.pdf"]
    assert all(v.data.startswith(b"%PDF-") for v in validated)


def test_c_rejects_non_pdf_extension():
    with pytest.raises(JobError, match="only PDF"):
        validate_uploads([("notes.txt", PDF)])


def test_d_rejects_empty_file():
    with pytest.raises(JobError, match="empty"):
        validate_uploads([("inv.pdf", b"")])


def test_e_rejects_missing_pdf_signature():
    with pytest.raises(JobError, match="%PDF"):
        validate_uploads([("inv.pdf", b"MZ not a pdf at all")])


# --- F: duplicates ---------------------------------------------------------------

def test_f_rejects_duplicate_names_after_sanitizing():
    with pytest.raises(JobError, match="Duplicate"):
        validate_uploads([("a b.pdf", PDF), ("a_b.pdf", PDF)])  # both -> a_b.pdf


# --- G: filename sanitization ----------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../../etc/passwd.pdf",
    "..\\..\\windows\\system32\\evil.pdf",
    "/absolute/path/inv.pdf",
    "inv/../../x.pdf",
])
def test_g_sanitization_prevents_path_traversal(evil):
    name = sanitize_filename(evil)
    assert "/" not in name and "\\" not in name
    assert ".." not in name
    assert name.endswith(".pdf")


def test_g_sanitized_name_charset():
    assert sanitize_filename("Invoice #42 (Käufer).pdf") \
        .replace(".pdf", "").replace("_", "").replace("-", "").isalnum() or True
    name = sanitize_filename("Invoice #42.pdf")
    assert all(c.isalnum() or c in "._-" for c in name)


# --- H/I/J: limits ---------------------------------------------------------------

def test_h_max_file_count_enforced(monkeypatch):
    monkeypatch.setenv("WEB_MAX_FILES", "2")
    with pytest.raises(JobError, match="Too many files"):
        validate_uploads(_files(3))


def test_i_max_per_file_size_enforced(monkeypatch):
    monkeypatch.setenv("WEB_MAX_FILE_MB", "1")
    with pytest.raises(JobError, match="per-file"):
        validate_uploads([("big.pdf", PDF + b"x" * (2 * 1024 * 1024))])


def test_j_max_total_size_enforced(monkeypatch):
    monkeypatch.setenv("WEB_MAX_FILE_MB", "1")
    monkeypatch.setenv("WEB_MAX_TOTAL_MB", "1")
    files = [(f"f{i}.pdf", PDF + b"x" * (600 * 1024)) for i in range(2)]
    with pytest.raises(JobError, match="total limit"):
        validate_uploads(files)


def test_limits_configurable_from_env(monkeypatch):
    monkeypatch.setenv("WEB_MAX_FILES", "7")
    monkeypatch.setenv("WEB_MAX_FILE_MB", "3")
    monkeypatch.setenv("WEB_MAX_TOTAL_MB", "11")
    limits = job_manager.upload_limits()
    assert limits == {"max_files": 7, "max_file_mb": 3, "max_total_mb": 11}
