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

• SQLite is the single source of truth — meta.json removed; output.txt
  remains for streaming Codex stdout.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("codex-async-mcp")

from .config import (
    CODEX_BIN,
    CURSOR_BIN,
    DEFAULT_APPROVAL_POLICY,
    JOBS_DIR,
    JOB_TAIL_LINES,
    MAX_JOB_DURATION,
    MAX_OUTPUT_CHARS,
    MONITOR_POLL_INTERVAL,
    OUTPUT_STALL_TIMEOUT,
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


# ── File-based storage ───────────────────────────────────────────────────────

def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _read_tail(job_id: str, tail_lines: int = JOB_TAIL_LINES) -> str:
    output_path = _job_dir(job_id) / "output.txt"
    if not output_path.exists():
        return ""
    # Seek from end to avoid reading multi-MB files entirely into memory.
    max_bytes = MAX_OUTPUT_CHARS * 2  # generous estimate for tail
    try:
        size = output_path.stat().st_size
    except OSError:
        return ""
    with open(output_path, "r", errors="replace") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # discard partial line at the seek boundary
        text = f.read()
    lines = text.splitlines()
    tail = "\n".join(lines[-tail_lines:])
    if len(tail) > MAX_OUTPUT_CHARS:
        tail = "...(truncated)...\n" + tail[-MAX_OUTPUT_CHARS:]
    return tail


def _parse_token_usage_from_text(text: str) -> Optional[int]:
    """Parse 'tokens used\\nX' from Codex output text. Returns None if not found."""
    if not text:
        return None
    # Codex prints: "tokens used\n117,792" (with comma) or "117792"
    m = re.search(r"tokens used\s*\n\s*([\d,]+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── Internal queue machinery ──────────────────────────────────────────────────

def _notify_completion(job_id: str) -> None:
    """Signal per-job event, clean it up, and notify the global condition."""
    with _lock:
        event = _completion_events.pop(job_id, None)  # pop = set + cleanup in one step

    if event:
        event.set()

    with _any_completion:
        _last_completed_id[0] = job_id
        _any_completion.notify_all()


def _monitor(job_id: str, proc: subprocess.Popen) -> None:
    """
    Background thread: wait for Popen to exit, then update DB and advance queue.

    Stall detection
    ───────────────
    Codex sometimes finishes its task (printing a completion summary) but
    doesn't exit — e.g. because a child process it spawned (docker-exec, etc.)
    is still running or because it hangs on cleanup.

    Every MONITOR_POLL_INTERVAL seconds we check whether the output file has
    grown.  If OUTPUT_STALL_TIMEOUT elapses with no new output bytes, we
    SIGTERM the process and treat the job as done.  This unblocks codex_wait()
    without waiting forever.
    """
    output_path = _job_dir(job_id) / "output.txt"
    last_size: int = output_path.stat().st_size if output_path.exists() else 0
    last_growth_time: float = time.time()
    job_start_time: float = time.time()
    exit_code: int = 0

    while True:
        try:
            exit_code = proc.wait(timeout=MONITOR_POLL_INTERVAL)
            break  # process exited normally
        except subprocess.TimeoutExpired:
            pass   # still running — check stall and max-duration below

        # Hard ceiling: kill if job has run longer than MAX_JOB_DURATION
        if time.time() - job_start_time >= MAX_JOB_DURATION:
            logger.warning("Job %s exceeded max duration (%ds), terminating", job_id, MAX_JOB_DURATION)
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            exit_code = proc.returncode or 0
            break

        # Check output file growth (stall detection)
        try:
            current_size = output_path.stat().st_size if output_path.exists() else 0
        except OSError:
            current_size = last_size

        if current_size > last_size:
            last_size = current_size
            last_growth_time = time.time()
        elif time.time() - last_growth_time >= OUTPUT_STALL_TIMEOUT:
            # Output stalled → Codex is done but not exiting; kill it.
            logger.warning("Job %s output stalled for %ds, terminating", job_id, OUTPUT_STALL_TIMEOUT)
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            exit_code = proc.returncode or 0
            break

    now = datetime.now(timezone.utc).isoformat()

    try:
        # Check if Codex already called codex_notify_done() — if so, the job
        # is already "done" in the DB and Claude has already been unblocked.
        # We only update the DB / notify if the job is still marked "running".
        job = db_get_job(job_id)
        already_notified = job is not None and job["status"] != "running"

        if not already_notified:
            status = "done" if exit_code == 0 else "error"
            elapsed = time.time() - job_start_time
            logger.info("Job %s finished (status=%s, exit=%d, %.1fs)", job_id, status, exit_code, elapsed)
            db_update_job(job_id, status=status, exit_code=exit_code, finished_at=now)
            with _lock:
                _active_procs.pop(job_id, None)
                _try_start_next_locked()    # advance queue before releasing lock
        else:
            # notify_job_done() already advanced the queue — just clean up.
            with _lock:
                _active_procs.pop(job_id, None)
    except Exception:
        # DB might be unavailable (e.g. during tests when the path is swapped).
        with _lock:
            _active_procs.pop(job_id, None)
        already_notified = False

    if not already_notified:
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
    agent_type     = job.get("agent_type", "codex")

    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "output.txt"

    flags = _APPROVAL_FLAGS.get(approval_policy, ["--full-auto"])

    # Append job_id instruction so Codex can call codex_notify_done() when done.
    # This gives Claude an immediate callback instead of waiting for process exit.
    full_prompt = (
        f"{prompt}\n\n"
        f"---\n"
        f"When you have fully completed the task above, call the MCP tool "
        f"`codex_notify_done` with:\n"
        f"  job_id  = \"{job_id}\"\n"
        f"  summary = one-sentence description of what was done\n"
        f"This signals Claude that you are done so it can continue immediately."
    )
    
    if agent_type == "cursor":
        if os.name == "nt":
            cmd = ["wsl", "bash", "-lc", 'agent -p --force "$1"', "--", full_prompt]
        else:
            cmd = [CURSOR_BIN, "-p", "--force", full_prompt]
    else:
        cmd = [CODEX_BIN, "exec"] + flags + [full_prompt]
        
    now = datetime.now(timezone.utc).isoformat()

    try:
        with open(output_path, "w") as out_f:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                cwd=cwd,
            )
    except FileNotFoundError:
        logger.error("Executable not found: %s", cmd[0])
        now = datetime.now(timezone.utc).isoformat()
        db_update_job(job_id, status="error", exit_code=127, finished_at=now)
        with open(output_path, "w") as out_f:
            out_f.write(f"Error: command not found: {cmd[0]}\n")
        _try_start_next_locked()
        return

    db_update_job(job_id, status="running", pid=proc.pid, started_at=now)
    logger.info("Job %s spawned (pid=%d, cwd=%s)", job_id, proc.pid, cwd)

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

def _inject_context(prompt: str, cwd: str, context_files: list[str] | None) -> str:
    """Read context files and cursorrules, then prepend them to the prompt."""
    context_blocks = []
    
    # Auto-detect rules
    rules_paths = [Path(cwd) / ".cursorrules", Path(cwd) / ".cursor" / "rules"]
    for path in rules_paths:
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                context_blocks.append(f"--- PROJECT RULES ({path.name}) ---\n{content}\n")
            except Exception as e:
                logger.warning("Failed to read rules file %s: %s", path, e)
        elif path.is_dir():
            for rule_file in path.glob("*"):
                if rule_file.is_file():
                    try:
                        content = rule_file.read_text(encoding="utf-8")
                        context_blocks.append(f"--- RULE ({rule_file.name}) ---\n{content}\n")
                    except Exception:
                        pass
    
    # Inject user-provided context files
    if context_files:
        for file_path in context_files:
            # Resolve relative to cwd if not absolute
            path = Path(cwd) / file_path if not Path(file_path).is_absolute() else Path(file_path)
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8")
                    context_blocks.append(f"--- FILE CONTEXT: {file_path} ---\n{content}\n")
                except Exception as e:
                    logger.warning("Failed to read context file %s: %s", file_path, e)
            else:
                context_blocks.append(f"--- FILE CONTEXT: {file_path} ---\n(File not found or is a directory)\n")

    if context_blocks:
        return "\n".join(context_blocks) + "\n\n=== TASK ===\n" + prompt
    return prompt


def start_job(
    prompt: str,
    cwd: str,
    approval_policy: str = DEFAULT_APPROVAL_POLICY,
    agent_type: str = "codex",
    context_files: list[str] | None = None,
) -> dict:
    """
    Enqueue a new job.

    Starts immediately if the queue is idle; otherwise the job waits and will
    be started automatically when the current job finishes.

    Returns immediately with job_id — does NOT block.
    """
    # Validate cwd before touching the DB.
    if not Path(cwd).is_dir():
        return {"error": f"cwd does not exist or is not a directory: {cwd}"}

    # Inject context into prompt
    prompt = _inject_context(prompt, cwd, context_files)

    job_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()

    # Write to DB (source of truth).
    db_insert_job(job_id, prompt, cwd, approval_policy, now, agent_type)
    logger.info("Job %s enqueued (prompt=%.80s, agent_type=%s)", job_id, prompt, agent_type)

    # Create job directory for output.txt (no meta.json — DB is authoritative).
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

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
            "output": _read_tail(job_id),   # current output so far
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


def notify_job_done(job_id: str, summary: str = "") -> dict:
    """
    Called by Codex (via the codex_notify_done MCP tool) to signal task completion.

    Immediately unblocks codex_wait() without waiting for the process to exit.
    The stall-detection logic in _monitor serves as a fallback if this is never called
    (e.g. because Codex crashed before reaching the end of its task).

    If *summary* is provided it is appended to the job's output file so Claude
    can read it in the poll / wait result.
    """
    job = db_get_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}

    if job["status"] != "running":
        # Already finished (race with _monitor, or duplicate call) — no-op.
        return {
            "job_id": job_id,
            "status": job["status"],
            "message": "Job already finished — no action taken",
        }

    # Append summary to the output file so it's visible in the poll result.
    if summary:
        output_path = _job_dir(job_id) / "output.txt"
        try:
            with open(output_path, "a") as f:
                f.write(f"\n[codex_notify_done] {summary}\n")
        except OSError:
            pass

    now = datetime.now(timezone.utc).isoformat()
    db_update_job(job_id, status="done", exit_code=0, finished_at=now)
    logger.info("Job %s notify_done received (summary=%.80s)", job_id, summary)

    # Remove from active procs and start the next queued job.
    with _lock:
        _active_procs.pop(job_id, None)
        _try_start_next_locked()

    # Unblock any codex_wait() / codex_await_any() calls.
    _notify_completion(job_id)

    return {
        "job_id": job_id,
        "status": "done",
        "message": "Claude notified — continuing immediately",
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
            logger.warning("Recovering stale job %s (pid %d dead)", running["job_id"], pid)
            now = datetime.now(timezone.utc).isoformat()
            db_update_job(running["job_id"], status="error", finished_at=now)

    with _lock:
        _try_start_next_locked()


# ── Backward-compatible helpers ───────────────────────────────────────────────

def _build_result(job: dict) -> dict:
    job_id = job["job_id"]
    output = _read_tail(job_id)
    # Parse token usage from the already-read output (avoids reading the file twice).
    token_usage = _parse_token_usage_from_text(output)
    # Codex's context limit is ~200k tokens. Flag if usage is high enough
    # that the output may have been cut short before the task was complete.
    possibly_truncated = token_usage is not None and token_usage >= 120_000
    
    agent_type = job.get("agent_type", "codex")
    
    result = {
        "job_id":      job_id,
        "status":      job["status"],
        "exit_code":   job.get("exit_code"),
        "output":      output,
        "prompt":      job["prompt"],
        "cwd":         job["cwd"],
        "started_at":  job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "token_usage": token_usage,
    }
    
    if possibly_truncated:
        result["possibly_truncated"] = True
        result["truncation_warning"] = (
            f"Agent used {token_usage:,} tokens — output may be incomplete. "
            "Read the output carefully, check what was NOT done, and resume "
            "with a follow-up task if needed."
        )
        
    if agent_type == "cursor" and job["status"] == "error":
        if "Authentication required" in output:
            result["error_hint"] = "Cursor CLI requires authentication. Run 'agent login' in the terminal or set the CURSOR_API_KEY environment variable."
        elif "command not found" in output:
            result["error_hint"] = "Cursor CLI (agent) is not installed or not in PATH."
            
    return result


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
    logger.info("Job %s cancelled (killed=%s)", job_id, killed)

    with _lock:
        _active_procs.pop(job_id, None)
        _try_start_next_locked()        # advance queue

    _notify_completion(job_id)
    return {"job_id": job_id, "status": "cancelled", "killed": killed}
