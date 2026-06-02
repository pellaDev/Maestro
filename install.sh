#!/usr/bin/env bash
# Maestro — installer script (v2.0)
# Path: install.sh
#
# Usage:
#   bash install.sh                        # installs to default path
#   bash install.sh --install-dir /my/path # custom install path
#   bash install.sh --no-autostart         # skip autostart prompt
#   bash install.sh --help
#
# What it does:
#   1. Checks system requirements
#   2. Creates directory structure (bin/ + data/)
#   3. Copies source files to install path
#   4. Makes scripts executable
#   5. Creates default settings.json
#   6. Optionally writes ~/.config/autostart/maestro.desktop
#   7. Prints launch instructions
#
# Tested on: Fedora 38+, Ubuntu 22.04+, Arch (best-effort for non-Fedora)
set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
BLU='\033[0;34m'; DIM='\033[2m'; RST='\033[0m'; BOLD='\033[1m'

ok()   { echo -e "  ${GRN}[✓]${RST} $*"; }
fail() { echo -e "  ${RED}[✗]${RST} $*"; }
warn() { echo -e "  ${YLW}[!]${RST} $*"; }
info() { echo -e "  ${BLU}[i]${RST} $*"; }
hdr()  { echo -e "\n${BOLD}$*${RST}"; }

# ── Defaults ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="${HOME}/.local/share/maestro"
INSTALL_DIR=""
NO_AUTOSTART=false
ERRORS=0

# ── Args ───────────────────────────────────────────────────────────────────────
show_help(){
  echo -e "${BOLD}Maestro installer${RST}"
  echo ""
  echo "Usage: bash install.sh [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --install-dir PATH   Install to PATH instead of default"
  echo "                       Default: ${DEFAULT_INSTALL_DIR}"
  echo "  --no-autostart       Skip the autostart prompt"
  echo "  --help               Show this help"
  echo ""
  echo "The installer creates two directories:"
  echo "  <install-dir>/bin/   — source files (never run directly)"
  echo "  <install-dir>/data/  — user data: config.txt, logs/, settings.json"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)  INSTALL_DIR="$2"; shift 2 ;;
    --no-autostart) NO_AUTOSTART=true; shift ;;
    --help|-h)      show_help ;;
    *)              warn "Unknown option: $1"; shift ;;
  esac
done

INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
BIN_DIR="${INSTALL_DIR}/bin"
DATA_DIR="${INSTALL_DIR}/data"
AUTOSTART_FILE="${HOME}/.config/autostart/maestro.desktop"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ███╗   ███╗ █████╗ ███████╗███████╗████████╗██████╗  ██████╗${RST}"
echo -e "${DIM}  ████╗ ████║██╔══██╗██╔════╝██╔════╝╚══██╔══╝██╔══██╗██╔═══██╗${RST}"
echo -e "${BOLD}  ██╔████╔██║███████║█████╗  ███████╗   ██║   ██████╔╝██║   ██║${RST}"
echo -e "${DIM}  ██║╚██╔╝██║██╔══██║██╔══╝  ╚════██║   ██║   ██╔══██╗██║   ██║${RST}"
echo -e "${BOLD}  ██║ ╚═╝ ██║██║  ██║███████╗███████║   ██║   ██║  ██║╚██████╔╝${RST}"
echo -e "${DIM}  ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ${RST}v2.0"
echo ""
echo -e "  Privacy-first process orchestrator for Linux"
echo -e "  ${DIM}https://github.com/AIdevelopmentPit/Maestro${RST}"
echo ""

# ── Requirements check ────────────────────────────────────────────────────────
hdr "Checking requirements…"

# Python 3.11+
PY_OK=false
if command -v python3 &>/dev/null; then
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
  PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
  if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
    ok "Python ${PY_VER}"
    PY_OK=true
  else
    fail "Python ${PY_VER} — need 3.11+. Install: sudo dnf install python3"
    ((ERRORS++))
  fi
else
  fail "python3 not found. Install: sudo dnf install python3"
  ((ERRORS++))
fi

# python3-gobject (gi.repository.Gtk)
GTK_OK=false
if $PY_OK && python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" &>/dev/null; then
  ok "python3-gobject (GTK)"
  GTK_OK=true
else
  fail "python3-gobject missing. Install: sudo dnf install python3-gobject"
  warn "  Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0"
  ((ERRORS++))
fi

# notify-send (libnotify)
if command -v notify-send &>/dev/null; then
  ok "notify-send (libnotify)"
else
  warn "notify-send not found — desktop notifications will be disabled"
  warn "  Install: sudo dnf install libnotify"
fi

# bash
if command -v bash &>/dev/null; then
  ok "bash $(bash --version | head -1 | grep -oP '\d+\.\d+')"
else
  fail "bash not found"
  ((ERRORS++))
fi

# ── Abort if hard requirements missing ────────────────────────────────────────
if [[ $ERRORS -gt 0 ]]; then
  echo ""
  fail "Found ${ERRORS} missing requirement(s). Fix them and re-run install.sh."
  exit 1
fi

# ── Confirm install path ───────────────────────────────────────────────────────
hdr "Install path"
info "bin/  → ${BIN_DIR}"
info "data/ → ${DATA_DIR}"
echo ""
read -rp "  Proceed? [Y/n] " CONFIRM
CONFIRM="${CONFIRM:-Y}"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "  Aborted."
  exit 0
fi

# ── Create directory structure ────────────────────────────────────────────────
hdr "Creating directories…"
mkdir -p "${BIN_DIR}/img" "${DATA_DIR}/logs"
ok "Created ${BIN_DIR}"
ok "Created ${DATA_DIR}"

# ── Source files to copy ───────────────────────────────────────────────────────
SRC_FILES=(
  "tray.py"
  "main.py"
  "daemon.py"
  "ui.html"
  "ui.js"
  "webui.js"
  "start.sh"
)

SRC_IMG_FILES=(
  "img/MaestroLogo.svg"
  "img/MaestroLogo24x24.png"
)

hdr "Copying files…"
COPY_ERRORS=0
for f in "${SRC_FILES[@]}"; do
  SRC="${SCRIPT_DIR}/${f}"
  if [[ -f "$SRC" ]]; then
    cp "$SRC" "${BIN_DIR}/${f}"
    ok "${f}"
  else
    warn "${f} not found in ${SCRIPT_DIR} — skipped"
    ((COPY_ERRORS++))
  fi
done

for f in "${SRC_IMG_FILES[@]}"; do
  SRC="${SCRIPT_DIR}/${f}"
  FNAME=$(basename "$f")
  if [[ -f "$SRC" ]]; then
    cp "$SRC" "${BIN_DIR}/img/${FNAME}"
    ok "img/${FNAME}"
  else
    warn "img/${FNAME} not found — skipped (icon may be missing)"
  fi
done

if [[ $COPY_ERRORS -gt 0 ]]; then
  warn "${COPY_ERRORS} file(s) missing from source. The install may be incomplete."
fi

# ── Make scripts executable ────────────────────────────────────────────────────
chmod +x "${BIN_DIR}/start.sh" "${BIN_DIR}/tray.py" "${BIN_DIR}/main.py" "${BIN_DIR}/daemon.py" 2>/dev/null || true
ok "Executable bits set"

# ── Create default settings.json ──────────────────────────────────────────────
hdr "Writing default settings.json…"
SETTINGS_FILE="${DATA_DIR}/settings.json"
if [[ ! -f "$SETTINGS_FILE" ]]; then
  cat > "$SETTINGS_FILE" << EOF
{
  "fileExplorer": false,
  "logsRecorder": false,
  "openBrowserAtLaunch": false,
  "autostart": false,
  "trayPersistent": true,
  "host": "127.0.0.1",
  "port": 8865,
  "dataDir": ""
}
EOF
  ok "Created ${SETTINGS_FILE}"
else
  info "settings.json already exists — not overwritten"
fi

# ── Autostart ─────────────────────────────────────────────────────────────────
hdr "Autostart"
if $NO_AUTOSTART; then
  info "Skipped (--no-autostart)"
else
  echo ""
  read -rp "  Enable autostart with OS? [y/N] " AS
  AS="${AS:-N}"
  if [[ "$AS" =~ ^[Yy]$ ]]; then
    mkdir -p "${HOME}/.config/autostart"
    cat > "$AUTOSTART_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Maestro
Exec=bash ${BIN_DIR}/start.sh
Hidden=false
X-GNOME-Autostart-enabled=true
EOF
    # Update settings.json to reflect autostart=true
    python3 -c "
import json, pathlib
p = pathlib.Path('${SETTINGS_FILE}')
s = json.loads(p.read_text())
s['autostart'] = True
p.write_text(json.dumps(s, indent=2) + '\n')
" 2>/dev/null || true
    ok "Autostart enabled → ${AUTOSTART_FILE}"
  else
    info "Autostart not enabled. You can enable it later from the Settings modal."
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ✓ Maestro installed successfully!${RST}"
echo ""
echo -e "  ${BOLD}Launch:${RST}"
echo -e "    bash ${BIN_DIR}/start.sh"
echo ""
echo -e "  ${BOLD}UI:${RST}"
echo -e "    http://127.0.0.1:8865  (after launch)"
echo ""
echo -e "  ${BOLD}Data directory:${RST}"
echo -e "    ${DATA_DIR}"
echo -e "    ${DIM}config.txt, logs/, settings.json${RST}"
echo ""
echo -e "  ${DIM}To uninstall: rm -rf ${INSTALL_DIR}${RST}"
echo ""
