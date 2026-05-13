from fastmcp import FastMCP

from .db import init_db
from .job_manager import (
    await_any_completion,
    cancel_job,
    get_queue_status,
    list_jobs,
    notify_job_done,
    recover_on_startup,
    start_job,
    wait_for_job,
)

# Initialise SQLite schema and recover any stale jobs on server start.
init_db()
recover_on_startup()

mcp = FastMCP("codex-async-mcp")


def _tag(name: str, value: str | None) -> str:
    return f"<{name}>{value}</{name}>\n" if value else ""


def _build_confluence_prompt(
    title: str,
    source_context: str = "",
    space_key: str = "",
    page_url: str = "",
    audience: str = "",
    instructions: str = "",
    publish: bool = False,
) -> str:
    if publish:
        publish_rule = (
            "publish=true: use Confluence tooling. "
            "If page_url given, update; else create in space_key. "
            "If unavailable, return draft + explain blocker."
        )
    else:
        publish_rule = "publish=false: return draft only, do not call Confluence API."

    return (
        "Task: write Confluence page.\n"
        f"<title>{title}</title>\n"
        f"{_tag('space_key', space_key)}"
        f"{_tag('page_url', page_url)}"
        f"{_tag('audience', audience)}"
        f"{_tag('instructions', instructions)}"
        f"<publish_rule>{publish_rule}</publish_rule>\n"
        f"<source>{source_context or 'inspect repo + git state'}</source>\n"
        "Sections: context, decisions, implementation, rollout/QA, open questions. "
        "Engineering detail over marketing."
    )


def _build_pr_prompt(
    pr_goal: str = "",
    base_branch: str = "",
    compare_branch: str = "",
    pr_number: str = "",
    instructions: str = "",
    publish: bool = False,
    draft: bool = True,
) -> str:
    if publish:
        publish_rule = (
            f"publish=true: use `gh`. Update pr_number if given, else create as "
            f"{'draft' if draft else 'ready'}. If unavailable, return title/body + blocker."
        )
    else:
        publish_rule = "publish=false: return title/body only, no gh calls."

    return (
        "Task: write PR title + body from repo state.\n"
        f"{_tag('goal', pr_goal)}"
        f"{_tag('base', base_branch)}"
        f"{_tag('compare', compare_branch)}"
        f"{_tag('pr_number', pr_number)}"
        f"{_tag('instructions', instructions)}"
        f"<publish_rule>{publish_rule}</publish_rule>\n"
        "Inspect git branch/diff/log/tests. Body: summary, impl details, verification."
    )


# ── Existing tools (unchanged API) ────────────────────────────────────────────

@mcp.tool()
def codex_start(
    prompt: str,
    cwd: str,
    approval_policy: str = "full-auto",
    context_files: list[str] | None = None
) -> dict:
    """Queue codex task. Best for: fast single-file edits, bug fixes, boilerplate.

    Returns job_id. Use codex_wait(job_id) for completion.

    Args:
        prompt: Task description.
        cwd: Absolute working directory.
        approval_policy: 'suggest' | 'auto-edit' | 'full-auto'.
        context_files: Optional file paths to inject.
    """
    return start_job(prompt, cwd, approval_policy, "codex", context_files)



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


# ── Cursor Tools (Aliases) ────────────────────────────────────────────────────

@mcp.tool()
def cursor_start(
    prompt: str,
    cwd: str,
    approval_policy: str = "full-auto",
    context_files: list[str] | None = None
) -> dict:
    """Queue Cursor headless task. Best for: multi-file refactor, architecture, deep repo context.

    Returns job_id. Use cursor_wait(job_id) for completion. `.cursorrules` auto-injected.

    Args:
        prompt: Task description.
        cwd: Absolute working directory.
        approval_policy: Passed to backend.
        context_files: Optional file paths to inject.
    """
    return start_job(prompt, cwd, approval_policy, agent_type="cursor", context_files=context_files)

@mcp.tool()
def cursor_wait(job_id: str, timeout_seconds: float = 50) -> dict:
    """Block until a cursor job finishes. Default 50s — safe under MCP ~60s limit."""
    return wait_for_job(job_id, timeout_seconds)

@mcp.tool()
def cursor_list(limit: int = 20) -> list:
    """List recent cursor/codex jobs."""
    return list_jobs(limit)

@mcp.tool()
def cursor_cancel(job_id: str) -> dict:
    """Cancel a running or pending cursor job."""
    return cancel_job(job_id)

@mcp.tool()
def cursor_queue_status() -> dict:
    """Return a snapshot of the shared job queue."""
    return get_queue_status()


# ── Gemini Tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def gemini_start(
    prompt: str,
    cwd: str,
    approval_policy: str = "full-auto",
    context_files: list[str] | None = None
) -> dict:
    """Queue Gemini CLI task. Best for: long-form writing, synthesis, Confluence/PR drafts.

    Args:
        prompt: Task description.
        cwd: Absolute working directory.
        approval_policy: 'suggest' | 'auto-edit' | 'full-auto'.
        context_files: Optional file paths to inject.
    """
    return start_job(prompt, cwd, approval_policy, "gemini", context_files)


@mcp.tool()
def gemini_confluence_start(
    title: str,
    cwd: str,
    source_context: str = "",
    space_key: str = "",
    page_url: str = "",
    audience: str = "",
    instructions: str = "",
    publish: bool = False,
    approval_policy: str = "suggest",
    context_files: list[str] | None = None
) -> dict:
    """
    Ask Gemini CLI to write a Confluence page draft, or publish/update it when requested.

    Defaults to draft-only mode. Set publish=True only when the caller explicitly
    wants Gemini to use available Confluence tooling or authentication.
    """
    prompt = _build_confluence_prompt(
        title=title,
        source_context=source_context,
        space_key=space_key,
        page_url=page_url,
        audience=audience,
        instructions=instructions,
        publish=publish,
    )
    return start_job(prompt, cwd, approval_policy, "gemini", context_files)


@mcp.tool()
def gemini_pr_start(
    cwd: str,
    pr_goal: str = "",
    base_branch: str = "",
    compare_branch: str = "",
    pr_number: str = "",
    instructions: str = "",
    publish: bool = False,
    draft: bool = True,
    approval_policy: str = "suggest",
    context_files: list[str] | None = None
) -> dict:
    """
    Ask Gemini CLI to write a PR title/body, or create/update the PR when requested.

    Defaults to draft-only mode. Set publish=True only when the caller explicitly
    wants Gemini to use `gh pr create` or `gh pr edit`.
    """
    prompt = _build_pr_prompt(
        pr_goal=pr_goal,
        base_branch=base_branch,
        compare_branch=compare_branch,
        pr_number=pr_number,
        instructions=instructions,
        publish=publish,
        draft=draft,
    )
    return start_job(prompt, cwd, approval_policy, "gemini", context_files)


@mcp.tool()
def gemini_wait(job_id: str, timeout_seconds: float = 50) -> dict:
    """Block until a Gemini job finishes. Default 50s — safe under MCP ~60s limit."""
    return wait_for_job(job_id, timeout_seconds)


@mcp.tool()
def gemini_list(limit: int = 20) -> list:
    """List recent Gemini/Codex/Cursor jobs."""
    return list_jobs(limit)


@mcp.tool()
def gemini_cancel(job_id: str) -> dict:
    """Cancel a running or pending Gemini job."""
    return cancel_job(job_id)


@mcp.tool()
def gemini_queue_status() -> dict:
    """Return a snapshot of the shared job queue."""
    return get_queue_status()


# ── New tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def codex_wait(job_id: str, timeout_seconds: float = 50) -> dict:
    """Block until codex job finishes, return result.

    Returns instantly if already done/error/cancelled.
    If still running after timeout_seconds: {"status": "timeout", "output": ...}.
    Loop again to keep waiting.

    Default 50s — safe under MCP ~60s drop limit. Lower if you want faster progress polling.

    Workflow:
        job = codex_start(prompt, cwd)
        while True:
            result = codex_wait(job["job_id"])
            if result["status"] != "timeout":
                break

    Args:
        job_id: From codex_start.
        timeout_seconds: Max block per call. Default 50.
    """
    return wait_for_job(job_id, timeout_seconds)


@mcp.tool()
def codex_await_any(timeout_seconds: float = 50) -> dict:
    """Block until any job completes. Loop on timeout.

    Default 50s — safe under MCP ~60s drop limit.

    Args:
        timeout_seconds: Max block per call. Default 50.
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


@mcp.tool()
def codex_notify_done(job_id: str, summary: str = "") -> dict:
    """
    Called BY CODEX to signal that it has finished its task.

    This immediately unblocks any codex_wait() call in Claude without waiting
    for the Codex process to exit.  Codex should call this at the very end of
    every task it receives via codex_start.

    Args:
        job_id:  The job_id embedded in the task prompt by codex_start.
        summary: One-sentence description of what was done (appended to output).
    """
    return notify_job_done(job_id, summary)


@mcp.tool()
def gemini_notify_done(job_id: str, summary: str = "") -> dict:
    """
    Called BY GEMINI to signal that it has finished its task.

    Args:
        job_id: The job_id embedded in the task prompt by gemini_start.
        summary: One-sentence description of what was done.
    """
    return notify_job_done(job_id, summary)


@mcp.tool()
def agent_notify_done(job_id: str, summary: str = "") -> dict:
    """
    Generic completion callback for any queued agent.

    Args:
        job_id: The job_id embedded in the task prompt.
        summary: One-sentence description of what was done.
    """
    return notify_job_done(job_id, summary)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
