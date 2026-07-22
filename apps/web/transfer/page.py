"""Streamlit page for the Transfer Note Packing List workflow (Build 1:
upload shell only - no extraction, product lookup, or Excel generation).

All session-state keys are prefixed "transfer_" so nothing collides with
the invoice workflow's keys (job_id, plans, uploader_gen, new_batch_msg).
"""

import streamlit as st

from apps.web.job_manager import JobError
from apps.web.transfer import extraction, jobs
from apps.web.transfer.models import (
    EXTRACTABLE_STATUSES,
    FILE_VALIDATED,
    JOB_EXTRACTING,
    TransferPackingJob,
)

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
    st.subheader("Transfer Packing job")
    row = st.columns(5)
    row[0].metric("Job ID", job.job_id.rsplit("-", 1)[-1])
    row[1].metric("Files", len(job.files))
    row[2].metric("Total pages", job.total_pages)
    row[3].metric("Total size", _size_mb(job.total_bytes))
    row[4].metric("Status", job.status)
    st.caption(f"Job ID: {job.job_id} - created {job.created_at}")
    with st.expander("Uploaded files (in processing order)"):
        st.table(_selection_table(job.files, from_job=True))

    _render_extraction_section(job)

    if st.button("Start a new Transfer Packing selection"):
        st.session_state["transfer_job_id"] = None
        _reset_selection()
        st.rerun()


def _render_extraction_section(job: TransferPackingJob) -> None:
    """Build 2: run extraction and show the reviewable result. Read-only -
    correction/editing arrives in Build 3; there are no product-API or
    packing-list controls."""
    result = extraction.load_result(job.job_id)

    if job.status in EXTRACTABLE_STATUSES:
        label = ("Retry Transfer Note extraction"
                 if result is not None or job.status == JOB_EXTRACTING
                 else "Extract Transfer Notes")
        if job.status == JOB_EXTRACTING:
            st.warning("A previous extraction did not finish; retrying is "
                       "safe (results are written only once, atomically).")
        if st.button(label, type="primary"):
            progress = st.progress(0.0, text="Starting extraction...")

            def on_progress(seq, total, name):
                progress.progress(min(seq / max(total, 1), 1.0),
                                  text=f"File {seq} of {total}: {name}")

            try:
                with st.spinner("Extracting Transfer Notes locally "
                                "(no cloud calls)..."):
                    extraction.run_extraction(job.job_id,
                                              on_progress=on_progress)
            except JobError as exc:
                st.error(str(exc))
            st.rerun()

    if result is None:
        st.caption("Extraction output: structured cartons and item lines "
                   "grouped for review. Product enrichment, carton "
                   "renumbering, and per-destination packing lists arrive "
                   "in later builds.")
        return

    summary = result.summary()
    st.subheader("Extraction summary")
    r1 = st.columns(6)
    r1[0].metric("Files", f"{summary['processed_files']}/"
                          f"{summary['uploaded_files']}")
    r1[1].metric("Pages", summary["processed_pages"])
    r1[2].metric("Text pages", summary["pages_embedded_text"])
    r1[3].metric("OCR pages", summary["pages_ocr"])
    r1[4].metric("Unreadable", summary["pages_unreadable"])
    r1[5].metric("Recognized notes", summary["recognized_documents"])
    r2 = st.columns(6)
    r2[0].metric("Destinations", len(summary["destination_codes"]))
    r2[1].metric("Cartons", summary["cartons"])
    r2[2].metric("Item lines", summary["lines"])
    r2[3].metric("Total units", summary["total_units"])
    r2[4].metric("Warnings", summary["warnings"])
    r2[5].metric("Blocking errors", summary["errors"])
    if summary["destination_codes"]:
        st.caption("Destinations (To Loc.): "
                   + ", ".join(summary["destination_codes"]))

    cartons = [c for d in result.documents for c in d.cartons]
    if cartons:
        st.subheader("Cartons")
        st.table([{
            "Order": i + 1,
            "Carton": c.original_carton_number or "?",
            "Destination": c.destination_code or "?",
            "D/N": c.delivery_note_number or "?",
            "File": c.source_file,
            "Page(s)": ",".join(str(p) for p in c.source_pages),
            "Lines": len(c.lines),
            "Units": c.calculated_carton_total,
            "Printed": (c.printed_carton_total
                        if c.printed_carton_total is not None else "-"),
            "Check": c.validation_status,
        } for i, c in enumerate(cartons)])

        with st.expander("Item lines (read-only preview)"):
            for c in cartons:
                st.markdown(f"**Carton {c.original_carton_number or '?'}** - "
                            f"{c.destination_code or '?'} - "
                            f"{len(c.lines)} line(s)")
                st.table([{
                    "Seq": ln.source_sequence_number,
                    "Item": ln.normalized_item_code or ln.raw_item_code,
                    "EAN": ln.normalized_ean or ln.raw_ean,
                    "Description": (ln.normalized_description or "")[:60],
                    "Price": ln.normalized_retail_price or ln.raw_retail_price,
                    "Color": ln.normalized_color_code,
                    "Size": ln.normalized_size_code,
                    "Qty": (ln.normalized_quantity
                            if ln.normalized_quantity is not None
                            else ln.raw_quantity),
                } for ln in c.lines])

    issues = result.all_issues()
    if issues:
        st.subheader(f"Issues ({len(issues)})")
        st.table([{
            "Severity": i.severity,
            "Code": i.code,
            "File": i.source_file,
            "Page": i.source_page if i.source_page is not None else "-",
            "Carton": i.carton or "-",
            "Line": i.line_ref if i.line_ref is not None else "-",
            "Message": i.message,
        } for i in issues[:200]])
        if len(issues) > 200:
            st.caption(f"Showing first 200 of {len(issues)} issues.")
    st.caption("Next stage (later builds): review/correction, product "
               "enrichment via the internal API, carton renumbering per "
               "destination, and Excel packing lists.")


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
