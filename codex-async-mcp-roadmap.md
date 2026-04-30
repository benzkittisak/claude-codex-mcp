# Codex Async MCP — Roadmap

## Problem

The official Codex MCP runs synchronously inside the MCP request-response cycle.
Long-running tasks hit the MCP protocol timeout (`-32001`) before Codex finishes,
forcing us to either keep tasks artificially small or bypass Codex entirely.

## Solution

Build a lightweight local MCP server that wraps the `codex` CLI asynchronously:

- Accept a task → spawn `codex` as a background subprocess → return a `job_id` immediately (no blocking, no timeout)
- A separate poll tool lets Claude check status and retrieve output when ready

---

## Architecture

```
Claude (Cowork/Claude Code)
        │
        │  MCP protocol
        ▼
codex-async-mcp  (local Python server)
        │
        ├── codex_start(prompt, cwd)  →  job_id  (instant)
        ├── codex_poll(job_id)        →  status + output
        └── codex_cancel(job_id)      →  (optional)
        │
        │  subprocess.Popen
        ▼
codex CLI  (already installed on machine)
        │
        └── writes stdout/stderr → ~/.codex-async/jobs/{job_id}/output.txt
```

State per job stored in `~/.codex-async/jobs/{job_id}/meta.json`:

```json
{
  "job_id": "abc123",
  "status": "running | done | error",
  "prompt": "...",
  "cwd": "/path/to/repo",
  "started_at": "2026-04-29T10:00:00",
  "finished_at": null,
  "exit_code": null
}
```

---

## Implementation Phases

### Phase 1 — Core server (MVP)

- [ ] Bootstrap Python project with `fastmcp`
- [ ] `codex_start(prompt, cwd, approval_policy?)` tool
  - Spawns `codex --approval-mode <policy> "<prompt>"` via `subprocess.Popen`
  - Redirects stdout + stderr to `~/.codex-async/jobs/{job_id}/output.txt`
  - Writes `meta.json` with status `running`
  - Returns `job_id` immediately
- [ ] `codex_poll(job_id)` tool
  - Reads `meta.json` → check if subprocess PID is still alive
  - If done: update status, capture exit code, return output tail
  - If still running: return `{status: "running", output_so_far: "..."}`
- [ ] Register with Claude via `claude mcp add`

### Phase 2 — Quality of life

- [ ] `codex_list()` — list recent jobs with status
- [ ] `codex_cancel(job_id)` — kill subprocess + mark cancelled
- [ ] Auto-cleanup of jobs older than N days
- [ ] Output streaming (return last N lines of output while still running)

### Phase 3 — Polish

- [ ] Config file (`~/.codex-async/config.json`) for default `approval_policy`, `codex_path`
- [ ] Web UI (optional) — simple HTML page to browse job history
- [ ] Homebrew formula or install script for easy setup

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | fastmcp is Python-native, minimal boilerplate |
| MCP framework | `fastmcp` | Simplest way to build MCP servers |
| Process management | `subprocess.Popen` | Standard library, no extra deps |
| State storage | JSON files | No DB needed, easy to inspect manually |
| Install | `pip install -e .` + `claude mcp add` | Works with existing Claude Code setup |

---

## File Structure

```
codex-async-mcp/
├── README.md
├── pyproject.toml
├── src/
│   └── codex_async_mcp/
│       ├── __init__.py
│       ├── server.py       # MCP server entry point
│       ├── job_manager.py  # spawn / poll / cancel logic
│       └── config.py       # paths, defaults
└── tests/
    └── test_job_manager.py
```

---

## Setup (after implementation)

```bash
# 1. Clone / create project
cd ~/projects
git init codex-async-mcp && cd codex-async-mcp

# 2. Install
pip install -e ".[dev]"

# 3. Register MCP with Claude
claude mcp add codex-async -- python -m codex_async_mcp.server

# 4. Verify
claude mcp list
```

---

## Usage Pattern (Claude side)

```
# Instead of one Codex MCP call that might timeout:
codex_start(
  prompt="In prorate_calculation_service.rb line 96, change format(...) to number_to_currency(...)",
  cwd="/Users/bbgummybear/payrollservice-thailand"
)
# → { job_id: "f3a9b2", status: "running" }

# Poll until done:
codex_poll(job_id="f3a9b2")
# → { status: "running", output_so_far: "Reading file..." }

codex_poll(job_id="f3a9b2")
# → { status: "done", exit_code: 0, output: "Applied changes to prorate_calculation_service.rb" }
```

---

## Open Questions

- Should `codex_poll` block for a few seconds (long-poll) or return immediately?
- Max output size to return in a single poll response?
- Run MCP server as a persistent daemon or spawn per-request?
