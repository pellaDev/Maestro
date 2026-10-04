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
# Supported distros (auto-detected):
#   Fedora / RHEL / CentOS / Rocky / Alma  →  dnf
#   Ubuntu / Debian / Mint / Pop!_OS       →  apt
#   Arch / Manjaro / EndeavourOS           →  pacman
#   openSUSE Leap / Tumbleweed             →  zypper
#
# Requirement tiers:
#   HARD  — Python 3.11+, bash, python3-gobject+GTK3 (abort if missing)
#   SOFT  — AppIndicator3 (warn; falls back to GtkStatusIcon → headless)
#   OPT   — notify-send (warn; no desktop notifications without it)
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
DEFAULT_INSTALL_DIR="${SCRIPT_DIR}"   # portable-first: installs inside the cloned folder
INSTALL_DIR=""
NO_AUTOSTART=false
ERRORS=0

# ── Args ───────────────────────────────────────────────────────────────────────
show_help() {
  echo -e "${BOLD}Maestro installer${RST}"
  echo ""
  echo "Usage: bash install.sh [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --install-dir PATH   Install to PATH instead of the cloned folder"
  echo "                       Default: ${DEFAULT_INSTALL_DIR}"
  echo "  --no-autostart       Skip the autostart prompt"
  echo "  --help               Show this help"
  echo ""
  echo "Supported distros: Fedora/RHEL, Ubuntu/Debian/Mint, Arch/Manjaro, openSUSE"
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
APPS_DIR="${HOME}/.local/share/applications"   # XDG standard — not project-specific, resolved via $HOME
APP_ENTRY_FILE="${APPS_DIR}/maestro.desktop"

# ── Distro detection ──────────────────────────────────────────────────────────
_DISTRO_ID=""
_DISTRO_LIKE=""
if [[ -f /etc/os-release ]]; then
  _DISTRO_ID="$(grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"' | tr '[:upper:]' '[:lower:]')"
  _DISTRO_LIKE="$(grep '^ID_LIKE=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)"
fi

_is_fedora_like() {
  [[ "$_DISTRO_ID"   =~ ^(fedora|rhel|centos|rocky|almalinux|nobara)$ ]] || \
  [[ "$_DISTRO_LIKE" =~ (fedora|rhel) ]]
}
_is_debian_like() {
  [[ "$_DISTRO_ID"   =~ ^(ubuntu|debian|linuxmint|pop|elementary|kali|zorin|mx)$ ]] || \
  [[ "$_DISTRO_LIKE" =~ (debian|ubuntu) ]]
}
_is_arch_like() {
  [[ "$_DISTRO_ID"   =~ ^(arch|manjaro|endeavouros|garuda|artix|cachyos)$ ]] || \
  [[ "$_DISTRO_LIKE" =~ arch ]]
}
_is_suse_like() {
  [[ "$_DISTRO_ID"   =~ ^(opensuse|opensuse-leap|opensuse-tumbleweed|sled|sles)$ ]] || \
  [[ "$_DISTRO_LIKE" =~ suse ]]
}

# ── Per-distro package install hints ─────────────────────────────────────────
_pkg_python_gi() {
  if   _is_fedora_like; then echo "sudo dnf install python3-gobject"
  elif _is_debian_like; then echo "sudo apt install python3-gi gir1.2-gtk-3.0"
  elif _is_arch_like;   then echo "sudo pacman -S python-gobject"
  elif _is_suse_like;   then echo "sudo zypper install python3-gobject typelib-1_0-Gtk-3_0"
  else echo "install python3-gobject via your package manager"
  fi
}

_pkg_appindicator() {
  if   _is_fedora_like; then echo "sudo dnf install libappindicator-gtk3"
  elif _is_debian_like; then
    # Ubuntu 22.04+ ships Ayatana as the AppIndicator replacement
    echo "sudo apt install gir1.2-appindicator3-0.1"
    echo "         # Ubuntu 22.04+: sudo apt install gir1.2-ayatanaappindicator3-0.1"
  elif _is_arch_like;   then
    echo "yay -S libappindicator-gtk3  # AUR — needs yay/paru"
    echo "         # fallback (GtkStatusIcon): no extra pkg needed if python-gobject installed" 
  elif _is_suse_like;   then echo "sudo zypper install typelib-1_0-AppIndicator3-0_1"
  else echo "install libappindicator-gtk3 via your package manager"
  fi
}

_pkg_notify() {
  if   _is_fedora_like; then echo "sudo dnf install libnotify"
  elif _is_debian_like; then echo "sudo apt install libnotify-bin"
  elif _is_arch_like;   then echo "sudo pacman -S libnotify"
  elif _is_suse_like;   then echo "sudo zypper install libnotify-tools"
  else echo "install libnotify via your package manager"
  fi
}

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
echo -e "  ${DIM}https://github.com/pellaDev/Maestro${RST}"
if [[ -n "$_DISTRO_ID" ]]; then
  echo -e "  ${DIM}Detected distro: ${_DISTRO_ID}${RST}"
fi
echo ""

# ── Requirements check ────────────────────────────────────────────────────────
hdr "Checking requirements…"

# ── 1. Python 3.11+ (HARD) ────────────────────────────────────────────────────
PY_OK=false
if /usr/bin/python3 --version &>/dev/null; then
  PY_VER=$(/usr/bin/python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  PY_MAJ=$(/usr/bin/python3 -c "import sys; print(sys.version_info.major)")
  PY_MIN=$(/usr/bin/python3 -c "import sys; print(sys.version_info.minor)")
  if [[ "$PY_MAJ" -ge 3 && "$PY_MIN" -ge 11 ]]; then
    ok "Python ${PY_VER}"
    PY_OK=true
  else
    fail "Python ${PY_VER} found — need 3.11+"
    if   _is_fedora_like; then warn "  sudo dnf install python3"
    elif _is_debian_like; then warn "  sudo apt install python3.11"
    elif _is_arch_like;   then warn "  sudo pacman -S python"
    elif _is_suse_like;   then warn "  sudo zypper install python311"
    fi
    ((ERRORS++))
  fi
else
  fail "python3 not found"
  if   _is_fedora_like; then warn "  sudo dnf install python3"
  elif _is_debian_like; then warn "  sudo apt install python3"
  elif _is_arch_like;   then warn "  sudo pacman -S python"
  elif _is_suse_like;   then warn "  sudo zypper install python3"
  fi
  ((ERRORS++))
fi

# ── 2. python3-gobject + GTK3 (HARD) ─────────────────────────────────────────
GTK_OK=false
if $PY_OK && /usr/bin/python3 -c "
import gi; gi.require_version('Gtk','3.0')
from gi.repository import Gtk
" &>/dev/null; then
  ok "python3-gobject + GTK3"
  GTK_OK=true
else
  fail "python3-gobject / GTK3 bindings missing — required for the tray daemon"
  warn "  Install: $(_pkg_python_gi)"
  ((ERRORS++))
fi

# ── 3. AppIndicator3 (SOFT — falls back to GtkStatusIcon, then headless) ─────
AI3_OK=false
if $GTK_OK && /usr/bin/python3 -c "
import gi
gi.require_version('Gtk','3.0')
gi.require_version('AppIndicator3','0.1')
from gi.repository import Gtk, AppIndicator3
" &>/dev/null; then
  ok "AppIndicator3 — full tray icon support"
  AI3_OK=true
else
  warn "AppIndicator3 not found — tray will fall back to GtkStatusIcon"
  warn "  Install: $(_pkg_appindicator)"
  # GNOME-specific hint: AppIndicator3 requires the shell extension
  if [[ "${XDG_CURRENT_DESKTOP:-}" =~ (GNOME|gnome) ]] || \
     [[ "${DESKTOP_SESSION:-}"     =~ (gnome|ubuntu) ]]; then
    warn "  GNOME detected: also enable the AppIndicator extension:"
    warn "    https://extensions.gnome.org/extension/615/appindicator-support/"
    warn "    or: gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com"
  fi
  # Not fatal — GtkStatusIcon or headless fallback will be used
fi

# ── 4. notify-send / libnotify (OPT) ─────────────────────────────────────────
if command -v notify-send &>/dev/null; then
  ok "notify-send (libnotify) — desktop notifications enabled"
else
  warn "notify-send not found — desktop notifications will be disabled"
  warn "  Install: $(_pkg_notify)"
fi

# ── 5. bash (HARD — needed to run .sh scripts) ───────────────────────────────
if command -v bash &>/dev/null; then
  ok "bash $(bash --version | head -1 | grep -oP '\d+\.\d+' | head -1)"
else
  fail "bash not found — required to run managed scripts"
  ((ERRORS++))
fi

# ── Abort if hard requirements missing ────────────────────────────────────────
if [[ $ERRORS -gt 0 ]]; then
  echo ""
  fail "Found ${ERRORS} missing hard requirement(s). Fix them and re-run install.sh."
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
  "start.py"
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
    warn "img/${FNAME} not found — skipped (tray icon may be missing)"
  fi
done

if [[ $COPY_ERRORS -gt 0 ]]; then
  warn "${COPY_ERRORS} file(s) missing from source. The install may be incomplete."
fi

# ── Make scripts executable ────────────────────────────────────────────────────
chmod +x \
  "${BIN_DIR}/start.sh" \
  "${BIN_DIR}/start.py" \
  "${BIN_DIR}/tray.py"  \
  "${BIN_DIR}/main.py"  \
  "${BIN_DIR}/daemon.py" 2>/dev/null || true
ok "Executable bits set"

# ── Create default settings.json ──────────────────────────────────────────────
hdr "Writing default settings.json…"
SETTINGS_FILE="${DATA_DIR}/settings.json"
if [[ ! -f "$SETTINGS_FILE" ]]; then
  cat > "$SETTINGS_FILE" << 'EOF'
{
  "fileExplorer": false,
  "logsRecorder": false,
  "openBrowserAtLaunch": false,
  "autostart": false,
  "trayPersistent": true,
  "systemNotifications": true,
  "host": "127.0.0.1",
  "port": 8865,
  "dataDir": ""
}
EOF
  # Fix #13: settings.json can later hold a GitHub PAT (githubToken) — make
  # it non-world-readable from the very first install, not just on later
  # writes from main.py/tray.py, so a shared-machine account never has a
  # window where it's readable.
  chmod 600 "$SETTINGS_FILE"
  ok "Created ${SETTINGS_FILE} (mode 600)"
else
  info "settings.json already exists — not overwritten"
fi

# ── Tray backend summary ───────────────────────────────────────────────────────
hdr "Tray backend"
if $AI3_OK; then
  ok "AppIndicator3 — full system tray icon (recommended)"
elif $GTK_OK; then
  warn "GtkStatusIcon — tray icon (AppIndicator3 not found; some desktops may hide it)"
  info "Install AppIndicator3 later and restart Maestro for the best experience."
else
  warn "Headless mode — no tray icon; Maestro will print the URL to the console"
fi

# ── Autostart ─────────────────────────────────────────────────────────────────
hdr "Autostart"
if $NO_AUTOSTART; then
  info "Skipped (--no-autostart)"
else
  echo ""
  read -rp "  Enable autostart with OS login? [y/N] " AS
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
    # Sync autostart=true into settings.json
    /usr/bin/python3 -c "
import json, pathlib
p = pathlib.Path('${SETTINGS_FILE}')
s = json.loads(p.read_text())
s['autostart'] = True
p.write_text(json.dumps(s, indent=2) + '\n')
" 2>/dev/null || true
    ok "Autostart enabled → ${AUTOSTART_FILE}"
  else
    info "Autostart not enabled. Enable it later from the ⚙ Settings modal."
  fi
fi

# ── Application menu entry ─────────────────────────────────────────────────────
# Adds Maestro to the desktop's application launcher (GNOME Activities, KDE menu,
# etc.) so it can be started like any other installed app — no terminal window,
# tray + background process only (Terminal=false).
hdr "Application menu entry"
echo ""
read -rp "  Create an application menu entry (so Maestro shows up like an installed app)? [Y/n] " APPENTRY
APPENTRY="${APPENTRY:-Y}"
if [[ "$APPENTRY" =~ ^[Yy]$ ]]; then
  mkdir -p "${APPS_DIR}"
  cat > "${APP_ENTRY_FILE}" << EOF
[Desktop Entry]
Type=Application
Name=Maestro
Comment=Privacy-first process orchestrator
Exec=bash ${BIN_DIR}/start.sh
Icon=${BIN_DIR}/img/MaestroLogo.svg
Terminal=false
Categories=Utility;Development;
StartupNotify=false
EOF
  ok "Application menu entry created → ${APP_ENTRY_FILE}"
  info "  Find it in your app launcher by searching 'Maestro'."
else
  info "Skipped. Launch manually via bin/start.sh"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ✓ Maestro installed successfully!${RST}"
echo ""
echo -e "  ${BOLD}Launch:${RST}"
echo -e "    bash ${BIN_DIR}/start.sh"
echo -e "    ${DIM}# or: python3 ${BIN_DIR}/start.py${RST}"
echo -e "    ${DIM}# or from your application launcher, if the menu entry was created${RST}"
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
