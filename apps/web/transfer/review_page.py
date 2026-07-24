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


from apps.web.transfer.models import (          # noqa: E402
    JOB_PACKING_PREPARATION_COMPLETE,
    JOB_PACKING_PREPARATION_FAILED,
    JOB_PACKING_PREPARATION_IN_PROGRESS,
    JOB_PACKING_PREPARATION_WITH_ISSUES,
    JOB_PRODUCT_LOOKUP_COMPLETE,
    JOB_PRODUCT_LOOKUP_FAILED,
    JOB_PRODUCT_LOOKUP_IN_PROGRESS,
    JOB_PRODUCT_LOOKUP_WITH_ISSUES,
    JOB_WORKBOOK_GENERATION_COMPLETE,
    JOB_WORKBOOK_GENERATION_FAILED,
    JOB_WORKBOOK_GENERATION_IN_PROGRESS,
    JOB_WORKBOOK_GENERATION_WITH_ISSUES,
)

_WORKBOOK_STATES = (JOB_PACKING_PREPARATION_COMPLETE,
                    JOB_PACKING_PREPARATION_WITH_ISSUES,
                    JOB_WORKBOOK_GENERATION_IN_PROGRESS,
                    JOB_WORKBOOK_GENERATION_COMPLETE,
                    JOB_WORKBOOK_GENERATION_WITH_ISSUES,
                    JOB_WORKBOOK_GENERATION_FAILED)

_PACKING_STATES = (JOB_PRODUCT_LOOKUP_COMPLETE,
                   JOB_PRODUCT_LOOKUP_WITH_ISSUES,
                   JOB_PACKING_PREPARATION_IN_PROGRESS,
                   JOB_PACKING_PREPARATION_COMPLETE,
                   JOB_PACKING_PREPARATION_WITH_ISSUES,
                   JOB_PACKING_PREPARATION_FAILED) + _WORKBOOK_STATES

_PRODUCT_STATES = (JOB_READY_FOR_PRODUCT_LOOKUP,
                   JOB_PRODUCT_LOOKUP_IN_PROGRESS,
                   JOB_PRODUCT_LOOKUP_COMPLETE,
                   JOB_PRODUCT_LOOKUP_WITH_ISSUES,
                   JOB_PRODUCT_LOOKUP_FAILED) + (
                       JOB_PACKING_PREPARATION_IN_PROGRESS,
                       JOB_PACKING_PREPARATION_COMPLETE,
                       JOB_PACKING_PREPARATION_WITH_ISSUES,
                       JOB_PACKING_PREPARATION_FAILED) + _WORKBOOK_STATES


def _render_product_lookup_section(job, result, review) -> None:
    """Build 5: plan, run, and review product enrichment. Configuration
    checks and planning are local; the API is called ONLY when the user
    presses Run/Retry. No token or credential ever reaches this page."""
    from apps.web.transfer import product_lookup as pl

    st.header("Product lookup")
    state = pl.readiness()
    label = {"configured": "Configured",
             "not_configured": "Not configured",
             "configuration_error": "Configuration error"}[state["status"]]
    st.markdown("**Product API authentication:** " + label)
    if state["status"] != "configured":
        # problem strings contain variable NAMES only - never values
        st.caption("Set the API_GATEWAY_* / PRODUCT_LOOKUP_* variables in "
                   "the server's environment (see docs/DEPLOYMENT.md): "
                   + " ".join(state["problems"]))

    enrichment = pl.load_enrichment(job.job_id)

    plan = None
    plan_problems: list[str] = []
    try:
        config = pl.load_product_config()
        plan = pl.build_plan(job.job_id, config)
        plan_problems = list(plan.planning_problems)
    except Exception as exc:                      # JobError / ProductError
        plan_problems = [str(exc)]

    if plan is not None:
        row = st.columns(6)
        row[0].metric("Reviewed lines", plan.line_count)
        row[1].metric("Unique lookups", len(plan.lookups))
        row[2].metric("EAN lines", plan.ean_lines)
        row[3].metric("Fallback-ready", plan.fallback_ready_lines)
        row[4].metric("No identifier", plan.no_identifier_lines)
        row[5].metric("Batch size", config.batch_size)
        st.caption("Location(s): " + (", ".join(plan.locations) or "-")
                   + " | PriceDate(s): " + (", ".join(plan.price_dates) or "-")
                   + " | Qty policy: 1 (lookup-only)")
    for problem in plan_problems[:3]:
        st.error(problem)

    run_disabled = (state["status"] != "configured" or plan is None
                    or bool(plan_problems) or plan.line_count == 0)
    run_label = ("Retry Product Lookup"
                 if job.status in (JOB_PRODUCT_LOOKUP_FAILED,
                                   JOB_PRODUCT_LOOKUP_WITH_ISSUES,
                                   JOB_PRODUCT_LOOKUP_IN_PROGRESS)
                 or enrichment is not None
                 else "Run Product Lookup")
    if job.status == JOB_PRODUCT_LOOKUP_IN_PROGRESS:
        st.warning("A previous product lookup did not finish; retrying is "
                   "safe (results are written once, atomically).")
    if st.button(run_label, type="primary", disabled=run_disabled):
        progress = st.progress(0.0, text="Contacting the product API...")

        def on_progress(stage, batch_number, total, count):
            progress.progress(min(batch_number / max(total, 1), 1.0),
                              text=f"{stage} batch {batch_number}/{total} "
                                   f"({count} request(s))")

        try:
            with st.spinner("Looking up products via the internal API "
                            "Gateway..."):
                pl.run_product_lookup(job.job_id, on_progress=on_progress)
        except Exception as exc:
            st.error(str(exc))
        st.rerun()

    if enrichment is None:
        st.caption("Lookup output: authoritative product attributes "
                   "(including Analysis Codes and Compositions) per "
                   "reviewed line. Destination grouping, carton "
                   "renumbering, and Excel packing lists arrive in later "
                   "builds.")
        return

    if enrichment.get("stale"):
        st.warning("The review changed after this product lookup ran - the "
                   "enrichment below is stale and packing-list generation "
                   "will require a fresh lookup.")

    summary = enrichment.get("summary") or {}
    st.subheader("Lookup summary")
    r1 = st.columns(6)
    r1[0].metric("Lines", summary.get("lines", 0))
    r1[1].metric("Matched", summary.get("matched_lines", 0))
    r1[2].metric("Via fallback", summary.get("matched_via_fallback", 0))
    r1[3].metric("Unmatched", summary.get("unmatched_lines", 0))
    r1[4].metric("Blocking issues", summary.get("blocking_issues", 0))
    r1[5].metric("Warnings", summary.get("warning_issues", 0))
    st.caption(f"Unique products: {summary.get('unique_products', 0)} | "
               f"Batches: {summary.get('batches', 0)} | Status: "
               f"{enrichment.get('status')}")

    products = enrichment.get("products", [])
    rows = []
    for line in enrichment.get("line_enrichments", []):
        product = (products[line["product_ref"]]
                   if line.get("product_ref") is not None else {})
        source = line.get("source", {})
        row = {
            "Line": line["line_id"],
            "Carton": line.get("original_carton_number"),
            "Status": line.get("status"),
            "Via": line.get("matched_via") or "-",
            "Src EAN": source.get("ean"),
            "API EAN": product.get("ean"),
            "Src item": source.get("item_code"),
            "API item": product.get("item_code"),
            "API color": product.get("color_code"),
            "API size": product.get("size_code"),
            "API desc": (product.get("item_desc") or "")[:40],
            "Orig price": product.get("original_retail_price"),
            "Disc price": product.get("discount_price"),
            "Issues": line.get("comparison_issue_count", 0),
        }
        for i in (1, 2, 3):                        # compact preview columns
            row[f"AC{i:02d}"] = product.get(f"analysis_code_{i:02d}")
        row["Comp01"] = product.get("composition_01")
        rows.append(row)
    if rows:
        st.subheader("Per-line enrichment (source vs API)")
        st.dataframe(rows, height=380)
        with st.expander("Full Analysis Codes 01-15 and Compositions 1-4"):
            st.dataframe([{
                "Product": p.get("plu") or p.get("ean"),
                **{f"AC{i:02d}": p.get(f"analysis_code_{i:02d}")
                   for i in range(1, 16)},
                **{f"Comp{i:02d}": p.get(f"composition_{i:02d}")
                   for i in range(1, 5)},
            } for p in products])

    issues = enrichment.get("issues", [])
    if issues:
        st.subheader(f"Product lookup issues ({len(issues)})")
        st.table([{
            "Severity": i.get("severity"),
            "Code": i.get("code"),
            "Line": i.get("line_id") or "-",
            "Field": i.get("field") or "-",
            "Source": i.get("source_value"),
            "API": i.get("api_value"),
            "Message": i.get("message"),
        } for i in issues[:300]])
    st.caption("API values never overwrite reviewed source values. Next "
               "stages (later builds): destination grouping, carton "
               "renumbering, delivery invoice numbering, and Excel "
               "packing lists.")


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
        st.success("Approved - ready for product lookup. Editing below "
                   "reopens the review and makes any enrichment stale.")
    if job.status in _PRODUCT_STATES:
        _render_product_lookup_section(job, result, review)
    if job.status in _PACKING_STATES:
        _render_packing_section(job)
    if job.status in _WORKBOOK_STATES:
        _render_workbook_section(job)

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


def _render_packing_section(job) -> None:
    """Build 6: prepare and review packing groups. Everything is local and
    deterministic - no API call, no Excel/ZIP output (later builds)."""
    from apps.web.transfer import packing as pk

    st.header("Prepare packing groups")
    prepared = pk.load_preparation(job.job_id)

    stats = None
    problems: list[str] = []
    try:
        config = pk.load_packing_config()
        stats = pk.preview(job.job_id)
    except Exception as exc:
        problems = [str(exc)]

    if stats is not None:
        row = st.columns(6)
        row[0].metric("Destinations", len(stats["destinations"]))
        row[1].metric("Source cartons", stats["source_cartons"])
        row[2].metric("Eligible lines", stats["eligible_lines"])
        row[3].metric("Blocked lines", stats["blocked_lines"])
        row[4].metric("Total units", stats["total_units"])
        row[5].metric("Carton start",
                      pk.format_carton_number(config.carton_start, config))
        st.caption("Destinations (first-appearance order): "
                   + (", ".join(stats["destinations"]) or "-")
                   + f" | Numbering restarts at "
                     f"{pk.format_carton_number(config.carton_start, config)}"
                     " per destination | Delivery invoice format: "
                   + config.summary()["invoice_format"])
    for problem in problems[:3]:
        st.error(problem)

    run_disabled = (stats is None or stats["eligible_lines"] == 0
                    or bool(problems))
    label = ("Rerun Packing Preparation" if prepared is not None
             else "Prepare Packing Groups")
    if job.status == "PACKING_PREPARATION_IN_PROGRESS":
        st.warning("A previous preparation did not finish; rerunning is "
                   "safe (results are written once, atomically).")
    if st.button(label, type="primary", disabled=run_disabled):
        try:
            with st.spinner("Grouping, renumbering, and consolidating "
                            "locally (no API calls)..."):
                pk.prepare_packing(job.job_id)
        except Exception as exc:
            st.error(str(exc))
        st.rerun()

    if prepared is None:
        st.caption("Preparation output: one destination package per future "
                   "workbook - grouped by To Loc., cartons renumbered from "
                   "001 per destination, same-carton duplicate lines "
                   "combined. Excel generation arrives in a later build.")
        return

    if prepared.get("stale"):
        st.warning("The review or product enrichment changed after this "
                   "preparation ran - rerun preparation before any later "
                   "packing-list step.")

    summary = prepared.get("summary") or {}
    st.subheader("Destination summary")
    r1 = st.columns(6)
    r1[0].metric("Destinations", summary.get("destinations", 0))
    r1[1].metric("Cartons", summary.get("generated_cartons", 0))
    r1[2].metric("Source lines", summary.get("source_lines", 0))
    r1[3].metric("Prepared lines", summary.get("prepared_lines", 0))
    r1[4].metric("Consolidated rows", summary.get("consolidated_rows", 0))
    r1[5].metric("Total units", summary.get("total_units", 0))
    st.caption(f"Status: {prepared.get('status')} | Blocking: "
               f"{summary.get('blocking_issues', 0)} | Warnings: "
               f"{summary.get('warning_issues', 0)}")

    st.table([{
        "Destination": g["destination_code"],
        "Name": g.get("destination_name") or "-",
        "Delivery invoice no.": g["delivery_invoice_number"],
        "Cartons": g["generated_carton_count"],
        "Prepared lines": g["prepared_line_count"],
        "Units": g["total_units"],
        "Blocked": "YES" if g.get("blocked") else "-",
        "Future workbook": g["suggested_workbook_filename"],
    } for g in prepared.get("destinations", [])])

    with st.expander("Carton mapping (original -> generated)"):
        st.table([{
            "Destination": m["destination_code"],
            "Generated carton": m["generated_carton_number"],
            "Original carton": m["original_carton_number"] or "?",
            "Upload seq": m["source_carton_key"]["upload_sequence"],
            "Source file": m["source_carton_key"]["source_file"],
            "First page": m["source_carton_key"]["first_source_page"],
            "D/N": m["source_carton_key"]["delivery_note_number"] or "-",
            "Lines": m["line_count"],
        } for g in prepared.get("destinations", [])
            for m in g.get("carton_mappings", [])])

    with st.expander("Prepared lines (consolidated)"):
        st.dataframe([{
            "Destination": ln["destination_code"],
            "Carton": ln["generated_carton_number"],
            "Original carton": ln["original_carton_number"],
            "API item": ln["product"].get("item_code"),
            "API EAN": ln["product"].get("ean"),
            "PLU": ln["product"].get("plu"),
            "Description": ln["product"].get("item_desc"),
            "Color": ln["product"].get("color_code"),
            "Color desc": ln["product"].get("color_desc"),
            "Size": ln["product"].get("size_code"),
            "Qty": ln["quantity"],
            "Source rows": ln["source_rows"],
            "Source line IDs": ", ".join(ln["source_line_ids"]),
        } for g in prepared.get("destinations", [])
            for ln in g.get("prepared_lines", [])], height=360)

    issues = prepared.get("issues", [])
    if issues:
        st.subheader(f"Packing preparation issues ({len(issues)})")
        st.table([{
            "Severity": i.get("severity"),
            "Code": i.get("code"),
            "Destination": i.get("destination") or "-",
            "Line": i.get("line_id") or "-",
            "File": i.get("source_file") or "-",
            "Message": i.get("message"),
        } for i in issues[:300]])
    st.caption("Original carton numbers stay auditable above; API and "
               "reviewed values remain stored separately. Workbook "
               "generation arrives in a later build - no files are created "
               "here.")


def _render_workbook_section(job) -> None:
    """Build 7: generate, validate, and download packing-list workbooks.
    Local only - no API call, no printing, no email."""
    from apps.web.transfer import packing as pk
    from apps.web.transfer import workbook as wbmod

    st.header("Packing list workbooks")
    output = wbmod.load_output(job.job_id)

    problems: list[str] = []
    prepared = None
    config = None
    try:
        config = wbmod.load_workbook_config()
        job_obj, prepared = wbmod.load_generation_inputs(job.job_id)
    except Exception as exc:
        problems = [str(exc)]

    if prepared is not None:
        groups = prepared["destinations"]
        row = st.columns(6)
        row[0].metric("Destinations", len(groups))
        row[1].metric("Workbooks to generate", len(groups))
        row[2].metric("Cartons", prepared["summary"]["generated_cartons"])
        row[3].metric("Prepared lines",
                      prepared["summary"]["prepared_lines"])
        row[4].metric("Total units", prepared["summary"]["total_units"])
        row[5].metric("ZIP", "yes" if len(groups) > 1
                      and config.create_zip_for_multiple else "no")
        st.caption("Every workbook is reopened and validated before "
                   "download. One workbook per destination; a ZIP bundles "
                   "them when multiple destinations exist.")
    for problem in problems[:3]:
        st.error(problem)

    label = ("Regenerate Workbooks" if output is not None
             else "Generate Workbooks")
    if job.status == "WORKBOOK_GENERATION_IN_PROGRESS":
        st.warning("A previous generation did not finish; regenerating is "
                   "safe (files are validated before being recorded).")
    if st.button(label, type="primary", disabled=bool(problems)):
        progress = st.progress(0.0, text="Generating workbooks...")

        def on_progress(index, total, destination):
            progress.progress(min(index / max(total, 1), 1.0),
                              text=f"Workbook {index}/{total}: "
                                   f"{destination}")

        try:
            with st.spinner("Generating and validating workbooks locally "
                            "(no API calls)..."):
                wbmod.generate_workbooks(job.job_id,
                                         on_progress=on_progress)
        except Exception as exc:
            st.error(str(exc))
        st.rerun()

    if output is None:
        st.caption("Output: Packing_List_<Destination>_<InvoiceNo>.xlsx "
                   "with Packing List, Detail, Carton Mapping, Needs "
                   "Review, and Source Documents sheets. Printing and "
                   "email delivery are not part of this build.")
        return

    if output.get("stale"):
        st.warning("The packing preparation changed after these workbooks "
                   "were generated - downloads are disabled; regenerate "
                   "first.")

    summary = output.get("summary") or {}
    st.caption(f"Status: {output.get('status')} | Files: "
               f"{summary.get('total_files', 0)} | Total bytes: "
               f"{summary.get('total_bytes', 0):,} | Generated: "
               f"{output.get('updated_at')}")

    directory = wbmod.output_dir(job.job_id)
    for entry in output.get("destination_workbooks", []):
        cols = st.columns([3, 2, 1, 1, 1, 2])
        cols[0].markdown(f"**{entry['destination_code']}** - "
                         f"{entry['delivery_invoice_number']}")
        cols[1].caption(entry["filename"])
        cols[2].caption(f"{entry['carton_count']} ctn")
        cols[3].caption(f"{entry['prepared_line_count']} lines / "
                        f"{entry['total_units']} units")
        cols[4].caption(f"{entry['byte_size']:,} B | "
                        f"{entry['validation_status']} | sha "
                        + entry["sha256"][:10])
        path = directory / entry["filename"]
        if output.get("stale") or not path.is_file():
            cols[5].caption("unavailable (stale)")
        else:
            cols[5].download_button(
                "Download", data=path.read_bytes(),
                file_name=entry["filename"],
                mime=("application/vnd.openxmlformats-officedocument"
                      ".spreadsheetml.sheet"),
                key=f"transfer_wb_dl_{entry['filename']}")
        for issue in entry.get("validation_issues", []):
            st.caption(f"  {issue['severity']}: [{issue['code']}] "
                       f"{issue['message']}")

    zip_entry = output.get("zip")
    if zip_entry:
        zip_path = directory / zip_entry["filename"]
        if not output.get("stale") and zip_path.is_file():
            st.download_button(
                f"Download all ({zip_entry['member_count']} workbooks as "
                "ZIP)", data=zip_path.read_bytes(),
                file_name=zip_entry["filename"],
                mime="application/zip", key="transfer_wb_zip_dl")
            st.caption(f"ZIP: {zip_entry['byte_size']:,} B | sha "
                       + zip_entry["sha256"][:10])

    issues = output.get("issues", [])
    if issues:
        with st.expander(f"Workbook issues ({len(issues)})"):
            st.table([{"Severity": i.get("severity"),
                       "Code": i.get("code"),
                       "Destination": i.get("destination") or "-",
                       "Message": i.get("message")} for i in issues[:100]])
    st.caption("Printing, email delivery, and confirmed customer Analysis "
               "Code mappings are outside this build.")
