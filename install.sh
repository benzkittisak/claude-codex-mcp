#!/usr/bin/env bash
# codex-async-mcp installer / uninstaller
# Install:   curl -fsSL https://raw.githubusercontent.com/benzkittisak/claude-codex-mcp/master/install.sh | bash
# Uninstall: curl -fsSL https://raw.githubusercontent.com/benzkittisak/claude-codex-mcp/master/install.sh | bash -s uninstall
#            or: bash install.sh uninstall

set -euo pipefail

REPO_URL="https://github.com/benzkittisak/claude-codex-mcp"
INSTALL_DIR="${HOME}/.local/share/codex-async-mcp"
LOCAL_BIN="${HOME}/.local/bin"
VENV_DIR="${INSTALL_DIR}/.venv"
MCP_NAME="codex-async"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${BLUE}[codex-async]${NC} $*"; }
ok()    { echo -e "${GREEN}[codex-async]${NC} $*"; }
warn()  { echo -e "${YELLOW}[codex-async]${NC} $*"; }
die()   { echo -e "${RED}[codex-async] ERROR:${NC} $*" >&2; exit 1; }

# ── uninstall ─────────────────────────────────────────────────────────────────

uninstall() {
    echo ""
    echo -e "${BOLD}codex-async-mcp uninstaller${NC}"
    echo "────────────────────────────────────────"
    echo ""

    # Remove from all agents via CLI (best-effort)
    CLI="${LOCAL_BIN}/${MCP_NAME}"
    if [[ -x "$CLI" ]]; then
        info "Removing agent registrations..."
        for agent in claude-code codex cursor claude-desktop; do
            "$CLI" remove-agent "$agent" 2>/dev/null && ok "  removed: $agent" || true
        done
    else
        warn "CLI not found — skipping agent deregistration."
        warn "Run manually: claude mcp remove ${MCP_NAME} -s user"
    fi

    # Remove CLI symlink
    if [[ -L "${LOCAL_BIN}/${MCP_NAME}" ]]; then
        rm "${LOCAL_BIN}/${MCP_NAME}"
        ok "Removed symlink: ${LOCAL_BIN}/${MCP_NAME}"
    fi

    # Remove install directory (repo + venv)
    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        ok "Removed: ${INSTALL_DIR}"
    fi

    # Remove data directory
    DATA_DIR="${HOME}/.codex-async"
    if [[ -d "${DATA_DIR}" ]]; then
        printf "Remove job data at %s? [y/n]: " "${DATA_DIR}"
        read -r ans </dev/tty
        if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
            rm -rf "${DATA_DIR}"
            ok "Removed: ${DATA_DIR}"
        else
            warn "Kept: ${DATA_DIR}"
        fi
    fi

    # Remove LaunchAgent (macOS)
    LAUNCH_AGENT="${HOME}/Library/LaunchAgents/com.codex-async.update.plist"
    if [[ -f "${LAUNCH_AGENT}" ]]; then
        launchctl unload "${LAUNCH_AGENT}" 2>/dev/null || true
        rm "${LAUNCH_AGENT}"
        ok "Removed LaunchAgent auto-update."
    fi

    echo ""
    echo "────────────────────────────────────────"
    ok "Uninstall complete."
    echo ""
}

# ── helpers ───────────────────────────────────────────────────────────────────

# Read from /dev/tty so curl | bash works (stdin is the script, not the user).
ask() {
    local prompt="$1" ans
    printf "%s [y/n]: " "$prompt"
    read -r ans </dev/tty
    [[ "$ans" == "y" || "$ans" == "Y" ]]
}

# ── OS detection ──────────────────────────────────────────────────────────────

OS="unknown"        # macos | linux
DISTRO=""           # ubuntu | debian | fedora | arch | ...
PKG_MANAGER=""      # brew | apt | dnf | pacman | zypper

detect_os() {
    case "$(uname -s)" in
        Darwin)
            OS="macos"
            PKG_MANAGER="brew"
            ;;
        Linux)
            OS="linux"
            if [[ -f /etc/os-release ]]; then
                # shellcheck disable=SC1091
                . /etc/os-release
                DISTRO="${ID:-unknown}"
            fi
            case "${DISTRO}" in
                ubuntu|debian|pop|linuxmint|kali) PKG_MANAGER="apt" ;;
                fedora|rhel|centos|rocky|alma)    PKG_MANAGER="dnf" ;;
                arch|manjaro|endeavouros)         PKG_MANAGER="pacman" ;;
                opensuse*|sles)                   PKG_MANAGER="zypper" ;;
            esac
            ;;
    esac
}

# Install a system package. Args: <macos> <apt> <dnf> <pacman> <zypper>
pkg_install() {
    local p_brew="$1" p_apt="$2" p_dnf="$3" p_pacman="$4" p_zypper="${5:-$3}"
    case "${PKG_MANAGER}" in
        brew)
            command -v brew &>/dev/null \
                || die "Homebrew required. Install: https://brew.sh"
            brew install "${p_brew}"
            ;;
        apt)
            local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
            ${SUDO} apt-get update -qq
            ${SUDO} apt-get install -y "${p_apt}"
            ;;
        dnf)
            local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
            ${SUDO} dnf install -y "${p_dnf}"
            ;;
        pacman)
            local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
            ${SUDO} pacman -S --noconfirm "${p_pacman}"
            ;;
        zypper)
            local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
            ${SUDO} zypper install -y "${p_zypper}"
            ;;
        *)
            die "Cannot auto-install on this OS/distro (${OS}/${DISTRO}). Install manually."
            ;;
    esac
}

# ── prerequisites ─────────────────────────────────────────────────────────────

find_python() {
    # Prefer versioned binaries first
    for py in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$py" &>/dev/null; then
            local ver
            ver=$("$py" -c "import sys; print(sys.version_info >= (3, 11))" 2>/dev/null || echo False)
            [[ "$ver" == "True" ]] && echo "$py" && return
        fi
    done
    return 1
}

check_deps() {
    detect_os
    info "Detected OS: ${OS}${DISTRO:+ / ${DISTRO}}"

    # git
    if ! command -v git &>/dev/null; then
        warn "git not found — installing..."
        pkg_install git git git git git
        command -v git &>/dev/null || die "git install failed."
        ok "git installed."
    fi

    # Python 3.11+
    if ! find_python &>/dev/null; then
        warn "Python 3.11+ not found — installing..."
        case "${PKG_MANAGER}" in
            brew)   brew install python@3.13 ;;
            apt)
                local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
                ${SUDO} apt-get update -qq
                ${SUDO} apt-get install -y python3 python3-venv python3-pip
                ;;
            dnf)
                local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
                ${SUDO} dnf install -y python3 python3-pip
                ;;
            pacman)
                local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
                ${SUDO} pacman -S --noconfirm python
                ;;
            zypper)
                local SUDO; SUDO=$(command -v sudo 2>/dev/null || true)
                ${SUDO} zypper install -y python3 python3-pip
                ;;
            *)
                die "Python 3.11+ required. Install from https://python.org"
                ;;
        esac
        find_python &>/dev/null || die "Python 3.11+ still not found after install."
        ok "Python installed."
    fi

    # Warn about optional agent CLIs (can't auto-install — require accounts)
    local missing_agents=()
    command -v claude  &>/dev/null || missing_agents+=("claude  → https://claude.ai/code")
    command -v codex   &>/dev/null || missing_agents+=("codex   → npm install -g @openai/codex")
    command -v cursor  &>/dev/null || missing_agents+=("cursor  → https://cursor.com")
    command -v gemini  &>/dev/null || missing_agents+=("gemini  → https://github.com/google-gemini/gemini-cli")

    if [[ ${#missing_agents[@]} -gt 0 ]]; then
        echo ""
        warn "Optional agent CLIs not found (install separately if needed):"
        for a in "${missing_agents[@]}"; do
            echo "    $a"
        done
    fi
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

[[ "${1:-}" == "uninstall" ]] && uninstall && exit 0

echo ""
echo -e "${BOLD}codex-async-mcp installer${NC}"
echo "────────────────────────────────────────"
echo ""

check_deps
PYTHON=$(find_python)
ok "Using: $($PYTHON --version)"

info "Cloning / updating → ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git -C "${INSTALL_DIR}" pull --ff-only
else
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

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

mkdir -p "${LOCAL_BIN}"
ln -sf "${VENV_DIR}/bin/codex-async" "${LOCAL_BIN}/codex-async"
ok "CLI linked → ${LOCAL_BIN}/codex-async"

# Ensure ~/.local/bin is in PATH across all common shell profiles
_path_line='export PATH="${HOME}/.local/bin:${PATH}"'

add_to_path() {
    local profile="$1"
    # Skip if already has ~/.local/bin
    [[ -f "$profile" ]] && grep -q '\.local/bin' "$profile" && return
    # Append (create file if needed)
    {
        echo ""
        echo "# codex-async"
        echo "${_path_line}"
    } >> "$profile"
    ok "  added PATH to ${profile}"
}

if [[ ":${PATH}:" != *":${LOCAL_BIN}:"* ]]; then
    info "Adding ~/.local/bin to shell profiles..."
    # zsh: interactive + login
    add_to_path "${HOME}/.zshrc"
    add_to_path "${HOME}/.zprofile"
    # bash: interactive + login
    add_to_path "${HOME}/.bashrc"
    add_to_path "${HOME}/.bash_profile"
    export PATH="${LOCAL_BIN}:${PATH}"
    warn "Open a new terminal (or run: source ~/.zshrc) for PATH to take effect."
fi

# Detect + let user pick agents
detect_agents
select_agents
register_agents "${PYTHON}"

# ── smoke test ────────────────────────────────────────────────────────────────

smoke_test() {
    local cli="${LOCAL_BIN}/codex-async"
    local pass=0 fail=0

    echo ""
    info "Running smoke tests..."

    # 1. CLI binary exists and is executable
    printf "    %-45s" "CLI binary exists"
    if [[ -x "$cli" ]]; then
        echo -e "${GREEN}PASS${NC}"; (( pass++ ))
    else
        echo -e "${RED}FAIL${NC}"; (( fail++ ))
        echo "    FIX: ln -sf ${VENV_DIR}/bin/codex-async ${cli}"
    fi

    # 2. CLI reachable in PATH
    printf "    %-45s" "codex-async in PATH"
    if command -v codex-async &>/dev/null; then
        echo -e "${GREEN}PASS${NC}"; (( pass++ ))
    else
        echo -e "${YELLOW}WARN${NC} (need new terminal)"; (( pass++ ))
        echo "    FIX: source ~/.zshrc  (or open a new terminal)"
    fi

    # 3. Python import
    printf "    %-45s" "Python import codex_async_mcp"
    if "${PYTHON}" -c "import codex_async_mcp" 2>/dev/null; then
        echo -e "${GREEN}PASS${NC}"; (( pass++ ))
    else
        echo -e "${RED}FAIL${NC}"; (( fail++ ))
        echo "    FIX: ${VENV_DIR}/bin/pip install -e ${INSTALL_DIR}"
    fi

    # 4. MCP server starts and exits cleanly (1-second timeout)
    printf "    %-45s" "MCP server starts"
    if timeout 2s "${PYTHON}" -c "
from codex_async_mcp.db import init_db
from codex_async_mcp.job_manager import recover_on_startup
init_db(); recover_on_startup()
print('ok')
" 2>/dev/null | grep -q ok; then
        echo -e "${GREEN}PASS${NC}"; (( pass++ ))
    else
        echo -e "${RED}FAIL${NC}"; (( fail++ ))
        echo "    FIX: check ${VENV_DIR}/bin/pip install -e ${INSTALL_DIR}"
        echo "         or re-run the installer"
    fi

    # 5. At least one agent registered
    printf "    %-45s" "At least one agent registered"
    local reg
    reg=$("$cli" list-agents 2>/dev/null | grep -c "yes" || echo 0)
    if [[ "$reg" -gt 0 ]]; then
        echo -e "${GREEN}PASS${NC} (${reg} agent(s))"; (( pass++ ))
    else
        echo -e "${YELLOW}WARN${NC} no agents registered"
        echo "    FIX: codex-async add-agent claude-code"
    fi

    echo ""
    if [[ $fail -eq 0 ]]; then
        ok "All tests passed (${pass}/${pass})."
    else
        warn "${fail} test(s) failed — see FIX hints above."
    fi
}

smoke_test

# ── done ──────────────────────────────────────────────────────────────────────

echo ""
echo "────────────────────────────────────────"
ok "Done! Restart your agent to load the server."
echo ""
echo -e "  ${BOLD}CLI commands:${NC}"
echo "    codex-async list-agents              # show detected / registered agents"
echo "    codex-async add-agent claude-code    # register with Claude Code CLI"
echo "    codex-async add-agent codex          # register with Codex CLI"
echo "    codex-async add-agent cursor         # register with Cursor IDE"
echo "    codex-async add-agent claude-desktop # register with Claude Desktop"
echo "    codex-async remove-agent <agent>     # unregister"
echo "    codex-async update                   # pull latest + reinstall"
echo "    codex-async enable-auto-update       # schedule daily auto-update"
echo "    codex-async uninstall                # remove everything"
echo ""
echo -e "  ${BOLD}Permissions to add in .claude/settings.local.json:${NC}"
echo '    "mcp__codex-async__codex_start",   "mcp__codex-async__codex_wait",'
echo '    "mcp__codex-async__cursor_start",  "mcp__codex-async__cursor_wait",'
echo '    "mcp__codex-async__gemini_start",  "mcp__codex-async__gemini_wait",'
echo '    "mcp__codex-async__queue_status",  "mcp__codex-async__job_list",'
echo '    "mcp__codex-async__job_cancel",    "mcp__codex-async__agent_notify_done"'
echo ""
