"""Invoice Extractor Pilot - single-user Streamlit UI (M9).

Run locally:   streamlit run apps/web/app.py     (binds localhost by default)
Run in Docker: docker compose up invoice-extractor-web   (127.0.0.1:8501)

Single user, no login: anyone who can reach the port can upload invoices and
trigger paid provider calls - keep it on localhost/Tailscale (docs/WEB_UI.md).
The page shows METADATA only; extracted invoice values live in the workbook.
"""

import sys
from pathlib import Path

# `streamlit run apps/web/app.py` puts the SCRIPT's directory on sys.path, not
# the project root - make the `apps` package importable however we're launched
# (repo checkout or /app inside the web image).
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st

from apps.web import cleanup, job_manager, ui_models
from apps.web.estimate import FilePlan, estimate_max_attempts
from apps.web.progress import (
    STATE_CANCELLED,
    STATE_RUNNING,
    TERMINAL_STATES,
    read_events,
    read_status,
)

st.set_page_config(page_title="Invoice Extractor Pilot", page_icon="🧾",
                   layout="centered")


# --- one-time-per-server startup cleanup --------------------------------------
@st.cache_resource
def _startup_cleanup() -> dict:
    return cleanup.cleanup_expired()


_startup_cleanup()


def _load_cfg():
    from invoice_extractor.config import load_config
    return load_config()


def _classify_job(job_id: str) -> list[FilePlan]:
    """Local, free classification of the job's uploads (no provider calls)."""
    from invoice_extractor import pdf_utils
    cfg = _load_cfg()
    plans: list[FilePlan] = []
    input_dir = job_manager.job_dir_for(job_id) / "input"
    for pdf in sorted(input_dir.glob("*.pdf")):
        try:
            pages = pdf_utils.analyze_pages(str(pdf), cfg.text_quality_threshold)
            plans.append(FilePlan(
                display_name=pdf.name,
                text_pages=sum(1 for p in pages if p.kind == "text"),
                image_pages=sum(1 for p in pages if p.kind == "image"),
                classification=pdf_utils.classify_document(pages)))
        except Exception:
            plans.append(FilePlan(display_name=pdf.name, text_pages=0,
                                  image_pages=0, classification="error"))
    return plans


def _provider_preflight(cfg) -> list[str]:
    """Safe, actionable readiness problems (no keys printed)."""
    problems = []
    if cfg.llm_gateway == "openrouter":
        if not cfg.openrouter_api_key:
            problems.append("OPENROUTER_API_KEY is not set in the server's .env.")
        if not cfg.openrouter_text_models:
            problems.append("OPENROUTER_TEXT_MODELS is not configured.")
        plans = st.session_state.get("plans") or []
        if any(p.image_pages for p in plans) and not cfg.openrouter_vision_models:
            problems.append("These PDFs contain image pages but "
                            "OPENROUTER_VISION_MODELS is not configured.")
    else:
        if not cfg.gemini_api_key:
            problems.append("GEMINI_API_KEY is not set in the server's .env.")
    return problems


# --- 6. Privacy notice (always visible, top of page) ---------------------------
st.title("Invoice Extractor Pilot")
st.info(
    "**Privacy notice.** During extraction, uploaded invoice content (text and "
    "rendered page images) is sent to the configured external model provider. "
    "Uploads and outputs are stored **temporarily** on the machine running this "
    "app and are deleted after the retention window "
    f"({cleanup.retention_hours():.0f}h) or via *Delete job files now*. This app "
    "provides no permanent storage - download your results. Do not upload "
    "unrelated confidential files. Debug artifacts are disabled by default."
)

active = job_manager.active_job()
session_job = st.session_state.get("job_id")


def _newest_terminal_job() -> str | None:
    root = job_manager.jobs_root()
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir(), reverse=True):
        if job_manager.JOB_ID_RE.match(entry.name) and not entry.is_symlink():
            status = read_status(entry) or {}
            if status.get("state") in TERMINAL_STATES:
                return entry.name
    return None


# A browser refresh rediscovers the ACTIVE job from the lock + status.json -
# it never starts another worker and keeps a working Cancel button.
if active is not None and active.job_id:
    st.session_state["job_id"] = active.job_id
    session_job = active.job_id

# --- 1. Upload invoices ---------------------------------------------------------
st.header("1. Upload invoices")
limits = job_manager.upload_limits()
st.caption(f"Limits: {limits['max_files']} files, {limits['max_file_mb']} MB each, "
           f"{limits['max_total_mb']} MB combined. PDF only.")
uploaded = st.file_uploader("Invoice PDFs", type=["pdf"],
                            accept_multiple_files=True,
                            disabled=active is not None)
if uploaded:
    total_mb = sum(len(f.getvalue()) for f in uploaded) / 1e6
    st.caption(f"Selected: {len(uploaded)} file(s), {total_mb:.1f} MB total")

if st.button("Validate & prepare", disabled=not uploaded or active is not None):
    try:
        validated = job_manager.validate_uploads(
            [(f.name, f.getvalue()) for f in uploaded])
        cleanup.cleanup_expired()
        job_id = job_manager.create_job(validated)
        plans = _classify_job(job_id)
        job_manager.mark_prepared(job_id, file_rows=[
            {"source_file": p.display_name, "extraction_method": p.classification,
             "provider": "-", "model": None, "needs_review": False,
             "error": p.classification == "error", "review_categories": []}
            for p in plans])
        st.session_state["job_id"] = job_id
        st.session_state["plans"] = plans
        st.rerun()
    except job_manager.JobError as exc:
        st.error(str(exc))

# --- 2. Run settings --------------------------------------------------------------
st.header("2. Run settings")
cfg = _load_cfg()
col1, col2 = st.columns(2)
with col1:
    enable_log = st.checkbox("Keep a downloadable run log", value=False)
with col2:
    enable_metadata = st.checkbox("Write run metadata JSON", value=False)

with st.expander("Advanced settings (this run only)"):
    st.caption("Loaded from the server configuration; changes apply only to "
               "this run's worker process. Dense scanned documents: use "
               "MAX_VISION_PAGES of 1-2 to reduce truncation risk.")
    adv_text_pages = st.number_input("MAX_TEXT_PAGES", 1, 50,
                                     value=cfg.max_text_pages)
    adv_vision_pages = st.number_input("MAX_VISION_PAGES", 1, 50,
                                       value=cfg.max_vision_pages)
    adv_attempts = st.text_input("MAX_MODEL_ATTEMPTS_PER_FILE (blank = no cap)",
                                 value=str(cfg.max_model_attempts_per_file or ""))
    adv_cost_file = st.text_input("MAX_COST_USD_PER_FILE (blank = no cap)",
                                  value=str(cfg.max_cost_usd_per_file or ""))
    adv_cost_run = st.text_input("MAX_COST_USD_PER_RUN (blank = no cap)",
                                 value=str(cfg.max_cost_usd_per_run or ""))
    adv_timeout = st.number_input("REQUEST_TIMEOUT_SECONDS", 10, 600,
                                  value=cfg.request_timeout_seconds)


def _settings_env() -> dict[str, str]:
    """Per-job env overrides (applied to the WORKER only - the server's own
    environment is never mutated). Validates numeric fields."""
    env = {
        "MAX_TEXT_PAGES": str(int(adv_text_pages)),
        "MAX_VISION_PAGES": str(int(adv_vision_pages)),
        "REQUEST_TIMEOUT_SECONDS": str(int(adv_timeout)),
    }
    for label, value, var in (("MAX_MODEL_ATTEMPTS_PER_FILE", adv_attempts,
                               "MAX_MODEL_ATTEMPTS_PER_FILE"),
                              ("MAX_COST_USD_PER_FILE", adv_cost_file,
                               "MAX_COST_USD_PER_FILE"),
                              ("MAX_COST_USD_PER_RUN", adv_cost_run,
                               "MAX_COST_USD_PER_RUN")):
        value = value.strip()
        if not value:
            env[var] = ""
            continue
        try:
            if "COST" in var:
                from decimal import Decimal
                if Decimal(value) < 0:
                    raise ValueError
            elif int(value) < 1:
                raise ValueError
        except Exception:
            raise job_manager.JobError(f"{label} must be a non-negative number.")
        env[var] = value
    return env


# --- prepared job: estimate + start ------------------------------------------------
job_id = st.session_state.get("job_id")
status = None
if job_id:
    try:
        status = read_status(job_manager.job_dir_for(job_id))
    except job_manager.JobError:
        status = None
if status is None and active is None:
    newest = _newest_terminal_job()
    if newest:
        job_id = newest
        status = read_status(job_manager.job_dir_for(newest))

state = (status or {}).get("state")

if state == "prepared" and active is None:
    plans = st.session_state.get("plans") or _classify_job(job_id)
    st.subheader("Prepared files")
    st.table([{"File": p.display_name, "Classification": p.classification,
               "Text pages": p.text_pages, "Image pages": p.image_pages}
              for p in plans])
    if all(v == "" for v in (adv_attempts.strip(), adv_cost_file.strip(),
                             adv_cost_run.strip())):
        st.warning("No safety limits are configured (attempt cap / file cost / "
                   "run cost). Paid requests are bounded only by chunks x "
                   "models x retries.")
    try:
        est = estimate_max_attempts(plans, cfg)
        st.markdown(f"**Maximum potential provider attempts under current "
                    f"settings: {est.max_attempts}**")
        st.caption("Actual requests may be lower because successful models, "
                   "validation, and budgets stop escalation. Assumptions: "
                   + " ".join(est.assumptions))
    except Exception:
        st.caption("Estimate unavailable.")

    problems = _provider_preflight(cfg)
    for p in problems:
        st.error(p)
    if st.button("Start extraction", type="primary",
                 disabled=bool(problems) or active is not None):
        try:
            settings_env = _settings_env()
            token = job_manager.acquire_lock(job_id)
            try:
                job_manager.spawn_worker(job_id, token,
                                         settings_env=settings_env,
                                         enable_log=enable_log,
                                         enable_metadata=enable_metadata)
            except Exception:
                st.error("Could not start the extraction worker. "
                         "Check the server logs.")
                st.stop()
            st.rerun()
        except job_manager.JobError as exc:
            st.error(str(exc))

elif active is not None and (session_job is None or active.job_id != job_id):
    st.warning("Another extraction is currently running.")

# --- 3. Extraction progress ---------------------------------------------------------
if job_id and ((status or {}).get("state") == STATE_RUNNING or
               (active is not None and active.job_id == job_id)):
    st.header("3. Extraction progress")

    @st.fragment(run_every="2s")
    def _progress_fragment():
        job_dir = job_manager.job_dir_for(job_id)
        current = read_status(job_dir) or {}
        if current.get("state") in TERMINAL_STATES:
            st.rerun(scope="app")
        events, _malformed = read_events(job_dir / "events.jsonl")
        view = ui_models.progress_from_events(events)
        if view.file_total:
            st.progress(min(view.files_done / view.file_total, 1.0),
                        text=f"File {min(view.file_index, view.file_total)} "
                             f"of {view.file_total}")
        if view.current_file:
            st.markdown(f"**Processing:** {view.current_file}")
            details = []
            if view.route:
                details.append(f"Route: {view.route}")
            if view.chunk_total:
                details.append(f"Chunk {view.chunk_index} of {view.chunk_total}")
            if view.attempt_type:
                details.append(f"Attempt: {view.attempt_type} "
                               f"({view.requested_model})")
            if details:
                st.caption(" - ".join(details))
        with st.container(height=220):
            for line in ui_models.compact_event_lines(events):
                st.text(line)
        lock = job_manager.read_lock()
        can_cancel = lock is not None and lock.job_id == job_id and lock.pid
        if st.button("Cancel extraction", disabled=not can_cancel):
            if job_manager.cancel_job(lock.job_id, lock.worker_token):
                st.warning("Cancelling - waiting for the worker to stop safely...")
            else:
                st.error("Could not verify the worker process; not sending "
                         "any signal.")

    _progress_fragment()

# --- 4/5. Results summary + downloads ------------------------------------------------
if status and state in TERMINAL_STATES:
    st.header("4. Results summary")
    if state == STATE_CANCELLED:
        st.warning("Cancelled by operator - partial results below.")
    summary = status.get("summary") or {}
    if summary:
        by_class = summary.get("by_classification") or {}
        st.table([{
            "Files submitted": summary.get("files_processed", 0),
            "Extracted": summary.get("extracted", 0),
            "Needs review": summary.get("needs_review", 0),
            "Failed/problem": summary.get("failed", 0),
        }])
        st.table([{
            "Text-native": by_class.get("text-native", 0),
            "Image-only": by_class.get("image-only", 0),
            "Mixed": by_class.get("mixed", 0),
            "Provider requests": summary.get("requests", 0),
            "Repairs": summary.get("repairs", 0),
            "Escalations": summary.get("escalations", 0),
        }])
        cost_line = f"Reported cost (USD): {summary.get('reported_cost', '0')}"
        if summary.get("unknown_cost_requests"):
            cost_line += (f" (incomplete: {summary['unknown_cost_requests']} "
                          "unknown-cost request(s))")
        st.caption(cost_line + f" - elapsed {summary.get('elapsed_seconds', 0)}s")

    review_rows = ui_models.needs_review_rows(status)
    if review_rows:
        st.subheader("Needs review")
        st.table(review_rows)
        st.caption("The Excel workbook's NeedsReview sheet has the full detail.")

    st.header("5. Downloads")
    artifacts = ui_models.downloadable_artifacts(job_id)
    if not artifacts:
        st.caption("No downloadable artifacts (nothing was written).")
    for name, content, mime in artifacts:
        st.download_button(f"Download {name}", data=content, file_name=name,
                           mime=mime, key=f"dl-{name}")
    st.caption("Uploaded source PDFs are not downloadable and are deleted "
               "with the job.")

    if st.button("Delete job files now"):
        if cleanup.delete_job(job_id):
            st.session_state.pop("job_id", None)
            st.session_state.pop("plans", None)
            st.success("Job files deleted.")
            st.rerun()
        else:
            st.error("Could not delete this job right now.")
