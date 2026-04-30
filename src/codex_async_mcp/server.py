from fastmcp import FastMCP

from .job_manager import cancel_job, list_jobs, poll_job, start_job

mcp = FastMCP("codex-async-mcp")


@mcp.tool()
def codex_start(prompt: str, cwd: str, approval_policy: str = "suggest") -> dict:
    """
    Start a codex task asynchronously in the background.

    Returns a job_id immediately — does not block or timeout.
    Use codex_poll(job_id) to check progress.

    Args:
        prompt: The task description to pass to codex.
        cwd: Absolute path to the working directory for codex.
        approval_policy: One of 'suggest', 'auto-edit', 'full-auto'. Default: 'suggest'.
    """
    return start_job(prompt, cwd, approval_policy)


@mcp.tool()
def codex_poll(job_id: str, tail_lines: int = 100) -> dict:
    """
    Poll the status and output of a running (or finished) codex job.

    Args:
        job_id: The job_id returned by codex_start.
        tail_lines: How many trailing lines of output to return. Default: 100.

    Returns a dict with status ('running' | 'done' | 'error' | 'cancelled'),
    exit_code, and the latest output.
    """
    return poll_job(job_id, tail_lines)


@mcp.tool()
def codex_list(limit: int = 20) -> list:
    """
    List recent codex jobs with their status and prompt summaries.

    Args:
        limit: Max number of jobs to return (most recent first). Default: 20.
    """
    return list_jobs(limit)


@mcp.tool()
def codex_cancel(job_id: str) -> dict:
    """
    Cancel a running codex job by sending SIGTERM to the subprocess.

    Args:
        job_id: The job_id returned by codex_start.
    """
    return cancel_job(job_id)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
