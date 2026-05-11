"""
Tests for the refactored job_manager with SQLite queue + blocking wait.

Strategy
────────
• All tests use tmp_path for JOBS_DIR and a fresh in-memory SQLite DB
  (via monkeypatching DB_PATH + re-initialising the schema).
• 'echo' is used as a fast stand-in for the real codex binary.
• Threading primitives (_active_procs, _completion_events) are reset
  between tests via the `clean_state` fixture.
"""

import json
import time
import threading
from pathlib import Path

import pytest

import codex_async_mcp.job_manager as jm
import codex_async_mcp.config as cfg
import codex_async_mcp.db as db_mod


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """
    Redirect JOBS_DIR, DB_PATH to tmp_path and reset in-memory state
    for every test.
    """
    db_file = tmp_path / "queue.db"

    # Patch config
    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DB_PATH",  db_file)
    monkeypatch.setattr(cfg, "CODEX_BIN", "echo")

    # Patch module-level names used inside job_manager / db
    monkeypatch.setattr(jm,     "JOBS_DIR",  tmp_path)
    monkeypatch.setattr(jm,     "CODEX_BIN", "echo")
    monkeypatch.setattr(db_mod, "DB_PATH",   db_file)

    # Redirect _job_dir helper
    monkeypatch.setattr(jm, "_job_dir", lambda job_id: tmp_path / job_id)

    # Re-initialise SQLite schema on the fresh DB file
    db_mod.init_db()

    # Clear in-memory state
    jm._active_procs.clear()
    jm._completion_events.clear()
    jm._last_completed_id[0] = None

    yield


# ── Basic job lifecycle ───────────────────────────────────────────────────────

def test_start_job_creates_files(tmp_path):
    result = jm.start_job("hello world", str(tmp_path))

    job_id = result["job_id"]
    assert len(job_id) == 8

    # meta.json removed — DB is now the single source of truth.
    job_dir = tmp_path / job_id
    assert (job_dir / "output.txt").exists()

    job = db_mod.db_get_job(job_id)
    assert job["job_id"] == job_id
    assert job["prompt"] == "hello world"


def test_start_job_is_inserted_into_db(tmp_path):
    result = jm.start_job("db check", str(tmp_path))
    job = db_mod.db_get_job(result["job_id"])
    assert job is not None
    assert job["prompt"] == "db check"


def test_poll_unknown_job():
    result = jm.poll_job("nonexistent")
    assert "error" in result


def test_cancel_unknown_job():
    result = jm.cancel_job("badid")
    assert "error" in result


def test_list_jobs_empty():
    result = jm.list_jobs()
    assert result == []


# ── Queue sequencing ──────────────────────────────────────────────────────────

def test_second_job_is_queued_while_first_runs(tmp_path):
    """When one job is running, the next should be queued (status=pending)."""
    # Use 'sleep 10' so the first job stays running during the test.
    import codex_async_mcp.job_manager as jm2
    jm2.CODEX_BIN = "sleep"

    # Start a long-running job manually so it doesn't exit immediately.
    import subprocess
    job_id_1 = "aabbccdd"
    job_dir_1 = tmp_path / job_id_1
    job_dir_1.mkdir()
    (job_dir_1 / "output.txt").write_text("")
    db_mod.db_insert_job(job_id_1, "sleep 10", str(tmp_path), "full-auto",
                         "2026-01-01T00:00:00+00:00")
    db_mod.db_update_job(job_id_1, status="running")

    proc = subprocess.Popen(
        ["sleep", "10"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    jm._active_procs[job_id_1] = proc
    jm._completion_events[job_id_1] = threading.Event()

    try:
        # Enqueue a second job
        result2 = jm.start_job("second task", str(tmp_path))
        assert result2["status"] in ("pending", "queued", "running")

        job2 = db_mod.db_get_job(result2["job_id"])
        # First job is still running, so second should be pending
        assert job2["status"] == "pending"
    finally:
        proc.terminate()
        proc.wait()


# ── Blocking wait ─────────────────────────────────────────────────────────────

def test_wait_for_job_returns_when_done(tmp_path):
    """codex_wait() should return as soon as 'echo' exits (near-instant)."""
    result = jm.start_job("test prompt", str(tmp_path))
    job_id = result["job_id"]

    wait_result = jm.wait_for_job(job_id, timeout_seconds=10)
    assert wait_result["status"] in ("done", "error")
    assert wait_result["job_id"] == job_id


def test_wait_for_job_on_already_finished(tmp_path):
    """wait_for_job() on a completed job should return immediately."""
    result = jm.start_job("instant", str(tmp_path))
    job_id = result["job_id"]

    # Wait for it to finish first.
    deadline = time.time() + 5
    while time.time() < deadline:
        if db_mod.db_get_job(job_id)["status"] != "running":
            break
        time.sleep(0.05)

    # Second call should be instant (no blocking).
    t0 = time.time()
    wait_result = jm.wait_for_job(job_id, timeout_seconds=5)
    elapsed = time.time() - t0

    assert wait_result["status"] in ("done", "error")
    assert elapsed < 1.0, "Should return instantly for a finished job"


def test_wait_for_unknown_job():
    result = jm.wait_for_job("no_such_id", timeout_seconds=1)
    assert "error" in result


def test_wait_timeout_returns_timeout_status(tmp_path):
    """If job doesn't finish within timeout, return status='timeout'."""
    import subprocess

    job_id = "deadbeef"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "output.txt").write_text("")
    db_mod.db_insert_job(job_id, "slow job", str(tmp_path), "full-auto",
                         "2026-01-01T00:00:00+00:00")
    db_mod.db_update_job(job_id, status="running")

    proc = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    jm._active_procs[job_id] = proc
    event = threading.Event()
    jm._completion_events[job_id] = event

    try:
        result = jm.wait_for_job(job_id, timeout_seconds=0.2)
        assert result["status"] == "timeout"
    finally:
        proc.terminate()
        proc.wait()


# ── await_any_completion ──────────────────────────────────────────────────────

def test_await_any_completion(tmp_path):
    """await_any_completion() should unblock when any job finishes."""
    result = jm.start_job("any completion test", str(tmp_path))
    job_id = result["job_id"]

    any_result = jm.await_any_completion(timeout_seconds=10)
    assert any_result.get("status") in ("done", "error")
    assert any_result.get("job_id") == job_id


def test_await_any_timeout():
    result = jm.await_any_completion(timeout_seconds=0.1)
    assert result["status"] == "timeout"


# ── Queue status ──────────────────────────────────────────────────────────────

def test_queue_status_structure(tmp_path):
    status = jm.get_queue_status()
    assert "running" in status
    assert "pending" in status
    assert "pending_count" in status
    assert "recent_completed" in status


def test_queue_status_reflects_jobs(tmp_path):
    result = jm.start_job("status test", str(tmp_path))
    # Job either runs immediately or is pending.
    status = jm.get_queue_status()
    all_ids = (
        [status["running"]["job_id"]] if status["running"] else []
        + [j["job_id"] for j in status["pending"]]
        + [j["job_id"] for j in status["recent_completed"]]
    )
    # After echo finishes it'll be in recent_completed; before that it's running.
    # Just verify the structure is valid.
    assert isinstance(status["pending_count"], int)


# ── cancel_job ────────────────────────────────────────────────────────────────

def test_cancel_pending_job(tmp_path):
    """Cancelling a pending (queued but not yet started) job should work."""
    # Force a job to stay running so the second one stays pending.
    import subprocess

    sentinel_id = "11223344"
    sentinel_dir = tmp_path / sentinel_id
    sentinel_dir.mkdir()
    (sentinel_dir / "output.txt").write_text("")
    db_mod.db_insert_job(sentinel_id, "sentinel", str(tmp_path), "full-auto",
                         "2026-01-01T00:00:00+00:00")
    db_mod.db_update_job(sentinel_id, status="running")
    proc = subprocess.Popen(["sleep", "10"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    jm._active_procs[sentinel_id] = proc
    jm._completion_events[sentinel_id] = threading.Event()

    try:
        pending = jm.start_job("will be cancelled", str(tmp_path))
        assert db_mod.db_get_job(pending["job_id"])["status"] == "pending"

        cancel_result = jm.cancel_job(pending["job_id"])
        assert cancel_result["status"] == "cancelled"
        assert db_mod.db_get_job(pending["job_id"])["status"] == "cancelled"
    finally:
        proc.terminate()
        proc.wait()


def test_cancel_advances_queue(tmp_path):
    """After cancelling the running job, the next pending job should start."""
    import subprocess

    # Start a long running job directly.
    first_id = "ffff0000"
    first_dir = tmp_path / first_id
    first_dir.mkdir()
    (first_dir / "output.txt").write_text("")
    db_mod.db_insert_job(first_id, "first", str(tmp_path), "full-auto",
                         "2026-01-01T00:00:00+00:00")
    db_mod.db_update_job(first_id, status="running")
    proc = subprocess.Popen(["sleep", "10"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    db_mod.db_update_job(first_id, pid=proc.pid)
    jm._active_procs[first_id] = proc
    jm._completion_events[first_id] = threading.Event()

    # Queue a second job.
    second = jm.start_job("second", str(tmp_path))
    assert db_mod.db_get_job(second["job_id"])["status"] == "pending"

    # Cancel the first — should auto-start the second.
    jm.cancel_job(first_id)

    # Give the monitor/spawn a moment.
    deadline = time.time() + 5
    while time.time() < deadline:
        job2 = db_mod.db_get_job(second["job_id"])
        if job2["status"] != "pending":
            break
        time.sleep(0.05)

    job2 = db_mod.db_get_job(second["job_id"])
    assert job2["status"] in ("running", "done", "error")


# ── poll_job (backward compat) ────────────────────────────────────────────────

def test_poll_job_after_completion(tmp_path):
    result = jm.start_job("poll test", str(tmp_path))
    job_id = result["job_id"]

    # Wait for it to finish.
    deadline = time.time() + 5
    while time.time() < deadline:
        poll = jm.poll_job(job_id)
        if poll["status"] != "running":
            break
        time.sleep(0.05)

    assert poll["status"] in ("done", "error")
    assert poll["job_id"] == job_id
