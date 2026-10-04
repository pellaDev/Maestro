#!/usr/bin/env python3
"""
Maestro — Windows installer
Path: install.py

Usage:
    python install.py
    python install.py --install-dir C:\\Users\\you\\AppData\\Local\\Maestro
    python install.py --no-autostart
    python install.py --help

What it does:
  1. Checks requirements (Python 3.11+, bash via WSL or Git Bash)
  2. Creates bin\\ and data\\ directories
  3. Copies source files
  4. Creates default settings.json
  5. Optionally enables autostart via HKCU registry Run key (winreg stdlib)
  6. Optionally creates a native Start Menu shortcut (pure ctypes COM, no pywin32) —
     launches via pythonw.exe so no console window ever flashes on startup
  7. Prints launch instructions

Zero pip dependencies — uses only Python stdlib.
"""
import argparse, ctypes, json, os, platform, shutil, subprocess, sys, tempfile
from ctypes import wintypes
from pathlib import Path

# ── Colours (Windows ANSI — enabled via os.system('') trick) ──────────────────
os.system('')   # enable ANSI escape codes on Windows 10+
RED  = '\033[0;31m'; GRN = '\033[0;32m'; YLW = '\033[0;33m'
BLU  = '\033[0;34m'; DIM = '\033[2m';    RST = '\033[0m'; BOLD = '\033[1m'

def ok(msg):   print(f"  {GRN}[✓]{RST} {msg}")
def fail(msg): print(f"  {RED}[✗]{RST} {msg}")
def warn(msg): print(f"  {YLW}[!]{RST} {msg}")
def info(msg): print(f"  {BLU}[i]{RST} {msg}")
def hdr(msg):  print(f"\n{BOLD}{msg}{RST}")


# ── Shortcut helper (pure ctypes COM, no pywin32) ──────────────────────────────
def _create_shortcut_win(shortcut_path: str, target: str, arguments: str = '',
                          working_dir: str = '', description: str = 'Maestro'):
    """
    Create a native Windows .lnk shortcut via raw ctypes COM calls
    (IShellLinkW + IPersistFile). No pywin32 dependency — consistent with
    tray.py's existing raw-ctypes Win32 backend (zero pip, stdlib only).

    Vtable slot indices below come from the documented IUnknown/IShellLinkW/
    IPersist/IPersistFile interface method orders (shobjidl_core.h, objidl.h).
    """
    from ctypes import (POINTER, Structure, byref, c_void_p, c_ulong,
                         c_ushort, c_ubyte, c_long, sizeof, cast, WINFUNCTYPE)

    HRESULT = c_long

    class GUID(Structure):
        _fields_ = [
            ('Data1', c_ulong), ('Data2', c_ushort), ('Data3', c_ushort),
            ('Data4', c_ubyte * 8),
        ]

    ole32 = ctypes.windll.ole32
    ole32.CLSIDFromString.argtypes  = [wintypes.LPCWSTR, POINTER(GUID)]
    ole32.CLSIDFromString.restype   = HRESULT
    ole32.CoInitialize.argtypes     = [c_void_p]
    ole32.CoInitialize.restype      = HRESULT
    ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD,
                                        POINTER(GUID), POINTER(c_void_p)]
    ole32.CoCreateInstance.restype  = HRESULT

    def _guid(s):
        g = GUID()
        ole32.CLSIDFromString(s, byref(g))
        return g

    def _com_call(iface_ptr, index, restype, argtypes, *args):
        """Call a COM interface method by vtable slot index."""
        vtbl = cast(iface_ptr, POINTER(c_void_p))[0]
        slot = cast(vtbl + index * sizeof(c_void_p), POINTER(c_void_p))[0]
        func = WINFUNCTYPE(restype, c_void_p, *argtypes)(slot)
        return func(iface_ptr, *args)

    CLSCTX_INPROC_SERVER = 0x1
    CLSID_ShellLink  = _guid('{00021401-0000-0000-C000-000000000046}')
    IID_IShellLinkW  = _guid('{000214F9-0000-0000-C000-000000000046}')
    IID_IPersistFile = _guid('{0000010B-0000-0000-C000-000000000046}')

    ole32.CoInitialize(None)

    p_sl = c_void_p()
    hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
                                 byref(IID_IShellLinkW), byref(p_sl))
    if hr != 0 or not p_sl:
        raise OSError(f'CoCreateInstance(IShellLinkW) failed: hr={hr:#x}')

    # IShellLinkW vtable slots (0/1/2 = IUnknown QueryInterface/AddRef/Release):
    #   7  = SetDescription
    #   9  = SetWorkingDirectory
    #   11 = SetArguments
    #   20 = SetPath
    _com_call(p_sl, 20, HRESULT, (wintypes.LPCWSTR,), target)
    _com_call(p_sl, 11, HRESULT, (wintypes.LPCWSTR,), arguments)
    _com_call(p_sl, 9,  HRESULT, (wintypes.LPCWSTR,), working_dir)
    _com_call(p_sl, 7,  HRESULT, (wintypes.LPCWSTR,), description)

    # QueryInterface (slot 0) for IPersistFile on the same object
    p_pf = c_void_p()
    hr = _com_call(p_sl, 0, HRESULT, (POINTER(GUID), POINTER(c_void_p)),
                    byref(IID_IPersistFile), byref(p_pf))
    if hr != 0 or not p_pf:
        raise OSError(f'QueryInterface(IPersistFile) failed: hr={hr:#x}')

    # IPersistFile vtable slot 6 = Save(LPCOLESTR pszFileName, BOOL fRemember)
    hr = _com_call(p_pf, 6, HRESULT, (wintypes.LPCWSTR, wintypes.BOOL),
                    shortcut_path, True)
    if hr != 0:
        raise OSError(f'IPersistFile::Save failed: hr={hr:#x}')

    _com_call(p_pf, 2, c_ulong, ())   # Release IPersistFile
    _com_call(p_sl, 2, c_ulong, ())   # Release IShellLinkW
    ole32.CoUninitialize()


# ── Args ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR          = Path(__file__).parent.resolve()
DEFAULT_INSTALL_DIR = Path(__file__).parent.resolve()  # portable-first: installs inside the cloned folder

parser = argparse.ArgumentParser(
    description="Maestro Windows installer",
    formatter_class=argparse.RawTextHelpFormatter,
)
parser.add_argument(
    '--install-dir', default=str(DEFAULT_INSTALL_DIR),
    help='Install directory (default: folder containing install.py)',
)
parser.add_argument('--no-autostart', action='store_true',
                    help='Skip the autostart prompt')
args = parser.parse_args()

INSTALL_DIR = Path(args.install_dir).resolve()
BIN_DIR     = INSTALL_DIR / 'bin'
DATA_DIR    = INSTALL_DIR / 'data'
ERRORS      = 0

# ── Banner ────────────────────────────────────────────────────────────────────
print()
print(f"{BOLD}  ███╗   ███╗ █████╗ ███████╗███████╗████████╗██████╗  ██████╗{RST}")
print(f"{DIM}  ████╗ ████║██╔══██╗██╔════╝██╔════╝╚══██╔══╝██╔══██╗██╔═══██╗{RST}")
print(f"{BOLD}  ██╔████╔██║███████║█████╗  ███████╗   ██║   ██████╔╝██║   ██║{RST}")
print(f"{DIM}  ██║╚██╔╝██║██╔══██║██╔══╝  ╚════██║   ██║   ██╔══██╗██║   ██║{RST}")
print(f"{BOLD}  ██║ ╚═╝ ██║██║  ██║███████╗███████║   ██║   ██║  ██║╚██████╔╝{RST}")
print(f"{DIM}  ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ {RST}v2.0")
print()
print("  Privacy-first process orchestrator — Windows installer")
print(f"  {DIM}https://github.com/pellaDev/Maestro{RST}")
print(f"  {DIM}Platform: Windows {platform.version()}{RST}")
print()

# ── Requirements check ────────────────────────────────────────────────────────
hdr("Checking requirements…")

# 1. Python 3.11+ (we're already running, just verify version)
major, minor = sys.version_info.major, sys.version_info.minor
py_ver = f"{major}.{minor}.{sys.version_info.micro}"
if major >= 3 and minor >= 11:
    ok(f"Python {py_ver}")
else:
    fail(f"Python {py_ver} — need 3.11+")
    warn("  Download: https://www.python.org/downloads/")
    ERRORS += 1

# 2. bash via WSL or Git Bash (needed to run .sh scripts)
BASH_PATH = None
BASH_CANDIDATES = [
    'bash',                                         # PATH (WSL or Git Bash configured)
    r'C:\Windows\System32\bash.exe',                # WSL
    r'C:\Program Files\Git\bin\bash.exe',           # Git Bash 64-bit
    r'C:\Program Files (x86)\Git\bin\bash.exe',     # Git Bash 32-bit
]
for candidate in BASH_CANDIDATES:
    try:
        r = subprocess.run(
            [candidate, '--version'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        if r.returncode == 0:
            BASH_PATH = candidate
            break
    except Exception:
        continue

if BASH_PATH:
    ok(f"bash found  →  {BASH_PATH}")
else:
    warn("bash not found — scripts (.sh files) will not run without it")
    warn("  Install WSL:      https://aka.ms/wslinstall")
    warn("  Install Git Bash: https://git-scm.com/download/win")
    warn("  Maestro will install but scripts cannot be launched until bash is available.")
    # Soft requirement — we warn but do not abort

# 3. Windows version (Win10+ recommended for balloon notifications + wt.exe)
win_ver = sys.getwindowsversion()
if win_ver.major >= 10:
    ok(f"Windows {win_ver.major}.{win_ver.minor} (build {win_ver.build})")
else:
    warn(f"Windows {win_ver.major}.{win_ver.minor} — some features require Windows 10+")

# Abort on hard failures
if ERRORS > 0:
    print()
    fail(f"Found {ERRORS} missing hard requirement(s). Fix and re-run install.py.")
    sys.exit(1)

# ── Confirm install path ───────────────────────────────────────────────────────
hdr("Install path")
info(f"Maestro will be self-contained in:")
info(f"  {INSTALL_DIR}")
info(f"  ├── bin\\   (source files)")
info(f"  └── data\\  (config, logs, settings)")
print()
confirm = input("  Proceed? [Y/n] ").strip() or 'Y'
if confirm.lower() != 'y':
    print("  Aborted.")
    sys.exit(0)

# ── Create directory structure ────────────────────────────────────────────────
hdr("Creating directories…")
(BIN_DIR / 'img').mkdir(parents=True, exist_ok=True)
(DATA_DIR / 'logs').mkdir(parents=True, exist_ok=True)
ok(f"Created {BIN_DIR}")
ok(f"Created {DATA_DIR}")

# ── Copy source files ──────────────────────────────────────────────────────────
SRC_FILES = [
    'tray.py', 'main.py', 'daemon.py',
    'ui.html', 'ui.js', 'webui.js',
    'start.sh', 'start.py', 'start.bat',
]
SRC_IMG = [
    'img/MaestroLogo.svg',
    'img/MaestroLogo24x24.png',
]

hdr("Copying files…")
copy_errors = 0
for fname in SRC_FILES:
    src = SCRIPT_DIR / fname
    if src.exists():
        shutil.copy2(src, BIN_DIR / fname)
        ok(fname)
    else:
        warn(f"{fname} not found in {SCRIPT_DIR} — skipped")
        copy_errors += 1

for fpath in SRC_IMG:
    src = SCRIPT_DIR / fpath
    if src.exists():
        shutil.copy2(src, BIN_DIR / 'img' / Path(fpath).name)
        ok(f"img/{Path(fpath).name}")
    else:
        warn(f"img/{Path(fpath).name} not found — skipped (tray icon may be missing)")

if copy_errors:
    warn(f"{copy_errors} file(s) missing. The install may be incomplete.")

# ── Create default settings.json ──────────────────────────────────────────────
hdr("Writing default settings.json…")
settings_file = DATA_DIR / 'settings.json'
if not settings_file.exists():
    default_settings = {
        "fileExplorer":        False,
        "logsRecorder":        False,
        "openBrowserAtLaunch": False,
        "autostart":           False,
        "trayPersistent":      True,
        "systemNotifications": True,
        "host":                "127.0.0.1",
        "port":                8865,
        "dataDir":             "",
    }
    settings_file.write_text(
        json.dumps(default_settings, indent=2) + '\n',
        encoding='utf-8',
    )
    # Fix #13: settings.json can later hold a GitHub PAT (githubToken) —
    # restrict permissions from the very first install, not just on later
    # writes from main.py/tray.py. No POSIX chmod bits on Windows itself,
    # but this installer can also target a WSL/POSIX-style path, so guard
    # with a platform check rather than assuming.
    if platform.system() != "Windows":
        try:
            os.chmod(settings_file, 0o600)
        except OSError:
            pass
    ok(f"Created {settings_file}")
else:
    info("settings.json already exists — not overwritten")

# ── Autostart ─────────────────────────────────────────────────────────────────
hdr("Autostart")
if args.no_autostart:
    info("Skipped (--no-autostart)")
else:
    print()
    enable_as = input("  Enable autostart at Windows login? [y/N] ").strip() or 'N'
    if enable_as.lower() == 'y':
        start_py = BIN_DIR / 'start.py'
        cmd_val  = f'"{sys.executable}" "{start_py}"'
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run',
                0, winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, 'Maestro', 0, winreg.REG_SZ, cmd_val)
            winreg.CloseKey(key)
            # Sync autostart=true into settings.json
            s = json.loads(settings_file.read_text())
            s['autostart'] = True
            settings_file.write_text(json.dumps(s, indent=2) + '\n')
            ok("Autostart enabled via registry HKCU\\...\\Run")
            info(f"  Command: {cmd_val}")
        except Exception as ex:
            warn(f"Could not write registry key: {ex}")
            warn("  Enable autostart manually from the ⚙ Settings modal after launch.")
    else:
        info("Autostart not enabled. Enable it later from the ⚙ Settings modal.")

# ── Shortcut ──────────────────────────────────────────────────────────────────
# Native Start Menu .lnk shortcut, so Maestro shows up like any installed app.
# Targets pythonw.exe directly (bypassing start.bat) so no console window ever
# flashes on launch — tray + background process only.
hdr("Shortcut")
print()
create_shortcut = input(
    "  Create a Start Menu shortcut (silent launch, no console window)? [Y/n] "
).strip() or 'Y'
if create_shortcut.lower() == 'y':
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    start_py = BIN_DIR / 'start.py'
    if pythonw.exists():
        shortcut_target    = str(pythonw)
        shortcut_arguments = f'"{start_py}"'
    else:
        warn(f"pythonw.exe not found next to {sys.executable}")
        warn("  Falling back to start.bat — a console window may flash briefly on launch")
        shortcut_target    = str(BIN_DIR / 'start.bat')
        shortcut_arguments = ''
    start_menu_dir = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
    try:
        start_menu_dir.mkdir(parents=True, exist_ok=True)
        shortcut_path = start_menu_dir / 'Maestro.lnk'
        _create_shortcut_win(
            str(shortcut_path), shortcut_target, shortcut_arguments,
            working_dir=str(BIN_DIR), description='Maestro — process orchestrator',
        )
        ok(f"Shortcut created → {shortcut_path}")
        info("  Find it in the Start Menu by searching 'Maestro'.")
    except Exception as ex:
        warn(f"Could not create shortcut: {ex}")
        warn(f"  You can still launch Maestro via {BIN_DIR}\\start.bat")
else:
    info(f"Skipped. Launch manually via {BIN_DIR}\\start.bat")

# ── Done ──────────────────────────────────────────────────────────────────────
print()
print(f"{BOLD}  ✓ Maestro installed successfully!{RST}")
print()
print(f"  {BOLD}Launch:{RST}")
print(f"    Double-click  {BIN_DIR}\\start.bat")
print(f"    Or run:       python \"{BIN_DIR}\\start.py\"")
print(f"    Or from the Start Menu, if the shortcut was created")
print()
print(f"  {BOLD}UI:{RST}")
print(f"    http://127.0.0.1:8865  (after launch)")
print()
print(f"  {BOLD}Data directory:{RST}")
print(f"    {DATA_DIR}")
print(f"    {DIM}config.txt, settings.json, logs\\{RST}")
if not BASH_PATH:
    print()
    warn("Remember: install WSL or Git Bash to be able to run .sh scripts.")
print()
print(f"  {DIM}To uninstall: delete {INSTALL_DIR}{RST}")
print()
