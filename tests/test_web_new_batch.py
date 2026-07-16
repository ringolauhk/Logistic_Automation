"""M9.1 web UI: Start-New-Batch workflow, uploader reset, compact cost
summary, and the compact stylesheet. Offline; temp dirs only."""

import pytest

from apps.web import cleanup, job_manager, new_batch, ui_models
from apps.web.job_manager import ValidatedUpload
from apps.web.progress import (
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_NEEDS_REVIEW,
    STATE_PREPARED,
    STATE_RUNNING,
    TERMINAL_STATES,
    build_status,
    write_status,
)
from apps.web.style import COMPACT_CSS

PDF = b"%PDF-1.4 test"


@pytest.fixture(autouse=True)
def jobs_root(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setenv("WEB_JOBS_DIR", str(root))
    return root


def _finished_job(state=STATE_COMPLETED):
    job_id = job_manager.create_job(
        [ValidatedUpload(display_name="inv0.pdf", data=PDF)])
    job_dir = job_manager.job_dir_for(job_id)
    write_status(job_dir, build_status(job_id, state, created_at="t0"))
    return job_id, job_dir


def _session(job_id):
    """Session-state stand-in as the app leaves it after a finished run."""
    return {"job_id": job_id, "plans": ["plan-a", "plan-b"]}


# --- Start New Batch visibility ---------------------------------------------------

class TestVisibility:
    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
    def test_every_terminal_state_offers_new_batch(self, state):
        assert new_batch.can_start_new_batch(state, active=None)

    @pytest.mark.parametrize("state",
                             [STATE_RUNNING, STATE_PREPARED, "created", None])
    def test_non_terminal_states_do_not(self, state):
        assert not new_batch.can_start_new_batch(state, active=None)

    def test_active_lock_always_wins_even_with_terminal_status(self):
        # Mid-cancellation the status may already look terminal while the
        # worker still owns the lock - the button must stay hidden until
        # the lock is released.
        assert not new_batch.can_start_new_batch(STATE_CANCELLED,
                                                 active=object())


# --- session reset + uploader generation ------------------------------------------

class TestReset:
    def test_uploader_generation_and_key_change(self):
        session = _session("job-x")
        key_before = new_batch.uploader_key(session)
        new_batch.start_new_batch(session, None, delete_files=False)
        assert new_batch.uploader_key(session) != key_before
        assert session[new_batch.UPLOADER_GEN_KEY] == 1
        new_batch.start_new_batch(session, None, delete_files=False)
        assert session[new_batch.UPLOADER_GEN_KEY] == 2

    def test_clears_classification_progress_and_job_reference(self):
        job_id, _ = _finished_job()
        session = _session(job_id)
        new_batch.start_new_batch(session, job_id, delete_files=False)
        assert "plans" not in session               # classification + estimate
        assert session["job_id"] is None            # progress/summary/downloads

    def test_reset_keeps_job_id_key_so_old_job_is_not_readopted(self):
        # The app's newest-terminal-job fallback runs only when the session
        # has NO job_id key at all (a genuinely fresh browser session).
        job_id, _ = _finished_job()
        session = _session(job_id)
        new_batch.start_new_batch(session, job_id, delete_files=False)
        assert "job_id" in session and session["job_id"] is None

    def test_delete_selected_invokes_safe_deletion(self, jobs_root):
        job_id, job_dir = _finished_job()
        session = _session(job_id)
        msg = new_batch.start_new_batch(session, job_id, delete_files=True)
        assert msg == new_batch.DELETED_MSG
        assert not job_dir.exists()

    def test_delete_unselected_preserves_files_for_retention(self, jobs_root):
        job_id, job_dir = _finished_job()
        session = _session(job_id)
        msg = new_batch.start_new_batch(session, job_id, delete_files=False)
        assert msg == new_batch.KEPT_MSG
        assert (job_dir / "input" / "inv0.pdf").exists()

    def test_active_job_cannot_be_deleted_by_reset(self, jobs_root):
        job_id, job_dir = _finished_job()
        job_manager.acquire_lock(job_id)      # live-PID lock: job is active
        session = _session(job_id)
        msg = new_batch.start_new_batch(session, job_id, delete_files=True)
        assert msg == new_batch.DELETE_REFUSED_MSG
        assert job_dir.exists()               # protection preserved
        assert session["job_id"] is None      # UI still detaches safely

    def test_reset_never_spawns_a_worker(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("reset must not spawn a worker")
        monkeypatch.setattr(job_manager, "spawn_worker", boom)
        session = _session("job-x")
        new_batch.start_new_batch(session, None, delete_files=False)

    def test_refreshed_session_adopting_terminal_job_can_reset(self, jobs_root):
        # A fresh session (browser refresh) has no job_id key; the app
        # adopts the newest terminal job, then Start New Batch must work.
        job_id, job_dir = _finished_job()
        session = {}                                    # fresh browser session
        assert "job_id" not in session                  # fallback would adopt
        msg = new_batch.start_new_batch(session, job_id, delete_files=True)
        assert msg == new_batch.DELETED_MSG
        assert not job_dir.exists()
        assert session[new_batch.UPLOADER_GEN_KEY] == 1

    def test_messages_expose_no_paths_or_ids(self, jobs_root):
        job_id, _ = _finished_job()
        for delete in (True, False):
            jid, _ = _finished_job()
            msg = new_batch.start_new_batch(_session(jid), jid,
                                            delete_files=delete)
            assert jid not in msg
            assert "/" not in msg and "\\" not in msg


# --- cost summary -----------------------------------------------------------------

def _status(state=STATE_COMPLETED, **summary):
    base = {"files_processed": 2, "extracted": 2, "needs_review": 0,
            "failed": 0, "requests": 4, "repairs": 0, "escalations": 0,
            "reported_cost": "0.0004", "unknown_cost_requests": 0,
            "elapsed_seconds": 1.5, "interrupted": False}
    base.update(summary)
    return {"state": state, "summary": base}


class TestCostSummary:
    def test_total_reported_cost_shown_correctly(self):
        costs = ui_models.cost_summary(_status())
        assert costs.available
        assert costs.total_display == "0.0004"
        assert costs.requests == 4
        assert costs.elapsed_display == "1.5s"

    def test_average_cost_per_submitted_pdf(self):
        costs = ui_models.cost_summary(_status())
        assert costs.average_display == "0.000200"     # 0.0004 / 2, exact

    def test_unknown_cost_warning_flag(self):
        costs = ui_models.cost_summary(_status(unknown_cost_requests=1))
        assert costs.available and costs.incomplete
        assert "may be incomplete" in ui_models.INCOMPLETE_COST_NOTE

    def test_no_requests_shows_unavailable_not_zero(self):
        costs = ui_models.cost_summary(
            _status(requests=0, reported_cost="0"))
        assert not costs.available
        assert costs.total_display == ui_models.COST_UNAVAILABLE
        assert costs.total_display != "0"
        assert costs.average_display == "-"

    def test_all_unknown_costs_shows_unavailable(self):
        # e.g. the direct gateway, or a provider that never reports cost:
        # a $0 total would be misleading.
        costs = ui_models.cost_summary(
            _status(requests=3, unknown_cost_requests=3, reported_cost="0"))
        assert not costs.available
        assert costs.total_display == ui_models.COST_UNAVAILABLE

    def test_cancelled_partial_batch_uses_same_summary(self):
        costs = ui_models.cost_summary(
            _status(state=STATE_CANCELLED, requests=1,
                    reported_cost="0.0001", files_processed=1))
        assert costs.available and costs.total_display == "0.0001"

    def test_malformed_cost_is_unavailable(self):
        costs = ui_models.cost_summary(_status(reported_cost=None))
        assert not costs.available


# --- compact stylesheet -------------------------------------------------------------

class TestCompactStyle:
    def test_stylesheet_is_pure_presentation(self):
        low = COMPACT_CSS.lower()
        assert low.strip().startswith("<style>")
        assert "<script" not in low
        for forbidden in ("api_key", "sk-", "traceback", "/users/", "c:\\",
                          "invoice_number", "job-", "http"):
            assert forbidden not in low, f"stylesheet contains {forbidden!r}"

    def test_stylesheet_targets_stable_semantic_selectors(self):
        # data-testid hooks and public element classes only - no generated
        # emotion class names (st-emotion-cache-*).
        assert 'data-testid="stMainBlockContainer"' in COMPACT_CSS
        assert "st-emotion" not in COMPACT_CSS

    def test_app_uses_wide_layout_and_compact_css(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        src = (root / "apps" / "web" / "app.py").read_text(encoding="utf-8")
        assert 'layout="wide"' in src
        assert "COMPACT_CSS" in src
        assert "uploader_key" in src                   # keyed uploader reset
