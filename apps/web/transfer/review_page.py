"""Streamlit review screen for Transfer Note extraction (Build 3).

Read/correct/exclude/approve - all local. Product lookup itself is NOT part
of this build; approval only marks the job READY_FOR_PRODUCT_LOOKUP. All
session keys are transfer_-prefixed. Editable tables use st.data_editor
keyed by stable entity IDs (never row positions).
"""

import streamlit as st

from apps.web.job_manager import JobError
from apps.web.transfer import extraction, jobs, review as review_mod
from apps.web.transfer.extraction_models import TransferExtractionResult
from apps.web.transfer.models import (
    JOB_EXTRACTED,
    JOB_EXTRACTED_WITH_ISSUES,
    JOB_READY_FOR_PRODUCT_LOOKUP,
)
from apps.web.transfer.review_models import (
    CARTON_FIELDS,
    HEADER_FIELDS,
    LINE_FIELDS,
    REVIEW_APPROVED,
    REVIEW_STALE,
    TransferReviewResult,
)

_FILTERS = ("All", "Changed", "Blocking issues", "Warnings", "Excluded",
            "Lookup not ready")


def _fmt(value) -> str:
    return "" if value is None else str(value)


def _render_auth_readiness() -> None:
    """Backend-only configuration status for the future product lookup.
    Config check only - NO network request happens on render, and no
    credential value is ever displayed."""
    from apps.web.transfer.gateway_auth import readiness
    state = readiness()
    label = {"configured": "Configured",
             "not_configured": "Not configured",
             "configuration_error": "Configuration error"}[state["status"]]
    st.markdown("**Product API authentication:** " + label)
    if state["status"] == "configured":
        st.caption("Product lookup will be added in Build 5. Credentials "
                   "stay on the server; nothing is shown here.")
    else:
        # problem strings contain variable NAMES only - never values
        st.caption("Set the API_GATEWAY_* variables in the server's "
                   "environment (see docs/DEPLOYMENT.md): "
                   + " ".join(state["problems"])
                   + " Product lookup will be added in Build 5.")


def render_review_section(job, result: TransferExtractionResult) -> None:
    st.header("Review & correct")
    st.caption("Original extracted values stay stored unchanged; your "
               "corrections are saved separately and the effective value is "
               "correction-if-present, otherwise the original. Empty cells "
               "never erase a value - type `<clear>` to clear one "
               "deliberately. Product lookup itself arrives in a later "
               "build.")

    review = review_mod.get_or_create_review(job.job_id)
    if review is None:
        st.error("No extraction result is available to review.")
        return
    if review.status == REVIEW_STALE:
        st.warning("The extraction result changed after this review was "
                   "created. The saved review is preserved for audit but "
                   "cannot be approved - rebuild it from the current "
                   "extraction to continue.")
        if st.button("Rebuild review from current extraction"):
            review_mod.rebuild_review(job.job_id)
            st.rerun()
        return
    if job.status in (JOB_EXTRACTED, JOB_EXTRACTED_WITH_ISSUES):
        review_mod.begin_review(job.job_id)

    ev = review_mod.evaluate(result, review)

    # --- B. blocking banner -----------------------------------------------------
    if ev.unresolved_blocking:
        st.error(f"{len(ev.unresolved_blocking)} blocking issue(s) must be "
                 "corrected or excluded before approval.")
    elif job.status == JOB_READY_FOR_PRODUCT_LOOKUP:
        st.success("Approved - ready for product lookup (a later build). "
                   "Editing below reopens the review.")
        _render_auth_readiness()

    # --- G. validation summary ----------------------------------------------------
    r1 = st.columns(6)
    r1[0].metric("Documents",
                 f"{ev.included_documents}/{ev.included_documents + ev.excluded_documents}")
    r1[1].metric("Cartons",
                 f"{ev.included_cartons}/{ev.included_cartons + ev.excluded_cartons}")
    r1[2].metric("Lines",
                 f"{ev.included_lines}/{ev.included_lines + ev.excluded_lines}")
    r1[3].metric("Effective units", ev.total_effective_units)
    r1[4].metric("Corrected fields", ev.corrected_field_count)
    r1[5].metric("Resolved issues", ev.resolved_issue_count)
    r2 = st.columns(6)
    r2[0].metric("Blocking", len(ev.unresolved_blocking))
    r2[1].metric("Warnings", len(ev.unresolved_warnings))
    r2[2].metric("Lookup-ready", ev.lookup_ready_lines)
    r2[3].metric("Not ready", ev.lookup_not_ready_lines)
    r2[4].metric("Destinations", len(ev.destinations))
    r2[5].metric("Review status", review.status)
    if ev.destinations:
        st.caption("Destinations (To Loc.): " + ", ".join(ev.destinations))

    # --- C. header review ---------------------------------------------------------
    st.subheader("Delivery-note headers")
    header_rows = []
    for h in review.headers:
        header_rows.append({
            "entity_id": h.entity_id,
            "File": h.source_file,
            "Seq": h.upload_sequence,
            "batch_reference": _fmt(h.effective("batch_reference")),
            "from_location_code": _fmt(h.effective("from_location_code")),
            "from_location_name": _fmt(h.effective("from_location_name")),
            "to_location_code": _fmt(h.effective("to_location_code")),
            "to_location_name": _fmt(h.effective("to_location_name")),
            "pick_reference": _fmt(h.effective("pick_reference")),
            "delivery_note_number": _fmt(h.effective("delivery_note_number")),
            "delivery_date": _fmt(h.effective("delivery_date")),
            "Original To Loc.": _fmt(h.original.get("to_location_code")),
            "Original D/N": _fmt(h.original.get("delivery_note_number")),
            "excluded": h.excluded,
            "exclusion_reason": _fmt(h.exclusion_reason),
        })
    edited_headers = st.data_editor(
        header_rows, key="transfer_review_headers", hide_index=True,
        disabled=("entity_id", "File", "Seq", "Original To Loc.",
                  "Original D/N"),
        column_config={"entity_id": None})

    # --- D. carton review ---------------------------------------------------------
    st.subheader("Cartons (upload order, then page order - not reorderable)")
    carton_rows = []
    for c in review.cartons:
        carton_rows.append({
            "entity_id": c.entity_id,
            "File": c.source_file,
            "Seq": c.upload_sequence,
            "Pages": ",".join(str(p) for p in c.source_pages),
            "D/N": _fmt(c.original.get("delivery_note_number")),
            "Destination": _fmt(c.effective("destination_code")
                                or c.original.get("destination_code")),
            "Inherited dest.": bool(c.original.get("destination_inherited")),
            "original_carton_number":
                _fmt(c.effective("original_carton_number")),
            "Extracted carton no.":
                _fmt(c.original.get("original_carton_number")),
            "Printed total": _fmt(c.original.get("printed_carton_total")),
            "Effective total": ev.carton_effective_totals.get(c.entity_id, 0),
            "excluded": c.excluded,
            "exclusion_reason": _fmt(c.exclusion_reason),
        })
    edited_cartons = st.data_editor(
        carton_rows, key="transfer_review_cartons", hide_index=True,
        disabled=("entity_id", "File", "Seq", "Pages", "D/N", "Destination",
                  "Inherited dest.", "Extracted carton no.", "Printed total",
                  "Effective total"),
        column_config={"entity_id": None})

    # --- E. line review (filtered) ------------------------------------------------
    st.subheader("Product lines")
    chosen = st.selectbox("Show", _FILTERS, key="transfer_review_filter")
    line_rows = []
    for ln in review.lines:
        line_ev = ev.lines[ln.entity_id]
        blocking = bool(line_ev.problems)
        has_warning = any(
            w.get("line_ref") == ln.original.get("source_sequence_number")
            and w.get("carton") == ln.original.get("original_carton_number")
            for w in ev.unresolved_warnings)
        if chosen == "Changed" and not ln.corrections:
            continue
        if chosen == "Blocking issues" and not blocking:
            continue
        if chosen == "Warnings" and not has_warning:
            continue
        if chosen == "Excluded" and not line_ev.effective_excluded:
            continue
        if chosen == "Lookup not ready" and (line_ev.lookup_ready
                                             or line_ev.effective_excluded):
            continue
        line_rows.append({
            "entity_id": ln.entity_id,
            "File": ln.source_file,
            "Page": ln.source_page,
            "Carton": _fmt(ln.original.get("original_carton_number")),
            "Seq#": _fmt(ln.original.get("source_sequence_number")),
            "Method": _fmt(ln.original.get("extraction_method")),
            "item_code": _fmt(ln.effective("item_code")),
            "ean": _fmt(ln.effective("ean")),
            "description": _fmt(ln.effective("description")),
            "retail_price": _fmt(ln.effective("retail_price")),
            "color_code": _fmt(ln.effective("color_code")),
            "size_code": _fmt(ln.effective("size_code")),
            "quantity": _fmt(ln.effective("quantity")),
            "Original size": _fmt(ln.original.get("size_code")),
            "Ready": "yes" if line_ev.lookup_ready else "NO",
            "excluded": ln.excluded,
            "exclusion_reason": _fmt(ln.exclusion_reason),
        })
    edited_lines = st.data_editor(
        line_rows, key=f"transfer_review_lines_{chosen}", hide_index=True,
        height=420,
        disabled=("entity_id", "File", "Page", "Carton", "Seq#", "Method",
                  "Original size", "Ready"),
        column_config={"entity_id": None})

    # --- F. excluded overview -----------------------------------------------------
    excluded_rows = [
        {"Type": t, "ID": e.entity_id, "Reason": _fmt(e.exclusion_reason)}
        for t, entities in (("document", review.headers),
                            ("carton", review.cartons),
                            ("line", review.lines))
        for e in entities if e.excluded]
    if excluded_rows:
        with st.expander(f"Excluded records ({len(excluded_rows)})"):
            st.table(excluded_rows)

    if ev.unresolved_blocking or ev.unresolved_warnings:
        with st.expander("Unresolved issues"):
            st.table([{"Severity": ("blocking" if r in ev.unresolved_blocking
                                    else "warning"),
                       "Code": r["code"], "File": r["source_file"],
                       "Page": _fmt(r["source_page"]),
                       "Carton": _fmt(r["carton"]),
                       "Line": _fmt(r["line_ref"]),
                       "Message": r["message"]}
                      for r in (ev.unresolved_blocking
                                + ev.unresolved_warnings)[:300]])

    # --- H/I. save + approve ------------------------------------------------------
    save_col, approve_col = st.columns([1, 2])
    with save_col:
        if st.button("Save Review", type="primary"):
            try:
                changed = 0
                changed += review_mod.apply_editor_rows(
                    review, "document", edited_headers, HEADER_FIELDS)
                changed += review_mod.apply_editor_rows(
                    review, "carton", edited_cartons, CARTON_FIELDS)
                changed += review_mod.apply_editor_rows(
                    review, "line", edited_lines, LINE_FIELDS)
                review_mod.save_review(
                    job.job_id, review,
                    expected_updated_at=review.updated_at)
                if job.status == JOB_READY_FOR_PRODUCT_LOOKUP and changed:
                    review_mod.reopen_review(job.job_id)
                st.session_state["transfer_review_msg"] = (
                    f"Saved ({changed} change(s)).")
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
    with approve_col:
        approve_disabled = (not ev.can_approve
                            or review.status == REVIEW_APPROVED)
        if st.button("Approve for Product Lookup",
                     disabled=approve_disabled):
            try:
                review_mod.approve_review(job.job_id)
                st.session_state["transfer_review_msg"] = (
                    "Approved - job is READY_FOR_PRODUCT_LOOKUP. Product "
                    "lookup itself arrives in a later build; no API was "
                    "called.")
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if approve_disabled and ev.approval_problems:
            st.caption("Approval blocked: "
                       + " ".join(ev.approval_problems[:3])
                       + (" ..." if len(ev.approval_problems) > 3 else ""))
    if st.session_state.get("transfer_review_msg"):
        st.success(st.session_state.pop("transfer_review_msg"))
