#!/usr/bin/env bash
# codex-async-mcp installer
# Usage: curl -fsSL https://raw.githubusercontent.com/benzkittisak/claude-codex-mcp/master/install.sh | bash

set -euo pipefail

REPO_URL="https://github.com/benzkittisak/claude-codex-mcp"
INSTALL_DIR="${HOME}/.local/share/codex-async-mcp"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${BLUE}[codex-async]${NC} $*"; }
ok()    { echo -e "${GREEN}[codex-async]${NC} $*"; }
warn()  { echo -e "${YELLOW}[codex-async]${NC} $*"; }
die()   { echo -e "${RED}[codex-async] ERROR:${NC} $*" >&2; exit 1; }

# ── helpers ───────────────────────────────────────────────────────────────────

# Read from /dev/tty so curl | bash works (stdin is the script, not the user).
ask() {
    local prompt="$1" ans
    printf "%s [y/n]: " "$prompt"
    read -r ans </dev/tty
    [[ "$ans" == "y" || "$ans" == "Y" ]]
}

# ── prerequisites ─────────────────────────────────────────────────────────────

find_python() {
    for py in python3 python; do
        if command -v "$py" &>/dev/null; then
            local ver
            ver=$("$py" -c "import sys; print(sys.version_info >= (3, 11))" 2>/dev/null || echo False)
            [[ "$ver" == "True" ]] && echo "$py" && return
        fi
    done
    die "Python 3.11+ required. Install from https://python.org or: brew install python@3.13"
}

# ── detect agents ─────────────────────────────────────────────────────────────

detect_agents() {
    DETECTED_AGENTS=()

    command -v claude &>/dev/null \
        && DETECTED_AGENTS+=("claude-code|Claude Code CLI")

    { command -v cursor &>/dev/null || [[ -d "${HOME}/.cursor" ]]; } \
        && DETECTED_AGENTS+=("cursor|Cursor IDE")

    { [[ -d "${HOME}/Library/Application Support/Claude" ]] \
        || [[ -d "${HOME}/.config/Claude" ]]; } \
        && DETECTED_AGENTS+=("claude-desktop|Claude Desktop")
}

# ── interactive agent selection ───────────────────────────────────────────────

select_agents() {
    SELECTED_AGENTS=()

    if [[ ${#DETECTED_AGENTS[@]} -eq 0 ]]; then
        warn "No supported agents detected on this machine."
        warn "Register manually later: codex-async add-agent <agent>"
        warn "Agents: claude-code | cursor | claude-desktop"
        return
    fi

    echo ""
    echo -e "${BOLD}  Detected agents — choose which to register with:${NC}"
    echo ""

    for entry in "${DETECTED_AGENTS[@]}"; do
        local name="${entry%%|*}"
        local label="${entry##*|}"
        printf "    %-22s" "$label"
        if ask ""; then
            SELECTED_AGENTS+=("$name")
        fi
    done
}

# ── register agents ───────────────────────────────────────────────────────────

register_agents() {
    local python="$1"

    if [[ ${#SELECTED_AGENTS[@]} -eq 0 ]]; then
        warn "No agents selected. Skip registration."
        return
    fi

    echo ""
    info "Registering selected agents..."

    for agent in "${SELECTED_AGENTS[@]}"; do
        info "  → $agent"
        "${python}" -m codex_async_mcp.cli add-agent "$agent" --python "$python" \
            || warn "    Failed to register $agent — run: codex-async add-agent $agent"
    done
}

# ── main ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}codex-async-mcp installer${NC}"
echo "────────────────────────────────────────"
echo ""

info "Checking Python..."
PYTHON=$(find_python)
ok "Found: $($PYTHON --version)"

info "Cloning / updating → ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git -C "${INSTALL_DIR}" pull --ff-only
else
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

VENV_DIR="${INSTALL_DIR}/.venv"

info "Creating virtual environment..."
"${PYTHON}" -m venv "${VENV_DIR}"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

info "Installing package..."
"${PIP}" install --quiet --upgrade pip
"${PIP}" install --quiet -e "${INSTALL_DIR}"
ok "Package installed."

# Verify import
"${PYTHON}" -c "import codex_async_mcp" \
    || die "Import failed — check Python environment."

# ── symlink CLI to ~/.local/bin ───────────────────────────────────────────────

LOCAL_BIN="${HOME}/.local/bin"
mkdir -p "${LOCAL_BIN}"
ln -sf "${VENV_DIR}/bin/codex-async" "${LOCAL_BIN}/codex-async"
ok "CLI linked → ${LOCAL_BIN}/codex-async"

# Ensure ~/.local/bin is in PATH (add to shell profile if missing)
add_to_path() {
    local profile="$1"
    local line='export PATH="${HOME}/.local/bin:${PATH}"'
    if [[ -f "$profile" ]] && grep -q '\.local/bin' "$profile"; then
        return
    fi
    if [[ -f "$profile" ]]; then
        echo "" >> "$profile"
        echo "# codex-async-mcp" >> "$profile"
        echo "$line" >> "$profile"
        warn "Added ~/.local/bin to PATH in ${profile}. Run: source ${profile}"
    fi
}

if [[ ":${PATH}:" != *":${LOCAL_BIN}:"* ]]; then
    add_to_path "${HOME}/.zshrc"
    add_to_path "${HOME}/.bashrc"
    export PATH="${LOCAL_BIN}:${PATH}"
fi

# Detect + let user pick agents
detect_agents
select_agents
register_agents "${PYTHON}"

# ── done ──────────────────────────────────────────────────────────────────────

echo ""
echo "────────────────────────────────────────"
ok "Done! Restart your agent to load the server."
echo ""
echo -e "  ${BOLD}CLI commands:${NC}"
echo "    codex-async list-agents              # show detected / registered agents"
echo "    codex-async add-agent claude-code    # register with Claude Code"
echo "    codex-async add-agent cursor         # register with Cursor"
echo "    codex-async add-agent claude-desktop # register with Claude Desktop"
echo "    codex-async remove-agent <agent>     # unregister"
echo ""
echo -e "  ${BOLD}Permissions to add in .claude/settings.local.json:${NC}"
echo '    "mcp__codex-async__codex_start",   "mcp__codex-async__codex_wait",'
echo '    "mcp__codex-async__cursor_start",  "mcp__codex-async__cursor_wait",'
echo '    "mcp__codex-async__gemini_start",  "mcp__codex-async__gemini_wait",'
echo '    "mcp__codex-async__queue_status",  "mcp__codex-async__job_list",'
echo '    "mcp__codex-async__job_cancel",    "mcp__codex-async__agent_notify_done"'
echo ""
