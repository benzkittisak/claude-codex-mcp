"""codex-async CLI — manage MCP server registrations across agents."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

MCP_NAME = "codex-async"
PYTHON = sys.executable
INSTALL_DIR = Path.home() / ".local" / "share" / "codex-async-mcp"
VENV_DIR = INSTALL_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
VENV_PIP = VENV_DIR / "bin" / "pip"

# LaunchAgent / cron identifiers
_LAUNCH_AGENT_ID = "com.codex-async.update"
_LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_AGENT_ID}.plist"
_CRON_TAG = "# codex-async auto-update"


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


# ── Codex CLI ─────────────────────────────────────────────────────────────────

def _codex_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


# Matches [mcp_servers.<MCP_NAME>] and everything until the next [section] or EOF.
# Safe to use for add/remove — leaves all other config untouched.
def _codex_section_re() -> re.Pattern:
    # Match from [mcp_servers.<name>] up to (but not including) the next top-level
    # section header (line starting with '[') or end of file.
    return re.compile(
        r'\[mcp_servers\.' + re.escape(MCP_NAME) + r'\].*?(?=\n\[|\Z)',
        re.DOTALL,
    )


def _codex_entry(python: str) -> str:
    return (
        f'[mcp_servers.{MCP_NAME}]\n'
        f'command = "{python}"\n'
        f'args = [\n    "-m",\n    "codex_async_mcp.server",\n]\n'
    )


def _add_codex(python: str) -> None:
    path = _codex_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _codex_entry(python)
    if not path.exists():
        path.write_text(entry)
        return
    text = path.read_text()
    if f'[mcp_servers.{MCP_NAME}]' in text:
        text = _codex_section_re().sub(entry, text)
    else:
        text = text.rstrip('\n') + '\n\n' + entry
    path.write_text(text)


def _remove_codex() -> None:
    path = _codex_path()
    if not path.exists():
        return
    text = _codex_section_re().sub('', path.read_text()).strip('\n') + '\n'
    path.write_text(text)


def _status_codex() -> bool:
    path = _codex_path()
    if not path.exists():
        return False
    with open(path, "rb") as f:
        return MCP_NAME in tomllib.load(f).get("mcp_servers", {})


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
    "codex": {
        "label": "Codex CLI",
        "detect": lambda: shutil.which("codex") is not None or _codex_path().parent.exists(),
        "add": _add_codex,
        "remove": _remove_codex,
        "status": _status_codex,
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


# ── Update ────────────────────────────────────────────────────────────────────

def _resolve_install_dir() -> Path:
    if not (INSTALL_DIR / ".git").exists():
        print(f"Install directory not found: {INSTALL_DIR}", file=sys.stderr)
        print("Run the install script first.", file=sys.stderr)
        sys.exit(1)
    return INSTALL_DIR


def _resolve_pip() -> Path:
    pip = VENV_PIP
    if not pip.exists():
        print(f"venv pip not found: {pip}", file=sys.stderr)
        print("Re-run the install script to recreate the venv.", file=sys.stderr)
        sys.exit(1)
    return pip


def _local_sha(install_dir: Path) -> str:
    r = subprocess.run(["git", "-C", str(install_dir), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _remote_sha(install_dir: Path) -> str:
    subprocess.run(["git", "-C", str(install_dir), "fetch", "--quiet"],
                   capture_output=True, timeout=10)
    r = subprocess.run(["git", "-C", str(install_dir), "rev-parse", "@{u}"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def cmd_update(check_only: bool = False) -> None:
    install_dir = _resolve_install_dir()

    print("Checking for updates... ", end="", flush=True)
    try:
        local = _local_sha(install_dir)
        remote = _remote_sha(install_dir)
    except Exception as e:
        print(f"could not reach remote: {e}")
        return

    if local == remote:
        print("already up to date.")
        return

    print(f"update available ({local[:7]} → {remote[:7]})")

    if check_only:
        print("Run 'codex-async update' to apply.")
        return

    print("Pulling latest... ", end="", flush=True)
    subprocess.run(["git", "-C", str(install_dir), "pull", "--ff-only", "--quiet"], check=True)
    print("OK")

    pip = _resolve_pip()
    print("Reinstalling package... ", end="", flush=True)
    subprocess.run([str(pip), "install", "--quiet", "-e", str(install_dir)], check=True)
    print("OK")

    print("Done. Restart your agent to load the new version.")


# ── Auto-update ───────────────────────────────────────────────────────────────

def _codex_async_bin() -> str:
    """Resolved path to this CLI — used in scheduled job commands."""
    candidate = VENV_DIR / "bin" / "codex-async"
    return str(candidate) if candidate.exists() else shutil.which("codex-async") or "codex-async"


def cmd_enable_auto_update() -> None:
    cli = _codex_async_bin()

    if sys.platform == "darwin":
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>             <string>{_LAUNCH_AGENT_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{cli}</string>
        <string>update</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>9</integer>
        <key>Minute</key> <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>  <string>{Path.home()}/.codex-async/update.log</string>
    <key>StandardErrorPath</key> <string>{Path.home()}/.codex-async/update.log</string>
    <key>RunAtLoad</key> <false/>
</dict>
</plist>
"""
        _LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAUNCH_AGENT_PATH.write_text(plist)
        subprocess.run(["launchctl", "unload", str(_LAUNCH_AGENT_PATH)], capture_output=True)
        subprocess.run(["launchctl", "load", str(_LAUNCH_AGENT_PATH)], check=True)
        print(f"Auto-update enabled (daily 09:00). Log: ~/.codex-async/update.log")

    elif sys.platform.startswith("linux"):
        cron_line = f"0 9 * * * {cli} update >> ~/.codex-async/update.log 2>&1  {_CRON_TAG}"
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if _CRON_TAG in existing:
            print("Auto-update already enabled.")
            return
        new_crontab = existing.rstrip() + "\n" + cron_line + "\n"
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
        print("Auto-update enabled (daily 09:00). Log: ~/.codex-async/update.log")

    else:
        print("Auto-update not supported on this platform. Run 'codex-async update' manually.",
              file=sys.stderr)


def cmd_uninstall() -> None:
    import shutil as _shutil

    print("\nThis will remove:")
    print(f"  • {INSTALL_DIR}  (repo + venv)")
    print(f"  • {LOCAL_BIN}/codex-async  (symlink)")
    data_dir = Path.home() / ".codex-async"
    if data_dir.exists():
        print(f"  • {data_dir}  (job data — optional)")
    if _LAUNCH_AGENT_PATH.exists():
        print(f"  • {_LAUNCH_AGENT_PATH}  (auto-update)")
    print()
    try:
        ans = input("Proceed? [y/n]: ").strip().lower()
    except EOFError:
        ans = "n"
    if ans != "y":
        print("Aborted.")
        return

    # deregister all agents
    print("\nRemoving agent registrations...")
    for name, info in AGENTS.items():
        try:
            if info["status"]():
                info["remove"]()
                print(f"  removed: {name}")
        except Exception:
            pass

    # symlink
    symlink = Path(LOCAL_BIN) / MCP_NAME
    if symlink.is_symlink():
        symlink.unlink()
        print(f"Removed symlink: {symlink}")

    # install dir
    if INSTALL_DIR.exists():
        _shutil.rmtree(INSTALL_DIR)
        print(f"Removed: {INSTALL_DIR}")

    # job data (ask)
    if data_dir.exists():
        try:
            ans2 = input(f"Remove job data at {data_dir}? [y/n]: ").strip().lower()
        except EOFError:
            ans2 = "n"
        if ans2 == "y":
            _shutil.rmtree(data_dir)
            print(f"Removed: {data_dir}")

    # LaunchAgent
    if _LAUNCH_AGENT_PATH.exists():
        subprocess.run(["launchctl", "unload", str(_LAUNCH_AGENT_PATH)], capture_output=True)
        _LAUNCH_AGENT_PATH.unlink()
        print("Removed LaunchAgent auto-update.")

    print("\nUninstall complete.")


def cmd_disable_auto_update() -> None:
    if sys.platform == "darwin":
        if _LAUNCH_AGENT_PATH.exists():
            subprocess.run(["launchctl", "unload", str(_LAUNCH_AGENT_PATH)], capture_output=True)
            _LAUNCH_AGENT_PATH.unlink()
            print("Auto-update disabled.")
        else:
            print("Auto-update was not enabled.")

    elif sys.platform.startswith("linux"):
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if _CRON_TAG not in existing:
            print("Auto-update was not enabled.")
            return
        new_crontab = "\n".join(
            line for line in existing.splitlines() if _CRON_TAG not in line
        ) + "\n"
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
        print("Auto-update disabled.")

    else:
        print("Auto-update not supported on this platform.", file=sys.stderr)


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

    sub.add_parser("update", help="Pull latest version and reinstall")
    sub.add_parser("check-update", help="Check if an update is available (no install)")

    sub.add_parser("enable-auto-update", help="Schedule daily auto-update (09:00)")
    sub.add_parser("disable-auto-update", help="Remove scheduled auto-update")
    sub.add_parser("uninstall", help="Remove codex-async-mcp from this machine")

    args = parser.parse_args()

    if args.command == "add-agent":
        cmd_add(args.agent, args.python)
    elif args.command == "remove-agent":
        cmd_remove(args.agent)
    elif args.command == "list-agents":
        _print_table()
    elif args.command == "update":
        cmd_update()
    elif args.command == "check-update":
        cmd_update(check_only=True)
    elif args.command == "enable-auto-update":
        cmd_enable_auto_update()
    elif args.command == "disable-auto-update":
        cmd_disable_auto_update()
    elif args.command == "uninstall":
        cmd_uninstall()


if __name__ == "__main__":
    main()
