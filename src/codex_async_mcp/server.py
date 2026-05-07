from fastmcp import FastMCP

from .db import init_db
from .job_manager import (
    await_any_completion,
    cancel_job,
    get_queue_status,
    list_jobs,
    poll_job,
    recover_on_startup,
    start_job,
    wait_for_job,
)

# Initialise SQLite schema and recover any stale jobs on server start.
init_db()
recover_on_startup()

mcp = FastMCP("codex-async-mcp")


# ── Existing tools (unchanged API) ────────────────────────────────────────────

@mcp.tool()
def codex_start(prompt: str, cwd: str, approval_policy: str = "full-auto") -> dict:
    """
    Queue a codex task and start it immediately (or enqueue it if another job is running).

    Returns a job_id instantly — never blocks or times out.
    The queue is sequential: only one Codex process runs at a time.
    When the current job finishes, the next pending job starts automatically.

    Use codex_wait(job_id) — NOT codex_poll — to be notified when the job finishes.

    Args:
        prompt: The task description to pass to codex.
        cwd: Absolute path to the working directory for codex.
        approval_policy: One of 'suggest', 'auto-edit', 'full-auto'. Default: 'full-auto'.
                         Always use 'full-auto' for autonomous Claude→Codex workflows.
    """
    return start_job(prompt, cwd, approval_policy)


@mcp.tool()
def codex_poll(job_id: str, tail_lines: int = 100) -> dict:
    """
    Non-blocking status snapshot for a codex job (backward-compat).

    Prefer codex_wait() for new workflows — it returns immediately when done
    without requiring repeated calls.

    Args:
        job_id: The job_id returned by codex_start.
        tail_lines: Trailing lines of output to return. Default: 100.
    """
    return poll_job(job_id, tail_lines)


@mcp.tool()
def codex_list(limit: int = 20) -> list:
    """
    List recent codex jobs (newest first) with status and prompt summaries.

    Args:
        limit: Max jobs to return. Default: 20.
    """
    return list_jobs(limit)


@mcp.tool()
def codex_cancel(job_id: str) -> dict:
    """
    Cancel a running or pending codex job.

    Running jobs receive SIGTERM; pending jobs are removed from the queue.
    The next pending job starts automatically after cancellation.

    Args:
        job_id: The job_id returned by codex_start.
    """
    return cancel_job(job_id)


# ── New tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def codex_wait(job_id: str, timeout_seconds: float = 300) -> dict:
    """
    Block until a codex job finishes, then return its full result.

    This is the preferred way to wait for a job — it returns the moment
    the job completes without any polling overhead.

    Returns immediately if the job is already done/error/cancelled.
    If the job exceeds timeout_seconds, returns {"status": "timeout"} and
    you can call codex_wait() again to keep waiting.

    Recommended workflow:
        job = codex_start(prompt, cwd, "full-auto")
        result = codex_wait(job["job_id"])        # blocks here
        # result.status == "done" → inspect result.output, decide next step

    Args:
        job_id: The job_id returned by codex_start.
        timeout_seconds: Max seconds to block. Default: 300 (5 min).
    """
    return                (job_id, timeout_seconds)


@mcp.tool()
def codex_await_any(timeout_seconds: float = 300) -> dict:
    """
    Block until any queued or running job completes, then return its result.

    Useful when you have queued several jobs and want to process each result
    as it arrives without tracking individual job_ids.

    Args:
        timeout_seconds: Max seconds to block. Default: 300 (5 min).
    """
    return await_any_completion(timeout_seconds)


@mcp.tool()
def codex_queue_status() -> dict:
    """
    Return a snapshot of the job queue.

    Shows:
      • running  — the currently executing job (or null)
      • pending  — jobs waiting to run, in FIFO order with queue positions
      • recent_completed — the last 5 finished/error/cancelled jobs

    Call this before starting new jobs to understand queue depth,
    or to check on progress without blocking.
    """
    return get_queue_status()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
