"""M9 web UI: job directories, the single-active-job lock, and retention
cleanup (tests K-R, AH-AK). Offline; temp dirs only."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from apps.web import cleanup, job_manager
from apps.web.job_manager import JobError, ValidatedUpload
from apps.web.progress import STATE_COMPLETED, build_status, write_status

PDF = b"%PDF-1.4 test"


@pytest.fixture(autouse=True)
def jobs_root(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setenv("WEB_JOBS_DIR", str(root))
    return root


def _uploads(n=1):
    return [ValidatedUpload(display_name=f"inv{i}.pdf", data=PDF) for i in range(n)]


# --- K/L/M: job directories -----------------------------------------------------

def test_k_unique_job_directory_created(jobs_root):
    a = job_manager.create_job(_uploads())
    b = job_manager.create_job(_uploads())
    assert a != b
    assert job_manager.JOB_ID_RE.match(a) and job_manager.JOB_ID_RE.match(b)
    for jid in (a, b):
        d = jobs_root / jid
        assert (d / "input" / "inv0.pdf").read_bytes() == PDF
        assert (d / "output").is_dir() and (d / "logs").is_dir()
        assert (d / "status.json").exists()


def test_l_job_dir_stays_under_root(jobs_root):
    jid = job_manager.create_job(_uploads())
    resolved = job_manager.job_dir_for(jid)
    assert resolved.parent == jobs_root.resolve()


def test_m_traversal_and_foreign_ids_rejected(jobs_root):
    for evil in ("../otherdir", "job-..", "job-20260101T000000-zzzzzzzzzzzz/../..",
                 "", "not-a-job", "job-20260101T000000-abcdef123456/../escape"):
        with pytest.raises(JobError):
            job_manager.job_dir_for(evil)


def test_m_symlinked_job_id_cannot_escape(jobs_root, tmp_path):
    jobs_root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    fake = "job-20260101T000000-abcdef123456"
    (jobs_root / fake).symlink_to(outside)
    # job_dir_for resolves; a symlink pointing OUTSIDE the root is rejected.
    with pytest.raises(JobError):
        job_manager.job_dir_for(fake)


# --- N/O/P/Q/R: the single-active-job lock ---------------------------------------

def test_n_second_active_job_refused(jobs_root):
    a = job_manager.create_job(_uploads())
    b = job_manager.create_job(_uploads())
    job_manager.acquire_lock(a)
    with pytest.raises(JobError, match="Another extraction"):
        job_manager.acquire_lock(b)


def test_lock_schema_and_permissions(jobs_root):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    raw = json.loads(job_manager.lock_path().read_text())
    assert set(raw) == {"schema_version", "job_id", "pid", "worker_token",
                        "started_at", "heartbeat_at"}
    assert raw["job_id"] == jid and raw["worker_token"] == token
    assert raw["schema_version"] == 1
    mode = job_manager.lock_path().stat().st_mode & 0o777
    assert mode == 0o600  # owner-only


def test_o_stale_lock_dead_pid_recovered(jobs_root):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    # Simulate a crashed worker: a PID that cannot exist.
    job_manager.update_lock(jid, token, pid=2**22 + 12345)
    assert job_manager.reclaim_stale_lock() is True
    assert not job_manager.lock_path().exists()
    # And a new job can now acquire.
    job_manager.acquire_lock(job_manager.create_job(_uploads()))


def test_o_live_verified_process_not_reclaimed(jobs_root, monkeypatch):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    job_manager.update_lock(jid, token, pid=os.getpid())  # alive (this test)
    # Fresh heartbeat -> not stale regardless of identity.
    assert job_manager.reclaim_stale_lock() is False
    # Expired heartbeat BUT process verifies as our worker -> still not stale.
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    job_manager.update_lock(jid, token, heartbeat_at=old)
    monkeypatch.setattr(job_manager, "_looks_like_our_worker", lambda pid: True)
    assert job_manager.reclaim_stale_lock() is False


def test_o_expired_heartbeat_unverified_process_reclaimed(jobs_root, monkeypatch):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    job_manager.update_lock(jid, token, pid=os.getpid())
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    job_manager.update_lock(jid, token, heartbeat_at=old)
    monkeypatch.setattr(job_manager, "_looks_like_our_worker", lambda pid: False)
    assert job_manager.reclaim_stale_lock() is True


def test_p_q_release_requires_matching_identity(jobs_root):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    job_manager.release_lock(jid, "wrong-token")      # not ours -> untouched
    assert job_manager.lock_path().exists()
    job_manager.release_lock("job-20990101T000000-aaaaaaaaaaaa", token)
    assert job_manager.lock_path().exists()
    job_manager.release_lock(jid, token)              # ours -> released
    assert not job_manager.lock_path().exists()


def test_r_cancel_fails_closed_without_verified_identity(jobs_root, monkeypatch):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    signals = []
    monkeypatch.setattr(job_manager.os, "killpg",
                        lambda pid, sig: signals.append((pid, sig)))
    # No PID recorded yet -> refuse.
    assert job_manager.cancel_job(jid, token) is False
    # PID recorded but identity check fails -> refuse (reused-PID protection).
    job_manager.update_lock(jid, token, pid=os.getpid())
    monkeypatch.setattr(job_manager, "_looks_like_our_worker", lambda pid: False)
    assert job_manager.cancel_job(jid, token) is False
    # Wrong token -> refuse.
    monkeypatch.setattr(job_manager, "_looks_like_our_worker", lambda pid: True)
    assert job_manager.cancel_job(jid, "bad-token") is False
    assert signals == []                     # nothing was ever signalled
    # Fully verified -> signal the process group once.
    assert job_manager.cancel_job(jid, token) is True
    assert signals == [(os.getpid(), job_manager.signal.SIGINT)]


def test_spawn_failure_releases_lock(jobs_root, monkeypatch):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    monkeypatch.setattr(job_manager.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("spawn fail")))
    with pytest.raises(OSError):
        job_manager.spawn_worker(jid, token, settings_env={},
                                 enable_log=False, enable_metadata=False)
    assert not job_manager.lock_path().exists()


# --- AH/AI/AJ/AK: cleanup ---------------------------------------------------------

def _age_job(jobs_root, jid, hours):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    write_status(jobs_root / jid, build_status(jid, STATE_COMPLETED,
                                               created_at=stamp,
                                               finished_at=stamp))


def test_ah_cleanup_removes_expired_jobs(jobs_root, monkeypatch):
    monkeypatch.setenv("WEB_JOB_RETENTION_HOURS", "24")
    old = job_manager.create_job(_uploads())
    fresh = job_manager.create_job(_uploads())
    _age_job(jobs_root, old, hours=30)
    stats = cleanup.cleanup_expired()
    assert stats["removed"] == 1
    assert not (jobs_root / old).exists()
    assert (jobs_root / fresh).exists()


def test_ai_cleanup_never_removes_active_job(jobs_root, monkeypatch):
    monkeypatch.setenv("WEB_JOB_RETENTION_HOURS", "0")  # everything "expired"
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    job_manager.update_lock(jid, token, pid=os.getpid())
    stats = cleanup.cleanup_expired()
    assert (jobs_root / jid).exists()
    assert stats["kept"] >= 1


def test_aj_cleanup_ignores_unrelated_dirs_and_symlinks(jobs_root, tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("WEB_JOB_RETENTION_HOURS", "0")
    jobs_root.mkdir(parents=True, exist_ok=True)
    (jobs_root / "not-a-job-dir").mkdir()
    (jobs_root / "precious.txt").write_text("keep me")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("outside data")
    (jobs_root / "job-20200101T000000-abcdef123456").symlink_to(outside)
    stats = cleanup.cleanup_expired()
    assert (jobs_root / "not-a-job-dir").exists()
    assert (jobs_root / "precious.txt").exists()
    assert (outside / "data.txt").exists()        # symlink target untouched
    assert stats["removed"] == 0
    assert stats["ignored"] >= 3


def test_ak_cleanup_survives_deletion_errors(jobs_root, monkeypatch):
    monkeypatch.setenv("WEB_JOB_RETENTION_HOURS", "0")
    jid = job_manager.create_job(_uploads())
    _age_job(jobs_root, jid, hours=1)
    monkeypatch.setattr(cleanup.shutil, "rmtree",
                        lambda p: (_ for _ in ()).throw(OSError("busy")))
    stats = cleanup.cleanup_expired()   # must not raise
    assert stats["errors"] == 1


def test_delete_job_refuses_active(jobs_root):
    jid = job_manager.create_job(_uploads())
    token = job_manager.acquire_lock(jid)
    job_manager.update_lock(jid, token, pid=os.getpid())
    assert cleanup.delete_job(jid) is False
    job_manager.release_lock(jid, token)
    assert cleanup.delete_job(jid) is True
    assert not (jobs_root / jid).exists()
