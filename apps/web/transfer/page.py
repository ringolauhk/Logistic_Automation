"""Streamlit page for the Transfer Note Packing List workflow (Build 1:
upload shell only - no extraction, product lookup, or Excel generation).

All session-state keys are prefixed "transfer_" so nothing collides with
the invoice workflow's keys (job_id, plans, uploader_gen, new_batch_msg).
"""

import streamlit as st

from apps.web.job_manager import JobError
from apps.web.transfer import jobs
from apps.web.transfer.models import FILE_VALIDATED, TransferPackingJob

_UPLOADER_GEN_KEY = "transfer_uploader_gen"


def _uploader_key() -> str:
    return f"transfer-uploader-{st.session_state.get(_UPLOADER_GEN_KEY, 0)}"


def _reset_selection() -> None:
    st.session_state[_UPLOADER_GEN_KEY] = (
        st.session_state.get(_UPLOADER_GEN_KEY, 0) + 1)


def _size_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1e6:.1f} MB"


def _selection_table(validated, *, from_job: bool = False) -> list[dict]:
    rows = []
    for f in validated:
        rows.append({
            "Order": f.sequence,
            "File": f.original_name,
            "Size": _size_mb(f.size_bytes),
            "Pages": f.page_count if f.page_count is not None else "-",
            "Status": f.status,
        })
    return rows


def _render_job_summary(job: TransferPackingJob) -> None:
    st.subheader("Transfer Packing job created")
    row = st.columns(5)
    row[0].metric("Job ID", job.job_id.rsplit("-", 1)[-1])
    row[1].metric("Files", len(job.files))
    row[2].metric("Total pages", job.total_pages)
    row[3].metric("Total size", _size_mb(job.total_bytes))
    row[4].metric("Status", job.status)
    st.caption(f"Job ID: {job.job_id} - created {job.created_at}")
    st.table(_selection_table(job.files, from_job=True))
    st.info("Next planned stage: **Transfer Note extraction** (a later "
            "build). Extraction, product lookup, carton renumbering, and "
            "packing-list generation are not part of this build - the "
            "uploaded files are stored and this job is ready for when "
            "extraction is added.")
    if st.button("Start a new Transfer Packing selection"):
        st.session_state["transfer_job_id"] = None
        _reset_selection()
        st.rerun()


def render() -> None:
    """Render the whole Transfer Note workflow page, then stop."""
    st.title("Transfer Note Packing List")
    st.markdown("Upload one or more Transfer Delivery Note PDF files in the "
                "order that cartons should be processed.")
    limits = jobs.transfer_limits()

    # Refresh recovery: a created job is redisplayed from its metadata -
    # never re-created. Session key is transfer-specific.
    job_id = st.session_state.get("transfer_job_id")
    if job_id is None and "transfer_job_id" not in st.session_state:
        job_id = jobs.newest_transfer_job_id()
        if job_id:
            st.session_state["transfer_job_id"] = job_id
    job = jobs.load_transfer_job(job_id) if job_id else None
    if job is not None:
        _render_job_summary(job)
        return

    up_col, info_col = st.columns([3, 2])
    with up_col:
        uploaded = st.file_uploader(
            "Transfer Delivery Note PDFs", type=["pdf"],
            accept_multiple_files=True, key=_uploader_key())
    with info_col:
        st.caption(f"Limits: {limits['max_files']} files, "
                   f"{limits['max_file_mb']} MB each, "
                   f"{limits['max_pages']} pages combined. PDF only.")
        st.caption("Upload order matters: cartons are processed in the "
                   "order shown below (1, 2, 3, ...).")

    validated, issues = [], []
    if uploaded:
        validated, issues = jobs.validate_transfer_uploads(
            [(f.name, f.getvalue()) for f in uploaded])
        st.table(_selection_table(validated))
        for issue in issues:
            st.error(f"[{issue.code}] {issue.message}")
        if st.button("Clear selection"):
            _reset_selection()
            st.rerun()

    can_create = bool(uploaded) and not issues and validated and all(
        f.status == FILE_VALIDATED for f in validated)
    if st.button("Create Transfer Packing Job", type="primary",
                 disabled=not can_create):
        try:
            new_id = jobs.create_transfer_job(
                [(f.name, f.getvalue()) for f in uploaded], validated)
            st.session_state["transfer_job_id"] = new_id
            _reset_selection()
            st.rerun()
        except JobError as exc:
            st.error(str(exc))

    st.caption("Build 1 stores and validates uploads only. Extraction, "
               "To-Loc. grouping, carton renumbering, and per-destination "
               "Excel packing lists arrive in later builds.")
