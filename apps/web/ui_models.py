"""Safe view models for the Streamlit page (M9).

Everything here is display-ready METADATA: counts, categories, routes, model
names, elapsed times. Never invoice values, review reason text, prompts,
provider bodies, or paths.
"""

from dataclasses import dataclass

from apps.web.job_manager import ARTIFACT_ALLOWLIST, JobError, job_dir_for


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
