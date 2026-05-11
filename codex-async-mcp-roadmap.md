# Codex Async MCP — Roadmap

## Problem

The official Codex MCP runs synchronously inside the MCP request-response cycle.
Long-running tasks hit the MCP protocol timeout (`-32001`) before Codex finishes,
forcing us to either keep tasks artificially small or bypass Codex entirely.

## Solution

Build a lightweight local MCP server that wraps the `codex` CLI asynchronously:

- Accept a task → spawn `codex` as a background subprocess → return a `job_id` immediately (no blocking, no timeout)
- A blocking-wait tool lets Claude be notified the moment a job finishes
- A sequential queue ensures only one Codex process runs at a time

---

## Architecture

```
Claude (Cowork/Claude Code)
        │
        │  MCP protocol
        ▼
codex-async-mcp  (local Python server)
        │
        ├── codex_start(prompt, cwd)     →  job_id  (instant, queued)
        ├── codex_wait(job_id)           →  blocks until done (≤10s per call)
        ├── codex_await_any()            →  blocks until any job finishes
        ├── codex_queue_status()         →  snapshot of running/pending/done
        ├── codex_list(limit)            →  recent jobs
        ├── codex_cancel(job_id)         →  kill running / remove pending
        └── codex_notify_done(job_id)    →  callback from Codex when task done
        │
        │  subprocess.Popen
        ▼
codex CLI  (already installed on machine)
        │
        └── writes stdout/stderr → ~/.codex-async/jobs/{job_id}/output.txt
```

State is stored in **SQLite** (`~/.codex-async/queue.db`) as the single source of truth.
Per-job output is streamed to `~/.codex-async/jobs/{job_id}/output.txt`.

### SQLite schema (jobs table)

| Column | Type | Description |
|---|---|---|
| job_id | TEXT PK | 8-char hex UUID |
| status | TEXT | pending / running / done / error / cancelled |
| prompt | TEXT | Task description |
| cwd | TEXT | Working directory |
| approval_policy | TEXT | suggest / auto-edit / full-auto |
| pid | INTEGER | OS process ID (nullable) |
| exit_code | INTEGER | Process exit code (nullable) |
| created_at | TEXT | ISO-8601 UTC |
| started_at | TEXT | When process was spawned (nullable) |
| finished_at | TEXT | When job reached terminal state (nullable) |

---

## Implementation Phases

### Phase 1 — Core server (MVP) ✅

- [x] Bootstrap Python project with `fastmcp`
- [x] `codex_start(prompt, cwd, approval_policy?)` tool
  - Spawns `codex exec` via `subprocess.Popen`
  - Redirects stdout + stderr to `~/.codex-async/jobs/{job_id}/output.txt`
  - Writes status to SQLite DB
  - Returns `job_id` immediately
- [x] Blocking wait via `codex_wait(job_id)` with 10s timeout per call
- [x] Register with Claude via `claude mcp add`

### Phase 2 — Queue & quality of life ✅

- [x] Sequential job queue — only one Codex process at a time
- [x] `codex_list()` — list recent jobs with status
- [x] `codex_cancel(job_id)` — kill subprocess + mark cancelled + advance queue
- [x] Output streaming (return last N lines of output while still running)
- [x] `codex_queue_status()` — snapshot of running/pending/done
- [x] `codex_await_any()` — block until any job completes
- [x] `codex_notify_done(job_id)` — Codex callback for instant completion signal
- [x] Stall detection — auto-SIGTERM after output stalls for 60s
- [x] Max job duration ceiling (30 min)
- [x] Server-restart recovery — detects dead PIDs and resumes queue
- [x] Token usage parsing and truncation warnings

### Phase 3 — Optimization ✅

- [x] Thread-local SQLite connection pooling
- [x] Composite index on (status, created_at)
- [x] Seek-from-end for large output files
- [x] Avoid double file reads in result building
- [x] Race condition fix between `notify_job_done()` and `_monitor()`
- [x] Structured logging throughout job lifecycle
- [ ] Auto-cleanup of old jobs (> 7 days)

### Phase 4 — Polish (future)

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
| State storage | **SQLite (WAL mode)** | Atomic, concurrent-safe, single source of truth |
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
│       ├── server.py       # MCP server entry point, tool definitions
│       ├── job_manager.py  # spawn / wait / cancel / queue logic
│       ├── db.py           # SQLite schema, CRUD operations
│       └── config.py       # paths, timeouts, defaults
└── tests/
    └── test_job_manager.py
```

---

## Setup

```bash
# 1. Clone / create project
cd ~/payroll-mcp

# 2. Install
pip install -e ".[dev]"

# 3. Register MCP with Claude
claude mcp add codex-async -s user -- python -m codex_async_mcp.server

# 4. Verify
claude mcp list
```

---

## Usage Pattern (Claude side)

```
# Start a task (queued automatically):
job = codex_start(
  prompt="In prorate_calculation_service.rb line 96, change format(...) to number_to_currency(...)",
  cwd="/Users/bbgummybear/payrollservice-thailand",
  approval_policy="full-auto"
)
# → { job_id: "f3a9b2", status: "running" }

# Block until done (loop with ≤10s timeout per call):
while True:
    result = codex_wait(job_id="f3a9b2")  # returns in ≤10 s
    if result["status"] != "timeout":
        break
    # log result["output"] to show progress, then loop

# → { status: "done", exit_code: 0, output: "Applied changes to ..." }
```
