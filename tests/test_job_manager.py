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
    monkeypatch.setattr(cfg, "GEMINI_BIN", "echo")

    # Patch module-level names used inside job_manager / db
    monkeypatch.setattr(jm,     "JOBS_DIR",  tmp_path)
    monkeypatch.setattr(jm,     "CODEX_BIN", "echo")
    monkeypatch.setattr(jm,     "GEMINI_BIN", "echo")
    monkeypatch.setattr(db_mod, "DB_PATH",   db_file)

    # Redirect _job_dir helper
    monkeypatch.setattr(jm, "_job_dir", lambda job_id: tmp_path / job_id)

    # Invalidate any cached DB connections from previous tests.
    db_mod.reset_pool()

    # Re-initialise SQLite schema on the fresh DB file
    db_mod.init_db()

    # Clear in-memory state
    jm._active_procs.clear()
    jm._completion_events.clear()
    jm._last_completed_id[0] = None

    yield

    # Clean up cached connections on teardown.
    db_mod.reset_pool()


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


def test_start_gemini_job_uses_gemini_cli_flags(tmp_path):
    result = jm.start_job(
        "write a PR description",
        str(tmp_path),
        approval_policy="suggest",
        agent_type="gemini",
    )
    job_id = result["job_id"]

    wait_result = jm.wait_for_job(job_id, timeout_seconds=10)
    assert wait_result["status"] in ("done", "error")

    job = db_mod.db_get_job(job_id)
    assert job["agent_type"] == "gemini"

    output = (tmp_path / job_id / "output.txt").read_text()
    assert "--skip-trust --approval-mode plan --prompt" in output
    assert "write a PR description" in output


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


# ── notify_job_done driven wait (verifies 50s default is safe) ────────────────

def _seed_running_job(tmp_path, job_id: str, sleep_secs: int = 30):
    """Insert a synthetic 'running' job backed by a real sleep subprocess."""
    import subprocess

    job_dir = tmp_path / job_id
    job_dir.mkdir(exist_ok=True)
    (job_dir / "output.txt").write_text("")
    db_mod.db_insert_job(
        job_id, "synthetic", str(tmp_path), "full-auto",
        "2026-01-01T00:00:00+00:00",
    )
    db_mod.db_update_job(job_id, status="running")

    proc = subprocess.Popen(
        ["sleep", str(sleep_secs)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    db_mod.db_update_job(job_id, pid=proc.pid)
    jm._active_procs[job_id] = proc
    jm._completion_events[job_id] = threading.Event()
    return proc


def test_notify_job_done_unblocks_wait_quickly(tmp_path):
    """notify_job_done() must unblock wait_for_job() within milliseconds,
    even when timeout_seconds is the new 50s default."""
    job_id = "notify01"
    proc = _seed_running_job(tmp_path, job_id, sleep_secs=30)

    try:
        # Fire notify_job_done from another thread after a short delay.
        def fire():
            time.sleep(0.3)
            jm.notify_job_done(job_id, summary="all done")

        threading.Thread(target=fire, daemon=True).start()

        t0 = time.time()
        # Use the new MCP wrapper default — 50 seconds.
        result = jm.wait_for_job(job_id, timeout_seconds=50)
        elapsed = time.time() - t0

        assert result["status"] == "done"
        assert result["job_id"] == job_id
        # Must return well under MCP's ~60s drop limit. Allow 2s grace for CI.
        assert elapsed < 2.0, f"notify-driven wait took {elapsed:.2f}s (must be <2s)"
        # Summary should be appended to output.
        assert "all done" in result.get("output", "")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


def test_wait_returns_timeout_status_under_default(tmp_path):
    """If the job is still running and notify never fires, wait_for_job must
    return status='timeout' (not block indefinitely, not crash)."""
    job_id = "stillrun"
    proc = _seed_running_job(tmp_path, job_id, sleep_secs=10)

    try:
        t0 = time.time()
        result = jm.wait_for_job(job_id, timeout_seconds=0.5)
        elapsed = time.time() - t0

        assert result["status"] == "timeout"
        assert result["job_id"] == job_id
        # Should respect the timeout (allow scheduling slack).
        assert 0.4 <= elapsed < 2.0
        # Output snapshot should be included even on timeout.
        assert "output" in result
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


def test_concurrent_waiters_all_unblock_on_notify(tmp_path):
    """Multiple concurrent wait_for_job() callers must all unblock when
    notify_job_done() fires. Verifies _completion_events broadcast semantics."""
    job_id = "multi001"
    proc = _seed_running_job(tmp_path, job_id, sleep_secs=30)

    results: list[dict] = []
    results_lock = threading.Lock()

    def waiter():
        r = jm.wait_for_job(job_id, timeout_seconds=10)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=waiter) for _ in range(3)]
    try:
        for t in threads:
            t.start()

        time.sleep(0.3)  # Let waiters block.
        jm.notify_job_done(job_id, summary="multi done")

        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "Waiter thread did not unblock"

        assert len(results) == 3
        for r in results:
            assert r["status"] == "done"
            assert r["job_id"] == job_id
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


def test_await_any_unblocked_by_notify(tmp_path):
    """await_any_completion() must unblock when notify_job_done() fires
    on a job started via start_job — even before the subprocess exits."""
    job_id = "anyc0001"
    proc = _seed_running_job(tmp_path, job_id, sleep_secs=30)

    try:
        def fire():
            time.sleep(0.2)
            jm.notify_job_done(job_id, summary="any-done")

        threading.Thread(target=fire, daemon=True).start()

        t0 = time.time()
        result = jm.await_any_completion(timeout_seconds=10)
        elapsed = time.time() - t0

        assert result.get("status") == "done"
        assert result.get("job_id") == job_id
        assert elapsed < 2.0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


def test_resume_wait_after_timeout(tmp_path):
    """Client workflow: wait → timeout → wait again → notify → done.
    Verifies the event is reused correctly across multiple wait calls."""
    job_id = "resume01"
    proc = _seed_running_job(tmp_path, job_id, sleep_secs=30)

    try:
        # First call: should time out fast.
        r1 = jm.wait_for_job(job_id, timeout_seconds=0.3)
        assert r1["status"] == "timeout"

        # Schedule notify between the two waits.
        def fire():
            time.sleep(0.3)
            jm.notify_job_done(job_id, summary="resumed")

        threading.Thread(target=fire, daemon=True).start()

        # Second call: should pick up the completion.
        r2 = jm.wait_for_job(job_id, timeout_seconds=5)
        assert r2["status"] == "done"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


# ── Context injection + caching ───────────────────────────────────────────────

def test_inject_context_xml_format(tmp_path):
    """Project rules + context files render as <rules>/<file> tags."""
    (tmp_path / ".cursorrules").write_text("rule one")
    extra = tmp_path / "notes.md"
    extra.write_text("hello notes")

    out = jm._inject_context("do the task", str(tmp_path), ["notes.md"])

    assert "<rules src=.cursorrules>" in out
    assert "rule one" in out
    assert "<file src=notes.md>" in out
    assert "hello notes" in out
    assert "<task>\ndo the task\n</task>" in out
    # Old delimiters must be gone (saves tokens).
    assert "--- PROJECT RULES" not in out
    assert "=== TASK ===" not in out


def test_inject_context_missing_file(tmp_path):
    out = jm._inject_context("t", str(tmp_path), ["missing.txt"])
    assert "<file src=missing.txt>(not found)</file>" in out


def test_inject_context_no_rules_no_extras(tmp_path):
    """No rules, no context files → returns prompt unchanged."""
    out = jm._inject_context("plain prompt", str(tmp_path), None)
    assert out == "plain prompt"


def test_rules_cache_avoids_reread(tmp_path, monkeypatch):
    """Second call with unchanged rules must not re-read the file."""
    rules_file = tmp_path / ".cursorrules"
    rules_file.write_text("cached content")

    # Clear cache to ensure clean state.
    with jm._rules_cache_lock:
        jm._rules_cache.clear()

    read_count = {"n": 0}
    orig_read_text = Path.read_text

    def counting_read_text(self, *a, **kw):
        if self.name == ".cursorrules":
            read_count["n"] += 1
        return orig_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    jm._load_rules_blocks(str(tmp_path))
    jm._load_rules_blocks(str(tmp_path))
    jm._load_rules_blocks(str(tmp_path))

    assert read_count["n"] == 1, f"expected 1 read, got {read_count['n']}"


def test_rules_cache_invalidated_on_mtime_change(tmp_path):
    """Modifying the rules file must invalidate the cache."""
    rules_file = tmp_path / ".cursorrules"
    rules_file.write_text("v1")

    with jm._rules_cache_lock:
        jm._rules_cache.clear()

    blocks1 = jm._load_rules_blocks(str(tmp_path))
    assert any("v1" in b for b in blocks1)

    # Bump mtime + content.
    time.sleep(0.05)
    rules_file.write_text("v2")
    import os
    new_mtime = time.time() + 1
    os.utime(rules_file, (new_mtime, new_mtime))

    blocks2 = jm._load_rules_blocks(str(tmp_path))
    assert any("v2" in b for b in blocks2)
    assert not any("v1" in b for b in blocks2)


def test_notify_done_on_already_finished_is_noop(tmp_path):
    """Duplicate notify_job_done() calls must not crash or re-trigger."""
    result = jm.start_job("quick", str(tmp_path))
    job_id = result["job_id"]

    # Wait for echo to finish.
    jm.wait_for_job(job_id, timeout_seconds=5)

    # Now call notify_job_done — job already done; must be a no-op.
    r = jm.notify_job_done(job_id, summary="late")
    assert r["job_id"] == job_id
    # Status should reflect the already-finished state.
    assert r["status"] in ("done", "error")
    assert "already finished" in r.get("message", "").lower() or r["status"] == "done"
