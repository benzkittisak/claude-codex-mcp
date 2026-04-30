# codex-async-mcp

Local MCP server that wraps the `codex` CLI asynchronously — returns a `job_id` immediately instead of blocking, so Claude never hits the MCP protocol timeout (`-32001`).

## Requirements

- Python 3.11+
- `codex` CLI installed and on `$PATH` (v0.125.0+)
- Claude Code CLI

---

## Installation

```bash
cd ~/payroll-mcp   # or wherever this repo lives
pip install -e ".[dev]"
```

Verify:

```bash
python -c "from codex_async_mcp.server import mcp; print(mcp.name)"
# → codex-async-mcp
```

---

## Register with Claude

### Global (all projects)

```bash
claude mcp add codex-async -s user -- python -m codex_async_mcp.server
```

### Project-only

```bash
cd ~/payrollservice-thailand   # or any project
claude mcp add codex-async -- python -m codex_async_mcp.server
```

### Verify

```bash
claude mcp list
# codex-async: python -m codex_async_mcp.server - ✓ Connected
```

### Add tool permissions (settings.local.json)

```json
{
  "permissions": {
    "allow": [
      "mcp__codex-async__codex_start",
      "mcp__codex-async__codex_poll",
      "mcp__codex-async__codex_list",
      "mcp__codex-async__codex_cancel"
    ]
  }
}
```

---

## Tools

| Tool | Description |
|------|-------------|
| `codex_start(prompt, cwd, approval_policy?)` | Start codex in background → returns `job_id` instantly |
| `codex_poll(job_id, tail_lines?)` | Check status + output tail |
| `codex_list(limit?)` | List recent jobs (newest first) |
| `codex_cancel(job_id)` | Kill running job |

### approval_policy values

| Value | Codex flag | Behavior |
|-------|-----------|----------|
| `suggest` | `-s read-only` | Read-only sandbox, no writes |
| `auto-edit` | `--full-auto` | Auto-applies edits |
| `full-auto` | `--dangerously-bypass-approvals-and-sandbox` | No prompts, no sandbox |

**For Claude automation always use `full-auto`** — `suggest` mode waits for interactive input which will never arrive inside a subprocess.

### Example usage

```
codex_start(
  prompt="In app/services/prorate_calculation_service.rb line 96, change format(...) to number_to_currency(...)",
  cwd="/Users/bbgummybear/payrollservice-thailand",
  approval_policy="full-auto"
)
# → { job_id: "f3a9b2", status: "running", pid: 12345 }

codex_poll(job_id="f3a9b2")
# → { status: "running", output: "Reading file..." }

codex_poll(job_id="f3a9b2")
# → { status: "done", exit_code: 0, output: "Applied changes to prorate_calculation_service.rb" }
```

---

## Job state

Jobs are stored in `~/.codex-async/jobs/{job_id}/`:

```
~/.codex-async/jobs/f3a9b2/
  meta.json     ← status, pid, timestamps, exit_code
  output.txt    ← stdout + stderr from codex
```

`meta.json` structure:

```json
{
  "job_id": "f3a9b2",
  "status": "running | done | error | cancelled",
  "prompt": "...",
  "cwd": "/path/to/repo",
  "approval_policy": "full-auto",
  "pid": 12345,
  "started_at": "2026-04-29T10:00:00+00:00",
  "finished_at": null,
  "exit_code": null
}
```

---

## Troubleshooting

### `codex-async: ... - ✗ Failed` in `claude mcp list`

Python can't be found or the package isn't installed in the right environment.

```bash
# Check which python Claude is using
which python

# If using conda, register with the full path
claude mcp add codex-async -s user -- /Users/bbgummybear/miniconda3/bin/python -m codex_async_mcp.server

# Verify the package is installed in that environment
/Users/bbgummybear/miniconda3/bin/python -c "import codex_async_mcp; print('ok')"
```

---

### `status: "error"` immediately after `codex_start`

Codex failed to start. Check the raw output:

```bash
cat ~/.codex-async/jobs/<job_id>/output.txt
```

Common causes:

| Output message | Fix |
|----------------|-----|
| `command not found: codex` | `codex` not on PATH — add to shell profile or set `CODEX_BIN` in `config.py` |
| `unknown flag: --dangerously-bypass-approvals-and-sandbox` | Codex version < 0.125.0 — run `npm install -g @openai/codex` to upgrade |
| `permission denied` | `cwd` doesn't exist or Claude doesn't have access |

---

### `status: "running"` forever, never finishes

The subprocess is hung (waiting for input or stuck in a loop).

```bash
# Check if the process is still alive
ps aux | grep codex

# Check live output
tail -f ~/.codex-async/jobs/<job_id>/output.txt

# Cancel the job
codex_cancel(job_id="<job_id>")
```

Most common cause: using `approval_policy="suggest"` which pauses for interactive approval. Use `"full-auto"` instead.

---

### Job shows `status: "running"` after server restart

The MCP server lost the in-memory `Popen` registry on restart. The next `codex_poll` call will detect the PID is dead and update the status automatically.

```bash
codex_poll(job_id="<job_id>")
# → { status: "done", ... }   ← auto-resolved on first poll
```

---

### Old jobs filling up disk

```bash
# View all jobs sorted by date
ls -lt ~/.codex-async/jobs/

# Delete jobs older than 7 days
find ~/.codex-async/jobs -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
```

---

## Project structure

```
codex-async-mcp/
├── README.md
├── pyproject.toml
├── src/
│   └── codex_async_mcp/
│       ├── __init__.py
│       ├── server.py        # MCP entry point, tool definitions
│       ├── job_manager.py   # spawn / poll / cancel / list
│       └── config.py        # JOBS_DIR, CODEX_BIN, defaults
└── tests/
    └── test_job_manager.py
```

## Run tests

```bash
pytest tests/ -v
```
