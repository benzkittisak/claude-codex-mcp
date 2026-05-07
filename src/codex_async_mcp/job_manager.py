"""
Job manager with sequential queue and blocking-wait support.

Key design decisions
────────────────────
• Sequential queue  — at most one Codex process runs at a time.
  When a job finishes, _try_start_next_locked() automatically starts the
  next pending job from the SQLite queue.

• threading.Event per job  — wait_for_job() blocks cheaply inside the MCP
  server using event.wait(timeout).  No polling loop.  The monitor thread
  calls event.set() the moment the job finishes.

• Server-restart recovery  — _completion_events is in-memory and lost on
  restart.  wait_for_job() detects the missing event and either:
    – immediately returns the result if the job is already done in the DB, or
    – spawns a lightweight PID-watcher thread that watches the process until
      it exits, then resolves it.

• Backward compat  — start_job / poll_job / list_jobs / cancel_job keep
  their original signatures and file-based meta.json/output.txt storage.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import (
    CODEX_BIN,
    DEFAULT_APPROVAL_POLICY,
    JOBS_DIR,
    JOB_TAIL_LINES,
    MAX_OUTPUT_CHARS,
)
from .db import (
    db_count_status,
    db_get_job,
    db_get_next_pending,
    db_get_running,
    db_insert_job,
    db_list_jobs,
    db_update_job,
)

# Map approval_policy → codex exec flags
_APPROVAL_FLAGS: dict[str, list[str]] = {
    "suggest":   ["-s", "read-only"],
    "auto-edit": ["--full-auto"],
    "full-auto": ["--dangerously-bypass-approvals-and-sandbox"],
}

# ── In-memory state ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_active_procs: dict[str, subprocess.Popen] = {}
_completion_events: dict[str, threading.Event] = {}

# Condition for codex_await_any(): notified on every job completion.
_any_completion = threading.Condition()
_last_completed_id: list[Optional[str]] = [None]   # mutable singleton


# ── File-based storage (backward compat) ─────────────────────────────────────

def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _write_meta(job_id: str, data: dict) -> None:
    path = _job_dir(job_id) / "meta.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _patch_meta(job_id: str, **kwargs) -> None:
    """Update specific keys in meta.json without overwriting the whole file."""
    path = _job_dir(job_id) / "meta.json"
    if not path.exists():
        return
    with open(path) as f:
        meta = json.load(f)
    meta.update(kwargs)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)


def _read_tail(job_id: str, tail_lines: int = JOB_TAIL_LINES) -> str:
    output_path = _job_dir(job_id) / "output.txt"
    if not output_path.exists():
        return ""
    text = output_path.read_text(errors="replace")
    lines = text.splitlines()
    tail = "\n".join(lines[-tail_lines:])
    if len(tail) > MAX_OUTPUT_CHARS:
        tail = "...(truncated)...\n" + tail[-MAX_OUTPUT_CHARS:]
    return tail


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── Internal queue machinery ──────────────────────────────────────────────────

def _notify_completion(job_id: str) -> None:
    """Signal per-job event and the global any-completion condition."""
    with _lock:
        event = _completion_events.get(job_id)

    if event:
        event.set()

    with _any_completion:
        _last_completed_id[0] = job_id
        _any_completion.notify_all()


def _monitor(job_id: str, proc: subprocess.Popen) -> None:
    """Background thread: wait for Popen → update DB/meta → advance queue."""
    exit_code = proc.wait()
    status = "done" if exit_code == 0 else "error"
    now = datetime.now(timezone.utc).isoformat()

    try:
        db_update_job(job_id, status=status, exit_code=exit_code, finished_at=now)
        _patch_meta(job_id, status=status, exit_code=exit_code, finished_at=now)

        with _lock:
            _active_procs.pop(job_id, None)
            _try_start_next_locked()    # advance queue before releasing lock
    except Exception:
        # DB might be unavailable (e.g. during tests when the path is swapped).
        # Still notify waiters so codex_wait() / await_any_completion() unblock.
        with _lock:
            _active_procs.pop(job_id, None)

    _notify_completion(job_id)


def _pid_watcher(job_id: str, pid: int, event: threading.Event) -> None:
    """
    Fallback monitor after a server restart (no Popen available).
    Polls every 2 s until the PID disappears, then resolves the job.
    """
    while _is_pid_alive(pid):
        time.sleep(2)

    now = datetime.now(timezone.utc).isoformat()
    job = db_get_job(job_id)
    if job and job["status"] == "running":
        db_update_job(job_id, status="done", finished_at=now)
        _patch_meta(job_id, status="done", finished_at=now)

    with _lock:
        _active_procs.pop(job_id, None)
        _try_start_next_locked()

    event.set()
    with _any_completion:
        _last_completed_id[0] = job_id
        _any_completion.notify_all()


def _spawn_locked(job: dict) -> None:
    """
    Spawn a codex process for *job* and start the monitor thread.
    Must be called while _lock is held.
    """
    job_id         = job["job_id"]
    prompt         = job["prompt"]
    cwd            = job["cwd"]
    approval_policy = job["approval_policy"]

    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "output.txt"

    flags = _APPROVAL_FLAGS.get(approval_policy, ["--full-auto"])
    cmd = [CODEX_BIN, "exec"] + flags + [prompt]
    now = datetime.now(timezone.utc).isoformat()

    with open(output_path, "w") as out_f:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=subprocess.STDOUT,
            cwd=cwd,
        )

    db_update_job(job_id, status="running", pid=proc.pid, started_at=now)
    _patch_meta(job_id, status="running", pid=proc.pid, started_at=now)

    _active_procs[job_id] = proc
    if job_id not in _completion_events:
        _completion_events[job_id] = threading.Event()

    threading.Thread(target=_monitor, args=(job_id, proc), daemon=True).start()


def _try_start_next_locked() -> None:
    """
    If nothing is running, pull the oldest pending job from DB and start it.
    Must be called while _lock is held.
    """
    if _active_procs:
        return
    next_job = db_get_next_pending()
    if next_job:
        _spawn_locked(next_job)


# ── Public API ────────────────────────────────────────────────────────────────

def start_job(
    prompt: str,
    cwd: str,
    approval_policy: str = DEFAULT_APPROVAL_POLICY,
) -> dict:
    """
    Enqueue a new job.

    Starts immediately if the queue is idle; otherwise the job waits and will
    be started automatically when the current job finishes.

    Returns immediately with job_id — does NOT block.
    """
    job_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()

    # Write to DB first (as pending).
    db_insert_job(job_id, prompt, cwd, approval_policy, now)

    # Write legacy meta.json.
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    _write_meta(job_id, {
        "job_id": job_id,
        "status": "pending",
        "prompt": prompt,
        "cwd": cwd,
        "approval_policy": approval_policy,
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    })

    with _lock:
        _completion_events[job_id] = threading.Event()
        _try_start_next_locked()

    # Re-read DB to report accurate status.
    job = db_get_job(job_id)
    status = job["status"] if job else "pending"
    pending_count = db_count_status("pending")
    running = db_get_running()

    return {
        "job_id": job_id,
        "status": status,
        "message": (
            "Job started immediately"
            if status == "running"
            else f"Job queued (position {pending_count}) — will start automatically when current job finishes"
        ),
        "running_job": (
            running["job_id"]
            if running and running["job_id"] != job_id
            else None
        ),
    }


def wait_for_job(job_id: str, timeout_seconds: float = 300) -> dict:
    """
    Block until *job_id* reaches a terminal state, then return its result.

    Returns immediately if the job is already done/error/cancelled.
    Returns {"status": "timeout"} if it exceeds timeout_seconds — the caller
    can call again to keep waiting.
    """
    job = db_get_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}

    if job["status"] in ("done", "error", "cancelled"):
        return _build_result(job)

    with _lock:
        event = _completion_events.get(job_id)
        if event is None:
            # Server restarted — rebuild event and recover.
            event = threading.Event()
            _completion_events[job_id] = event

            if job["status"] == "running":
                pid = job.get("pid")
                if pid and _is_pid_alive(pid):
                    threading.Thread(
                        target=_pid_watcher,
                        args=(job_id, pid, event),
                        daemon=True,
                    ).start()
                else:
                    # PID already dead — resolve immediately.
                    now = datetime.now(timezone.utc).isoformat()
                    db_update_job(job_id, status="done", finished_at=now)
                    _patch_meta(job_id, status="done", finished_at=now)
                    event.set()
            # If still pending: event will be set by _monitor when the job runs.

    finished = event.wait(timeout=timeout_seconds)
    if not finished:
        return {
            "job_id": job_id,
            "status": "timeout",
            "message": (
                f"Job still running after {timeout_seconds}s. "
                "Call codex_wait() again to continue waiting."
            ),
        }

    job = db_get_job(job_id)
    return _build_result(job)


def await_any_completion(timeout_seconds: float = 300) -> dict:
    """
    Block until any job completes, then return its result.

    Useful when multiple jobs are queued and the caller wants to react to
    each completion sequentially.
    """
    with _any_completion:
        notified = _any_completion.wait(timeout=timeout_seconds)

    if not notified:
        return {
            "status": "timeout",
            "message": f"No job completed within {timeout_seconds}s.",
        }

    job_id = _last_completed_id[0]
    if not job_id:
        return {"status": "timeout", "message": "No completion recorded yet."}

    job = db_get_job(job_id)
    return _build_result(job) if job else {"error": "Completed job missing from DB"}


def get_queue_status() -> dict:
    """Return a snapshot of the full queue state."""
    all_jobs = db_list_jobs(limit=50)

    running = next((j for j in all_jobs if j["status"] == "running"), None)
    pending = sorted(
        [j for j in all_jobs if j["status"] == "pending"],
        key=lambda j: j["created_at"],
    )
    recent_done = [
        j for j in all_jobs
        if j["status"] in ("done", "error", "cancelled")
    ][:5]

    return {
        "running": (
            {
                "job_id":     running["job_id"],
                "prompt":     running["prompt"][:120],
                "cwd":        running["cwd"],
                "started_at": running["started_at"],
            }
            if running else None
        ),
        "pending_count": len(pending),
        "pending": [
            {
                "job_id":         j["job_id"],
                "queue_position": i + 1,
                "prompt":         j["prompt"][:120],
                "created_at":     j["created_at"],
            }
            for i, j in enumerate(pending)
        ],
        "recent_completed": [
            {
                "job_id":      j["job_id"],
                "status":      j["status"],
                "prompt":      j["prompt"][:120],
                "finished_at": j.get("finished_at"),
                "exit_code":   j.get("exit_code"),
            }
            for j in recent_done
        ],
    }


def recover_on_startup() -> None:
    """
    Called once when the MCP server starts.

    Marks stale 'running' jobs (whose PID is dead) as errors,
    then kicks off the next pending job if the queue is non-empty.
    """
    running = db_get_running()
    if running:
        pid = running.get("pid")
        if pid and not _is_pid_alive(pid):
            now = datetime.now(timezone.utc).isoformat()
            db_update_job(running["job_id"], status="error", finished_at=now)
            _patch_meta(running["job_id"], status="error", finished_at=now)

    with _lock:
        _try_start_next_locked()


# ── Backward-compatible helpers ───────────────────────────────────────────────

def _build_result(job: dict) -> dict:
    return {
        "job_id":      job["job_id"],
        "status":      job["status"],
        "exit_code":   job.get("exit_code"),
        "output":      _read_tail(job["job_id"]),
        "prompt":      job["prompt"],
        "cwd":         job["cwd"],
        "started_at":  job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def poll_job(job_id: str, tail_lines: int = JOB_TAIL_LINES) -> dict:
    """Non-blocking status check (original API — preserved for backward compat)."""
    job = db_get_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}

    if job["status"] == "running":
        with _lock:
            proc = _active_procs.get(job_id)
        if proc is None:
            pid = job.get("pid")
            if pid and not _is_pid_alive(pid):
                now = datetime.now(timezone.utc).isoformat()
                db_update_job(job_id, status="done", finished_at=now)
                _patch_meta(job_id, status="done", finished_at=now)
                job = db_get_job(job_id)

    output = _read_tail(job_id, tail_lines)
    return {
        "job_id":      job_id,
        "status":      job["status"],
        "exit_code":   job.get("exit_code"),
        "output":      output,
        "started_at":  job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "prompt":      job.get("prompt"),
        "cwd":         job.get("cwd"),
    }


def list_jobs(limit: int = 20) -> list[dict]:
    jobs = db_list_jobs(limit)
    return [
        {
            "job_id":      j["job_id"],
            "status":      j["status"],
            "prompt":      j["prompt"][:80] + ("..." if len(j["prompt"]) > 80 else ""),
            "cwd":         j["cwd"],
            "started_at":  j.get("started_at"),
            "finished_at": j.get("finished_at"),
        }
        for j in jobs
    ]


def cancel_job(job_id: str) -> dict:
    job = db_get_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}

    status = job["status"]

    if status == "pending":
        now = datetime.now(timezone.utc).isoformat()
        db_update_job(job_id, status="cancelled", finished_at=now)
        _patch_meta(job_id, status="cancelled", finished_at=now)
        _notify_completion(job_id)
        return {"job_id": job_id, "status": "cancelled", "message": "Removed from queue"}

    if status != "running":
        return {"job_id": job_id, "status": status, "message": "Job is not running or pending"}

    with _lock:
        proc = _active_procs.get(job_id)

    killed = False
    if proc is not None:
        proc.terminate()
        killed = True
    else:
        pid = job.get("pid")
        if pid and _is_pid_alive(pid):
            os.kill(pid, 15)  # SIGTERM
            killed = True

    now = datetime.now(timezone.utc).isoformat()
    db_update_job(job_id, status="cancelled", finished_at=now)
    _patch_meta(job_id, status="cancelled", finished_at=now)

    with _lock:
        _active_procs.pop(job_id, None)
        _try_start_next_locked()        # advance queue

    _notify_completion(job_id)
    return {"job_id": job_id, "status": "cancelled", "killed": killed}
