# agent-async-mcp

Local MCP server that runs Codex, Cursor, and Gemini CLI tasks asynchronously — returns a `job_id` immediately instead of blocking, so the orchestrating agent never hits the MCP 60-second timeout.

## How it works

```
Claude (orchestrator)
  │
  ├─ codex_start(prompt, cwd)  →  job_id (instant)
  │
  └─ codex_wait(job_id)        →  blocks up to 50 s, returns result
                                   loop again on timeout
```

A sequential queue ensures only one agent process runs at a time. Jobs are persisted in SQLite so the queue survives server restarts.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/benzkittisak/claude-codex-mcp/master/install.sh | bash
```

The installer will:
- Clone this repo to `~/.local/share/agent-async-mcp/`
- Create an isolated Python venv
- Symlink `agent-async` to `~/.local/bin/`
- Detect Claude Code, Codex, Cursor, Claude Desktop and ask which to register

**Uninstall:**

```bash
curl -fsSL https://raw.githubusercontent.com/benzkittisak/claude-codex-mcp/master/install.sh | bash -s uninstall
# or, if already installed:
agent-async uninstall
```

---

## CLI

```bash
agent-async list-agents              # show detected / registered agents
agent-async add-agent claude-code    # register with Claude Code CLI
agent-async add-agent codex          # register with Codex CLI
agent-async add-agent cursor         # register with Cursor IDE
agent-async add-agent claude-desktop # register with Claude Desktop
agent-async remove-agent <agent>     # unregister
agent-async status                   # open real-time job monitor
agent-async update                   # pull latest + reinstall
agent-async check-update             # check without installing
agent-async enable-auto-update       # schedule daily auto-update (09:00)
agent-async disable-auto-update      # remove scheduled auto-update
agent-async uninstall                # remove everything
```

---

## Requirements

- Python 3.11+
- One or more agent CLIs: `codex`, `cursor`, `gemini` (optional — only needed for the tools you use)
- Claude Code CLI (recommended orchestrator)

---

## MCP Tools (13 total)

### Codex

| Tool | Description |
|------|-------------|
| `codex_start(prompt, cwd, approval_policy?, context_files?)` | Queue a Codex task → returns `job_id` instantly |
| `codex_wait(job_id, timeout_seconds=50)` | Block until done; loop on `{"status":"timeout"}` |
| `codex_await_any(timeout_seconds=50)` | Block until ANY queued job completes |

### Cursor

| Tool | Description |
|------|-------------|
| `cursor_start(prompt, cwd, approval_policy?, context_files?)` | Queue a Cursor headless task → `job_id` |
| `cursor_wait(job_id, timeout_seconds=50)` | Block until done |

### Gemini

| Tool | Description |
|------|-------------|
| `gemini_start(prompt, cwd, approval_policy?, context_files?)` | Queue a Gemini CLI task → `job_id` |
| `gemini_wait(job_id, timeout_seconds=50)` | Block until done |
| `gemini_confluence_start(title, cwd, ...)` | Ask Gemini to draft/publish a Confluence page |
| `gemini_pr_start(cwd, pr_goal, ...)` | Ask Gemini to draft/publish a PR |

### Shared / Queue

| Tool | Description |
|------|-------------|
| `job_list(limit=20)` | List recent jobs (all agents), newest first |
| `job_cancel(job_id)` | Cancel running or pending job |
| `queue_status()` | `{"busy": bool, "pending_count": int}` |
| `agent_notify_done(job_id, summary?)` | Called BY an agent to signal completion |

### `approval_policy` values

| Value | Behavior |
|-------|----------|
| `full-auto` | No prompts, no sandbox (use for automation) |
| `auto-edit` | Auto-applies edits |
| `suggest` | Read-only — pauses for interactive input (avoid in automation) |

---

## Permissions (settings.local.json)

Add to your Claude Code project's `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__agent-async__codex_start",   "mcp__agent-async__codex_wait",
      "mcp__agent-async__cursor_start",  "mcp__agent-async__cursor_wait",
      "mcp__agent-async__gemini_start",  "mcp__agent-async__gemini_wait",
      "mcp__agent-async__queue_status",  "mcp__agent-async__job_list",
      "mcp__agent-async__job_cancel",    "mcp__agent-async__agent_notify_done",
      "mcp__agent-async__codex_await_any"
    ]
  }
}
```

---

## Usage pattern

```python
# Start a job (returns immediately)
result = codex_start(
    prompt="In app/services/foo.rb line 42, change X to Y. Do not change anything else.",
    cwd="/path/to/repo",
    approval_policy="full-auto"
)
job_id = result["job_id"]

# Wait in a loop (each call blocks up to 50 s)
while True:
    result = codex_wait(job_id, timeout_seconds=50)
    if result["status"] == "timeout":
        continue
    break  # "done" | "error" | "cancelled"
```

---

## Job data

Jobs are persisted in `~/.agent-async/`:

```
~/.agent-async/
  queue.db          ← SQLite: job metadata, status, token usage
  jobs/<job_id>/
    output.txt      ← stdout + stderr from the agent process
```

---

## Troubleshooting

### `agent-async: command not found`

`~/.local/bin` not in PATH. Run:

```bash
source ~/.zshrc   # or ~/.bashrc
```

Or open a new terminal. The installer adds it automatically.

### `status: "error"` immediately after `*_start`

The agent CLI failed to start. Check output:

```bash
cat ~/.agent-async/jobs/<job_id>/output.txt
```

| Message | Fix |
|---------|-----|
| `command not found: codex` | Install codex: `npm install -g @openai/codex` |
| `command not found: gemini` | Install gemini CLI from github.com/google-gemini/gemini-cli |
| `permission denied` | `cwd` doesn't exist or is inaccessible |

### `status: "running"` forever

The subprocess is hung. Most common cause: `approval_policy="suggest"` waiting for interactive input. Always use `"full-auto"` for automation.

```bash
agent-async status   # open monitor to see live state
```

Cancel a stuck job:

```python
job_cancel(job_id="<job_id>")
```

### Old jobs filling up disk

```bash
find ~/.agent-async/jobs -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
```

---

## Project structure

```
agent-async-mcp/
├── install.sh
├── mcp-monitor.py
├── pyproject.toml
└── src/
    └── agent_async_mcp/
        ├── server.py       # MCP entry point, tool definitions
        ├── job_manager.py  # queue, spawn, wait, cancel
        ├── db.py           # SQLite schema + helpers
        ├── config.py       # paths, timeouts, agent binaries
        └── cli.py          # agent-async CLI
```

## Development

```bash
git clone https://github.com/benzkittisak/claude-codex-mcp
cd claude-codex-mcp
pip install -e ".[dev]"
pytest tests/ -v
```
