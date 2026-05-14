"""codex-async CLI — manage MCP server registrations across agents."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MCP_NAME = "codex-async"
PYTHON = sys.executable


def _mcp_entry(python: str) -> dict:
    return {"command": python, "args": ["-m", "codex_async_mcp.server"]}


def _merge_json(path: Path, python: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("mcpServers", {})[MCP_NAME] = _mcp_entry(python)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _remove_json(path: Path) -> None:
    if not path.exists():
        return
    config = json.loads(path.read_text())
    config.get("mcpServers", {}).pop(MCP_NAME, None)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _registered_json(path: Path) -> bool:
    if not path.exists():
        return False
    return MCP_NAME in json.loads(path.read_text()).get("mcpServers", {})


# ── Claude Code CLI ───────────────────────────────────────────────────────────

def _add_claude_code(python: str) -> None:
    subprocess.run(["claude", "mcp", "remove", MCP_NAME, "-s", "user"], capture_output=True)
    subprocess.run(
        ["claude", "mcp", "add", MCP_NAME, "-s", "user", "--", python, "-m", "codex_async_mcp.server"],
        check=True,
    )


def _remove_claude_code() -> None:
    subprocess.run(["claude", "mcp", "remove", MCP_NAME, "-s", "user"], check=True)


def _status_claude_code() -> bool:
    r = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True)
    return MCP_NAME in r.stdout


# ── Cursor IDE ────────────────────────────────────────────────────────────────

def _cursor_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


# ── Claude Desktop ────────────────────────────────────────────────────────────

def _desktop_path() -> Path | None:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("linux"):
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    return None


# ── Agent registry ────────────────────────────────────────────────────────────

AGENTS: dict[str, dict] = {
    "claude-code": {
        "label": "Claude Code CLI",
        "detect": lambda: shutil.which("claude") is not None,
        "add": _add_claude_code,
        "remove": _remove_claude_code,
        "status": _status_claude_code,
    },
    "cursor": {
        "label": "Cursor IDE",
        "detect": lambda: shutil.which("cursor") is not None or _cursor_path().parent.exists(),
        "add": lambda python: _merge_json(_cursor_path(), python),
        "remove": lambda: _remove_json(_cursor_path()),
        "status": lambda: _registered_json(_cursor_path()),
    },
    "claude-desktop": {
        "label": "Claude Desktop",
        "detect": lambda: (p := _desktop_path()) is not None and p.parent.exists(),
        "add": lambda python: _merge_json(_desktop_path(), python),
        "remove": lambda: _remove_json(_desktop_path()),
        "status": lambda: _registered_json(_desktop_path()) if _desktop_path() else False,
    },
}


# ── Commands ──────────────────────────────────────────────────────────────────

def _print_table() -> None:
    print(f"\n  {'Agent':<20} {'Detected':<12} {'Registered'}")
    print(f"  {'-'*20} {'-'*11} {'-'*10}")
    for name, info in AGENTS.items():
        detected = "yes" if info["detect"]() else "no"
        try:
            registered = "yes" if info["status"]() else "no"
        except Exception:
            registered = "error"
        print(f"  {name:<20} {detected:<12} {registered}")
    print()


def cmd_add(agent: str, python: str) -> None:
    info = AGENTS[agent]
    print(f"Registering {info['label']}... ", end="", flush=True)
    try:
        info["add"](python)
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_remove(agent: str) -> None:
    info = AGENTS[agent]
    print(f"Removing {info['label']}... ", end="", flush=True)
    try:
        info["remove"]()
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex-async",
        description="Manage codex-async-mcp registrations across AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add-agent", help="Register with an agent")
    p_add.add_argument("agent", choices=list(AGENTS))
    p_add.add_argument("--python", default=PYTHON, help="Python executable (default: current)")

    p_rm = sub.add_parser("remove-agent", help="Remove registration from an agent")
    p_rm.add_argument("agent", choices=list(AGENTS))

    sub.add_parser("list-agents", help="Show all detected and registered agents")

    args = parser.parse_args()

    if args.command == "add-agent":
        cmd_add(args.agent, args.python)
    elif args.command == "remove-agent":
        cmd_remove(args.agent)
    elif args.command == "list-agents":
        _print_table()


if __name__ == "__main__":
    main()
