"""Safe view models for the Streamlit page (M9).

Everything here is display-ready METADATA: counts, categories, routes, model
names, elapsed times. Never invoice values, review reason text, prompts,
provider bodies, or paths.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apps.web.job_manager import ARTIFACT_ALLOWLIST, JobError, job_dir_for

INCOMPLETE_COST_NOTE = ("Some provider requests did not report cost; the "
                        "displayed total may be incomplete.")
COST_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProgressView:
    """Current-activity line derived from the newest events."""
    current_file: str = ""
    file_index: int = 0
    file_total: int = 0
    route: str = ""
    chunk_index: int = 0
    chunk_total: int = 0
    attempt_type: str = ""
    requested_model: str = ""
    files_done: int = 0


def progress_from_events(events: list[dict]) -> ProgressView:
    view = ProgressView()
    state = dict(files_done=0, current_file="", file_index=0, file_total=0,
                 route="", chunk_index=0, chunk_total=0, attempt_type="",
                 requested_model="")
    for ev in events:
        kind = ev.get("event")
        if kind == "file_started":
            state.update(current_file=ev.get("source_file", ""),
                         file_index=ev.get("file_index", 0),
                         file_total=ev.get("file_total", 0),
                         route="", chunk_index=0, chunk_total=0,
                         attempt_type="", requested_model="")
        elif kind == "chunk_started":
            state.update(route=ev.get("route", ""),
                         chunk_index=ev.get("chunk_index", 0),
                         chunk_total=ev.get("chunk_total", 0))
        elif kind == "provider_request_started":
            state.update(attempt_type=ev.get("attempt_type", ""),
                         requested_model=ev.get("requested_model", ""),
                         route=ev.get("route", state["route"]))
        elif kind in ("file_completed", "file_needs_review", "file_failed"):
            state["files_done"] = max(state["files_done"],
                                      ev.get("file_index", 0))
    return ProgressView(**state)


def compact_event_lines(events: list[dict], limit: int = 12) -> list[str]:
    """Human lines for the compact on-page event log (newest last)."""
    lines: list[str] = []
    for ev in events:
        kind = ev.get("event")
        name = ev.get("source_file", "")
        if kind == "file_started":
            lines.append(f"File {ev.get('file_index')} of {ev.get('file_total')}: "
                         f"{name}")
        elif kind == "classification_complete":
            lines.append(f"{name}: classified as {ev.get('classification')}")
        elif kind == "chunk_started":
            lines.append(f"{name}: {ev.get('route')} chunk "
                         f"{ev.get('chunk_index')} of {ev.get('chunk_total')} "
                         f"(pages {ev.get('page_range', '?')})")
        elif kind == "provider_request_started":
            model_n = (ev.get("ladder_index") or 0) + 1
            lines.append(f"{name}: starting {ev.get('attempt_type')} model "
                         f"{model_n} of {ev.get('model_count', '?')} "
                         f"({ev.get('requested_model', '?')})")
        elif kind == "file_completed":
            lines.append(f"{name}: completed in {ev.get('elapsed_seconds', 0)}s")
        elif kind == "file_needs_review":
            cats = ", ".join(ev.get("review_categories") or []) or "review"
            lines.append(f"{name}: needs review ({cats})")
        elif kind == "file_failed":
            cats = ", ".join(ev.get("review_categories") or []) or "failed"
            lines.append(f"{name}: failed ({cats})")
        elif kind == "job_cancelled":
            lines.append("Cancelled by operator")
    return lines[-limit:]


@dataclass(frozen=True)
class CostSummary:
    """Display-ready cost figures for a finished (or partially finished)
    batch. Derived ONLY from the run summary that the engine's single
    cost-accounting path (usage records) already produced - no second
    implementation, no estimation of missing dollar costs."""
    available: bool
    total_display: str            # reported USD total, or COST_UNAVAILABLE
    average_display: str          # reported USD per submitted PDF, or "-"
    requests: int
    unknown_cost_requests: int
    incomplete: bool              # some requests reported no cost
    files_submitted: int
    elapsed_display: str


def cost_summary(status: dict) -> CostSummary:
    """Safe cost roll-up from status.json's summary block. Cost counts as
    available only when at least one provider request actually reported a
    cost; otherwise the UI must say 'unavailable', never imply $0."""
    summary = (status or {}).get("summary") or {}
    requests = int(summary.get("requests") or 0)
    unknown = int(summary.get("unknown_cost_requests") or 0)
    files = int(summary.get("files_processed") or 0)
    elapsed = summary.get("elapsed_seconds", 0)
    try:
        total = Decimal(str(summary.get("reported_cost")))
    except (InvalidOperation, TypeError, ValueError):
        total = None
    available = total is not None and requests > 0 and unknown < requests
    if available:
        total_display = str(total)
        average_display = (str((total / files).quantize(Decimal("0.000001")))
                           if files > 0 else "-")
    else:
        total_display = COST_UNAVAILABLE
        average_display = "-"
    return CostSummary(
        available=available,
        total_display=total_display,
        average_display=average_display,
        requests=requests,
        unknown_cost_requests=unknown,
        incomplete=available and unknown > 0,
        files_submitted=files,
        elapsed_display=f"{elapsed}s",
    )


def needs_review_rows(status: dict) -> list[dict]:
    """Compact NeedsReview table rows from status.json's safe per-file rows."""
    rows = []
    for row in status.get("files", []):
        if not (row.get("needs_review") or row.get("error")):
            continue
        rows.append({
            "File": row.get("source_file", ""),
            "Categories": ", ".join(row.get("review_categories") or []) or "-",
            "Route": row.get("extraction_method", "-"),
            "Provider/model": f"{row.get('provider', '-')}/{row.get('model') or '-'}",
            "Outcome": "failed" if row.get("error") else "partial/review",
        })
    return rows


def downloadable_artifacts(job_id: str) -> list[tuple[str, bytes, str]]:
    """(name, content, mime) for every allowlisted artifact that exists in
    THIS job. Anything outside the fixed allowlist is unreachable, so
    arbitrary paths - and uploaded source PDFs - can never be downloaded."""
    try:
        job_dir = job_dir_for(job_id)
    except JobError:
        return []
    out = []
    for name, (subdir, mime) in ARTIFACT_ALLOWLIST.items():
        path = (job_dir / subdir / name)
        if path.is_file() and not path.is_symlink():
            try:
                out.append((name, path.read_bytes(), mime))
            except OSError:
                continue
    return out
