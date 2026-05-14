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


# ── Codex ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def codex_start(
    prompt: str,
    cwd: str,
    approval_policy: str = "full-auto",
    context_files: list[str] | None = None,
) -> dict:
    """Queue a Codex task. Returns job_id. Use codex_wait(job_id) to get result.

    Args:
        prompt: Task description.
        cwd: Absolute working directory.
        approval_policy: 'suggest' | 'auto-edit' | 'full-auto'.
        context_files: Optional file paths to inject into prompt.
    """
    return start_job(prompt, cwd, approval_policy, "codex", context_files)


@mcp.tool()
def codex_wait(job_id: str, timeout_seconds: float = 50) -> dict:
    """Block until codex job finishes. Returns result or {"status":"timeout"} — loop again if timeout.

    Args:
        job_id: From codex_start.
        timeout_seconds: Max block per call. Default 50 (safe under MCP 60s limit).
    """
    return wait_for_job(job_id, timeout_seconds)


@mcp.tool()
def codex_await_any(timeout_seconds: float = 50) -> dict:
    """Block until any queued job completes. Loop on timeout."""
    return await_any_completion(timeout_seconds)


# ── Cursor ────────────────────────────────────────────────────────────────────

@mcp.tool()
def cursor_start(
    prompt: str,
    cwd: str,
    approval_policy: str = "full-auto",
    context_files: list[str] | None = None,
) -> dict:
    """Queue a Cursor headless task. Returns job_id. Use cursor_wait(job_id) to get result. .cursorrules auto-injected.

    Args:
        prompt: Task description.
        cwd: Absolute working directory.
        approval_policy: Passed to backend.
        context_files: Optional file paths to inject.
    """
    return start_job(prompt, cwd, approval_policy, "cursor", context_files)


@mcp.tool()
def cursor_wait(job_id: str, timeout_seconds: float = 50) -> dict:
    """Block until cursor job finishes. Returns result or {"status":"timeout"} — loop again if timeout."""
    return wait_for_job(job_id, timeout_seconds)


# ── Gemini ────────────────────────────────────────────────────────────────────

@mcp.tool()
def gemini_start(
    prompt: str,
    cwd: str,
    approval_policy: str = "full-auto",
    context_files: list[str] | None = None,
) -> dict:
    """Queue a Gemini CLI task. Returns job_id. Use gemini_wait(job_id) to get result.

    Args:
        prompt: Task description.
        cwd: Absolute working directory.
        approval_policy: 'suggest' | 'auto-edit' | 'full-auto'.
        context_files: Optional file paths to inject.
    """
    return start_job(prompt, cwd, approval_policy, "gemini", context_files)


@mcp.tool()
def gemini_wait(job_id: str, timeout_seconds: float = 50) -> dict:
    """Block until Gemini job finishes. Returns result or {"status":"timeout"} — loop again if timeout."""
    return wait_for_job(job_id, timeout_seconds)


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
    context_files: list[str] | None = None,
) -> dict:
    """Ask Gemini to write/publish a Confluence page. publish=True calls Confluence API."""
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
    context_files: list[str] | None = None,
) -> dict:
    """Ask Gemini to write/publish a PR. publish=True runs gh pr create/edit."""
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


# ── Shared / Queue ────────────────────────────────────────────────────────────

@mcp.tool()
def job_list(limit: int = 20) -> list:
    """List recent jobs (all agents), newest first."""
    return list_jobs(limit)


@mcp.tool()
def job_cancel(job_id: str) -> dict:
    """Cancel a running or pending job. Next pending job starts automatically."""
    return cancel_job(job_id)


@mcp.tool()
def queue_status() -> dict:
    """Snapshot of job queue: running job, pending count, recent completed."""
    return get_queue_status()


@mcp.tool()
def agent_notify_done(job_id: str, summary: str = "") -> dict:
    """Called BY any agent to signal task completion, unblocking *_wait immediately.

    Args:
        job_id: Embedded in task prompt by *_start.
        summary: One-sentence description of what was done.
    """
    return notify_job_done(job_id, summary)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
