#!/usr/bin/env python3
"""
Maestro tray.py — System tray daemon (v2.0)
Path: tray.py

Cross-platform: Linux (GTK AppIndicator3 / StatusIcon / Headless) + Windows (Win32 ctypes).
Zero external dependencies.

Launched by start.sh (Linux) or start.py (cross-platform):
    python3 tray.py --bin-dir <path>

Linux backends (tried in order):
  1. AppIndicator3  — preferred; requires libappindicator-gtk3
  2. GtkStatusIcon  — GTK3 fallback; works without AppIndicator extension
  3. Headless       — no GUI; launches main.py and prints URL to console

Windows backend:
  - Win32 ctypes: NOTIFYICONDATA message pump, GDI colored-dot icons,
    balloon notifications, HKCU registry autostart (winreg stdlib)

Notification design:
  - Linux: notify-send subprocess called directly by this process.
  - Windows: balloon via Shell_NotifyIcon; daemon.py's own _notify()
    is a no-op on Windows — all notifications reach the user through
    tray.py's SSE listener → _notify() → _notify_win32() balloon.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
from urllib.error import URLError

# ── Platform detection ─────────────────────────────────────────────────────────
_WIN = sys.platform == 'win32'

# ── Windows: imports, constants, ctypes structures, GDI icon helper ────────────
if _WIN:
    import ctypes, ctypes.wintypes, winreg

    # Window / tray message constants
    _WM_DESTROY       = 0x0002
    _WM_CLOSE         = 0x0010
    _WM_RBUTTONUP     = 0x0205
    _WM_LBUTTONDBLCLK = 0x0203
    _WM_APP_TRAY      = 0x8001   # WM_APP+1 — tray icon callback

    # Shell_NotifyIcon operations
    _NIM_ADD    = 0x0
    _NIM_MODIFY = 0x1
    _NIM_DELETE = 0x2

    # NOTIFYICONDATA flags
    _NIF_MESSAGE = 0x1
    _NIF_ICON    = 0x2
    _NIF_TIP     = 0x4
    _NIF_INFO    = 0x10   # balloon tooltip

    # Balloon icon type
    _NIIF_INFO    = 0x1
    _NIIF_NOSOUND = 0x10

    # AppendMenu / TrackPopupMenu flags
    _MF_STRING    = 0x00000000
    _MF_SEPARATOR = 0x00000800
    _TPM_LEFTALIGN   = 0x0000
    _TPM_RIGHTBUTTON = 0x0002
    _TPM_RETURNCMD   = 0x0100   # return selection instead of posting WM_COMMAND

    # GDI: NULL_PEN stock object (no border on drawn shapes)
    _NULL_PEN = 8

    # Context menu item IDs
    _ID_OPEN      = 1001
    _ID_RESTART   = 1002
    _ID_AUTOSTART = 1003
    _ID_SHUTDOWN  = 1004
    _ID_QUIT      = 1005

    # WndProc function type
    _WNDPROC_TYPE = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.wintypes.HWND,
        ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    )

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ('cbSize',        ctypes.wintypes.UINT),
            ('style',         ctypes.wintypes.UINT),
            ('lpfnWndProc',   _WNDPROC_TYPE),
            ('cbClsExtra',    ctypes.c_int),
            ('cbWndExtra',    ctypes.c_int),
            ('hInstance',     ctypes.wintypes.HANDLE),
            ('hIcon',         ctypes.c_void_p),
            ('hCursor',       ctypes.c_void_p),
            ('hbrBackground', ctypes.c_void_p),
            ('lpszMenuName',  ctypes.c_wchar_p),
            ('lpszClassName', ctypes.c_wchar_p),
            ('hIconSm',       ctypes.c_void_p),
        ]

    class _NOTIFYICONDATA(ctypes.Structure):
        """NOTIFYICONDATAW — V2 without GUID (XP+ compatible)."""
        _fields_ = [
            ('cbSize',           ctypes.wintypes.DWORD),
            ('hWnd',             ctypes.wintypes.HWND),
            ('uID',              ctypes.wintypes.UINT),
            ('uFlags',           ctypes.wintypes.UINT),
            ('uCallbackMessage', ctypes.wintypes.UINT),
            ('hIcon',            ctypes.c_void_p),
            ('szTip',            ctypes.c_wchar * 128),
            ('dwState',          ctypes.wintypes.DWORD),
            ('dwStateMask',      ctypes.wintypes.DWORD),
            ('szInfo',           ctypes.c_wchar * 256),
            ('uVersion',         ctypes.wintypes.UINT),
            ('szInfoTitle',      ctypes.c_wchar * 64),
            ('dwInfoFlags',      ctypes.wintypes.DWORD),
        ]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [
            ('fIcon',    ctypes.wintypes.BOOL),
            ('xHotspot', ctypes.wintypes.DWORD),
            ('yHotspot', ctypes.wintypes.DWORD),
            ('hbmMask',  ctypes.c_void_p),   # HBITMAP
            ('hbmColor', ctypes.c_void_p),   # HBITMAP
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

    class _RECT(ctypes.Structure):
        _fields_ = [
            ('left',   ctypes.c_long), ('top',    ctypes.c_long),
            ('right',  ctypes.c_long), ('bottom', ctypes.c_long),
        ]

    class _MSG(ctypes.Structure):
        _fields_ = [
            ('hWnd',    ctypes.wintypes.HWND),
            ('message', ctypes.wintypes.UINT),
            ('wParam',  ctypes.wintypes.WPARAM),
            ('lParam',  ctypes.wintypes.LPARAM),
            ('time',    ctypes.wintypes.DWORD),
            ('pt',      _POINT),
        ]

    def _create_dot_hicon(dot_rgb: tuple, size: int = 16) -> int:
        """
        Create an HICON with a dark background and a colored dot via GDI ctypes.
        dot_rgb: (R, G, B) as 0-255 integers. Returns an HICON handle (int).
        The dot is placed in the bottom-right quadrant (matching the Linux Cairo icon).
        """
        r, g, b  = dot_rgb
        dot_bgr  = (b << 16) | (g << 8) | r          # GDI uses BGR byte order
        dark_bgr = (0x08 << 16) | (0x08 << 8) | 0x0E  # #0E0808 — Maestro dark bg

        hdc_screen = ctypes.windll.user32.GetDC(None)
        hdc        = ctypes.windll.gdi32.CreateCompatibleDC(hdc_screen)
        hbm_color  = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc_screen, size, size)
        ctypes.windll.user32.ReleaseDC(None, hdc_screen)

        old = ctypes.windll.gdi32.SelectObject(hdc, hbm_color)

        # Fill dark background
        bg_br = ctypes.windll.gdi32.CreateSolidBrush(dark_bgr)
        rect  = _RECT(0, 0, size, size)
        ctypes.windll.user32.FillRect(hdc, ctypes.byref(rect), bg_br)
        ctypes.windll.gdi32.DeleteObject(bg_br)

        # Draw colored dot (bottom-right, no outline)
        dot_r  = max(3, size // 4)
        cx, cy = size - dot_r - 2, size - dot_r - 2
        dot_br = ctypes.windll.gdi32.CreateSolidBrush(dot_bgr)
        old_br = ctypes.windll.gdi32.SelectObject(hdc, dot_br)
        null_p = ctypes.windll.gdi32.GetStockObject(_NULL_PEN)
        old_p  = ctypes.windll.gdi32.SelectObject(hdc, null_p)
        ctypes.windll.gdi32.Ellipse(
            hdc,
            cx - dot_r, cy - dot_r,
            cx + dot_r + 1, cy + dot_r + 1,
        )
        ctypes.windll.gdi32.SelectObject(hdc, old_br)
        ctypes.windll.gdi32.SelectObject(hdc, old_p)
        ctypes.windll.gdi32.DeleteObject(dot_br)

        ctypes.windll.gdi32.SelectObject(hdc, old)
        ctypes.windll.gdi32.DeleteDC(hdc)

        # Monochrome mask: all zeros = fully opaque color bitmap
        hbm_mask = ctypes.windll.gdi32.CreateBitmap(size, size, 1, 1, None)

        ii = _ICONINFO(
            fIcon=True, xHotspot=0, yHotspot=0,
            hbmMask=hbm_mask, hbmColor=hbm_color,
        )
        hicon = ctypes.windll.user32.CreateIconIndirect(ctypes.byref(ii))

        ctypes.windll.gdi32.DeleteObject(hbm_color)
        ctypes.windll.gdi32.DeleteObject(hbm_mask)
        return hicon

    # Module-level balloon callback — set by MaestroTray._setup_win32()
    # Used by tray.py's own _notify() to show balloon notifications.
    # This is tray.py-internal only; daemon.py runs in a separate process.
    _notify_win_tray_cb = None

else:
    # ── Linux: detect available GTK backend ───────────────────────────────────
    _GTK_MODE = 'headless'
    try:
        import gi
        gi.require_version("Gtk",          "3.0")
        gi.require_version("AppIndicator3","0.1")
        gi.require_version("GLib",         "2.0")
        from gi.repository import Gtk, AppIndicator3, GLib
        import cairo
        _GTK_MODE = 'appindicator3'
    except Exception:
        try:
            import gi
            gi.require_version("Gtk",  "3.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gtk, GLib
            import cairo
            _GTK_MODE = 'statusicon'
        except Exception:
            pass   # headless

# ── CLI args ───────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--bin-dir", default="")
_args, _ = _parser.parse_known_args()

BIN_DIR = Path(_args.bin_dir).resolve() if _args.bin_dir else Path(__file__).parent

# ── Path resolution ────────────────────────────────────────────────────────────
def _resolve_data_dir() -> Path:
    env = os.environ.get("MAESTRO_DATA", "").strip()
    if env:
        return Path(env).resolve()
    default       = (BIN_DIR.parent / "data").resolve()
    settings_probe = default / "settings.json"
    if settings_probe.exists():
        try:
            dd = json.loads(settings_probe.read_text()).get("dataDir", "").strip()
            if dd:
                return Path(dd).resolve()
        except Exception:
            pass
    return default

DATA_DIR          = _resolve_data_dir()
SETTINGS_FILE     = DATA_DIR / "settings.json"
ICON_PNG          = BIN_DIR / "img" / "MaestroLogo24x24.png"
MAIN_PY           = BIN_DIR / "main.py"
START_SH          = BIN_DIR / "start.sh"
START_PY          = BIN_DIR / "start.py"
AUTOSTART_DESKTOP = Path.home() / ".config" / "autostart" / "maestro.desktop"

# Marker file written by main.py's POST /api/update-maestro handler once a
# channel's source zip has been downloaded, validated, and staged into a
# clean temp directory. Checked by _watch_main() after main.py exits —
# see _check_and_apply_update() / _perform_update() below.
UPDATE_MARKER = DATA_DIR / "update_request.json"

# Temp dir for Cairo-generated Linux PNG tray icons
_ICON_TMPDIR = Path(tempfile.mkdtemp(prefix="maestro_tray_"))

# ── Default settings ───────────────────────────────────────────────────────────
_DEFAULT_SETTINGS = {
    "fileExplorer":        False,
    "logsRecorder":        False,
    "openBrowserAtLaunch": False,
    "autostart":           False,
    "trayPersistent":      True,
    "systemNotifications": True,
    "host":                "127.0.0.1",
    "port":                8865,
    "dataDir":             "",
    "githubToken":         "",
}


def _load_settings() -> dict:
    s = dict(_DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            on_disk = json.loads(SETTINGS_FILE.read_text())
            s.update({k: v for k, v in on_disk.items() if k in _DEFAULT_SETTINGS})
        except Exception:
            pass
    return s


def _write_settings(s: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".json", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
            f.write("\n")
        shutil.move(tmp, str(SETTINGS_FILE))
    except Exception as ex:
        print(f"[tray] _write_settings error: {ex}", file=sys.stderr)


def _get_url() -> str:
    s = _load_settings()
    return f"http://{s.get('host','127.0.0.1')}:{int(s.get('port',8865))}"


# ── Notifications ──────────────────────────────────────────────────────────────
def _notify(title: str, body: str):
    """
    Desktop notification (best-effort, silent on failure).

    Linux : notify-send subprocess.
    Windows: balloon via tray icon (Shell_NotifyIcon NIF_INFO).
             _notify_win_tray_cb is set by MaestroTray._setup_win32().
             daemon.py's own _notify() is a no-op on Windows; all daemon
             events reach the user via tray.py's SSE listener instead.

    Respects the systemNotifications setting.
    """
    if not _load_settings().get("systemNotifications", True):
        return
    if _WIN:
        cb = _notify_win_tray_cb
        if cb:
            try:
                cb(title, body)
            except Exception:
                pass
        return
    # Linux: notify-send subprocess
    cmd = ["notify-send", title, body, "--expire-time", "4000"]
    if ICON_PNG.exists():
        cmd += ["--icon", str(ICON_PNG)]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ── Autostart ──────────────────────────────────────────────────────────────────
_DESKTOP_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Maestro
Exec=bash {start_sh}
Hidden=false
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _write_autostart():
    if _WIN:
        _write_autostart_win()
    else:
        AUTOSTART_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_DESKTOP.write_text(
            _DESKTOP_TEMPLATE.format(start_sh=str(START_SH))
        )


def _remove_autostart():
    if _WIN:
        _remove_autostart_win()
    else:
        try:    AUTOSTART_DESKTOP.unlink(missing_ok=True)
        except: pass


def _write_autostart_win():
    """Add Maestro to Windows startup via HKCU\\...\\Run registry key."""
    launcher = START_PY if START_PY.exists() else MAIN_PY
    cmd_val  = f'"{sys.executable}" "{launcher}"'
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "Maestro", 0, winreg.REG_SZ, cmd_val)
        winreg.CloseKey(key)
    except Exception as ex:
        print(f"[tray] autostart write error: {ex}", file=sys.stderr)


def _remove_autostart_win():
    """Remove Maestro from Windows startup registry."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, "Maestro")
        winreg.CloseKey(key)
    except Exception:
        pass


def _set_autostart(enabled: bool):
    s = _load_settings()
    s["autostart"] = enabled
    _write_settings(s)
    if enabled: _write_autostart()
    else:       _remove_autostart()


# ── Linux icon generation (Cairo) ─────────────────────────────────────────────
# Dot colors as Cairo float tuples (0.0–1.0); used only on Linux.
_DOT_GREEN = (0.0,   0.902, 0.463)   # #00e676
_DOT_RED   = (0.906, 0.298, 0.235)   # #e74c3c
_DOT_GREY  = (0.365, 0.427, 0.494)   # #5d6d7e

_ICON_PATHS: dict = {}   # state str -> abs PNG path (populated by _init_icons on Linux)


def _generate_icon_png(dot_color: tuple, out_path: Path):
    """
    Draw a 24×24 PNG: Maestro logo background + colored dot bottom-right.
    Falls back to a plain dark square if the logo PNG is missing.
    Only called on Linux (Cairo available).
    """
    size    = 24
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx     = cairo.Context(surface)

    logo_loaded = False
    if ICON_PNG.exists():
        try:
            ls = cairo.ImageSurface.create_from_png(str(ICON_PNG))
            sw, sh = ls.get_width(), ls.get_height()
            if sw > 0 and sh > 0:
                ctx.save()
                ctx.scale(size / sw, size / sh)
                ctx.set_source_surface(ls, 0, 0)
                ctx.paint()
                ctx.restore()
                logo_loaded = True
        except Exception:
            pass

    if not logo_loaded:
        ctx.set_source_rgb(0.055, 0.031, 0.031)
        ctx.rectangle(0, 0, size, size)
        ctx.fill()

    r, g, b = dot_color
    cx, cy  = 18.0, 18.0
    ctx.arc(cx, cy, 5.0, 0, 2 * 3.14159265)
    ctx.set_source_rgba(0.055, 0.031, 0.031, 0.9)
    ctx.fill()
    ctx.arc(cx, cy, 4.0, 0, 2 * 3.14159265)
    ctx.set_source_rgb(r, g, b)
    ctx.fill()

    surface.write_to_png(str(out_path))


def _init_icons():
    """Generate the 3 state PNG icons into a temp dir. Linux-only (Cairo)."""
    if _WIN:
        return
    states = {
        "up":   (_DOT_GREEN, _ICON_TMPDIR / "maestro_up.png"),
        "down": (_DOT_RED,   _ICON_TMPDIR / "maestro_down.png"),
        "off":  (_DOT_GREY,  _ICON_TMPDIR / "maestro_off.png"),
    }
    for state, (color, path) in states.items():
        _generate_icon_png(color, path)
        _ICON_PATHS[state] = str(path)


# ── Tray application ───────────────────────────────────────────────────────────
class MaestroTray:
    STATE_UP   = "up"
    STATE_DOWN = "down"
    STATE_OFF  = "off"

    def __init__(self):
        self._state            = self.STATE_DOWN
        self._main_proc        = None
        self._intentional_stop = False
        self._restart_pending  = False
        self._settings         = _load_settings()
        self._headless_stop    = threading.Event()

        if _WIN:
            self._setup_win32()
        elif _GTK_MODE == 'appindicator3':
            self._setup_appindicator3()
        elif _GTK_MODE == 'statusicon':
            self._setup_statusicon()
        # headless: nothing to set up

    # ── Backend setup ──────────────────────────────────────────────────────────
    def _setup_appindicator3(self):
        self._ind = AppIndicator3.Indicator.new(
            "maestro",
            _ICON_PATHS.get(self.STATE_DOWN, "dialog-information"),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._ind.set_menu(self._build_menu_gtk())

    def _setup_statusicon(self):
        """GTK StatusIcon backend — works without the AppIndicator3 extension."""
        self._si = Gtk.StatusIcon()
        icon_path = _ICON_PATHS.get(self.STATE_DOWN, '')
        if icon_path:
            self._si.set_from_file(icon_path)
        self._si.set_tooltip_text("Maestro — starting…")
        self._si.set_visible(True)
        self._si.connect("popup-menu", self._on_popup_statusicon)
        self._si.connect("activate",   lambda *a: self._open_browser())

    def _setup_win32(self):
        """
        Register a Win32 window class, create a hidden message window,
        add the tray icon via Shell_NotifyIcon, and pre-generate GDI icons.
        """
        global _notify_win_tray_cb

        self._cls_name = f"MaestroTrayWnd_{os.getpid()}"

        # Keep WndProc reference alive to prevent GC
        self._wndproc_ref = _WNDPROC_TYPE(self._wndproc)

        wc = _WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(wc)
        wc.lpfnWndProc   = self._wndproc_ref
        wc.hInstance     = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._cls_name
        ctypes.windll.user32.RegisterClassExW(ctypes.byref(wc))

        # Hidden message-only window (no visible window, just a message target)
        self._hwnd = ctypes.windll.user32.CreateWindowExW(
            0, self._cls_name, "Maestro", 0,
            0, 0, 0, 0, None, None, wc.hInstance, None,
        )

        # Pre-create GDI HICON for each state (colors match Linux Cairo dots)
        self._hicons = {
            self.STATE_UP:   _create_dot_hicon((0,   230, 118)),   # #00e676 green
            self.STATE_DOWN: _create_dot_hicon((231, 76,  60)),    # #e74c3c red
            self.STATE_OFF:  _create_dot_hicon((93,  109, 126)),   # #5d6d7e grey
        }

        # Add tray icon
        nid = _NOTIFYICONDATA()
        nid.cbSize           = ctypes.sizeof(nid)
        nid.hWnd             = self._hwnd
        nid.uID              = 1
        nid.uFlags           = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = _WM_APP_TRAY
        nid.hIcon            = self._hicons[self.STATE_DOWN]
        nid.szTip            = "Maestro — starting…"
        ctypes.windll.shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid))

        # Register balloon callback for tray.py's own _notify()
        _notify_win_tray_cb = self._notify_win32

    # ── GTK menu (Linux) ───────────────────────────────────────────────────────
    def _build_menu_gtk(self) -> 'Gtk.Menu':
        """Build and return the right-click GTK context menu."""
        self._settings = _load_settings()
        menu = Gtk.Menu()

        item_open = Gtk.MenuItem(label="Open in Browser")
        item_open.connect("activate", lambda _: self._open_browser())
        menu.append(item_open)

        label_r = "⚠ Restart to apply changes" if self._restart_pending else "Restart Maestro"
        item_restart = Gtk.MenuItem(label=label_r)
        item_restart.connect("activate", lambda _: self._restart())
        menu.append(item_restart)

        menu.append(Gtk.SeparatorMenuItem())

        auto_on   = self._settings.get("autostart", False)
        item_auto = Gtk.MenuItem(label=f"Autostart with OS: {'ON' if auto_on else 'OFF'}")
        item_auto.connect("activate", lambda _: self._toggle_autostart())
        menu.append(item_auto)

        menu.append(Gtk.SeparatorMenuItem())

        item_off = Gtk.MenuItem(label="Shut Down Maestro")
        item_off.connect("activate", lambda _: self._shutdown())
        menu.append(item_off)

        item_quit = Gtk.MenuItem(label="Quit Maestro")
        item_quit.connect("activate", lambda _: self._quit())
        menu.append(item_quit)

        menu.show_all()
        return menu

    def _on_popup_statusicon(self, icon, button, timestamp):
        """Show the GTK context menu anchored to the StatusIcon position."""
        menu = self._build_menu_gtk()
        menu.popup(
            None, None,
            Gtk.StatusIcon.position_menu, icon,
            button, timestamp,
        )

    def _refresh_menu(self):
        """Re-attach a fresh GTK menu (AppIndicator3 only; StatusIcon builds on popup)."""
        if _GTK_MODE == 'appindicator3':
            GLib.idle_add(lambda: self._ind.set_menu(self._build_menu_gtk()))

    # ── Win32 WndProc ──────────────────────────────────────────────────────────
    def _wndproc(self, hwnd, msg, wparam, lparam):
        """Win32 window procedure — handles tray callbacks and window lifecycle."""
        if msg == _WM_APP_TRAY:
            # Right-click or double-click on tray icon
            if lparam in (_WM_RBUTTONUP, _WM_LBUTTONDBLCLK):
                self._show_menu_win32()
        elif msg == _WM_CLOSE:
            ctypes.windll.user32.DestroyWindow(hwnd)
        elif msg == _WM_DESTROY:
            # Remove tray icon then exit the message loop
            nid = _NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd   = hwnd
            nid.uID    = 1
            ctypes.windll.shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(nid))
            ctypes.windll.user32.PostQuitMessage(0)
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu_win32(self):
        """
        Build and display the Win32 popup context menu at the cursor position.
        Must be called from the message pump thread (i.e. from _wndproc).
        Uses TPM_RETURNCMD to receive the selection without posting WM_COMMAND.
        """
        self._settings = _load_settings()
        menu = ctypes.windll.user32.CreatePopupMenu()

        ctypes.windll.user32.AppendMenuW(menu, _MF_STRING,    _ID_OPEN,      "Open in Browser")
        label_r = "⚠ Restart to apply changes" if self._restart_pending else "Restart Maestro"
        ctypes.windll.user32.AppendMenuW(menu, _MF_STRING,    _ID_RESTART,   label_r)
        ctypes.windll.user32.AppendMenuW(menu, _MF_SEPARATOR, 0,             None)
        auto_on = self._settings.get("autostart", False)
        ctypes.windll.user32.AppendMenuW(
            menu, _MF_STRING, _ID_AUTOSTART,
            f"Autostart with OS: {'ON' if auto_on else 'OFF'}",
        )
        ctypes.windll.user32.AppendMenuW(menu, _MF_SEPARATOR, 0,             None)
        ctypes.windll.user32.AppendMenuW(menu, _MF_STRING,    _ID_SHUTDOWN,  "Shut Down Maestro")
        ctypes.windll.user32.AppendMenuW(menu, _MF_STRING,    _ID_QUIT,      "Quit Maestro")

        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        # Required before TrackPopupMenu per MSDN
        ctypes.windll.user32.SetForegroundWindow(self._hwnd)

        cmd = ctypes.windll.user32.TrackPopupMenu(
            menu,
            _TPM_LEFTALIGN | _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
            pt.x, pt.y, 0, self._hwnd, None,
        )
        ctypes.windll.user32.DestroyMenu(menu)

        # Dispatch selected item in a daemon thread (keep WndProc responsive)
        dispatch = {
            _ID_OPEN:      self._open_browser,
            _ID_RESTART:   self._restart,
            _ID_AUTOSTART: self._toggle_autostart,
            _ID_SHUTDOWN:  self._shutdown,
            _ID_QUIT:      self._quit,
        }
        fn = dispatch.get(cmd)
        if fn:
            threading.Thread(target=fn, daemon=True).start()

    def _notify_win32(self, title: str, body: str):
        """
        Show a Win32 balloon notification via Shell_NotifyIcon NIF_INFO.
        Thread-safe — can be called from any thread.
        """
        if not hasattr(self, '_hwnd'):
            return
        nid = _NOTIFYICONDATA()
        nid.cbSize      = ctypes.sizeof(nid)
        nid.hWnd        = self._hwnd
        nid.uID         = 1
        nid.uFlags      = _NIF_INFO
        nid.szInfo      = body[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = _NIIF_INFO | _NIIF_NOSOUND
        ctypes.windll.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(nid))

    # ── Icon update ────────────────────────────────────────────────────────────
    def _update_icon(self, state: str):
        """Update the tray icon to reflect the new state. Thread-safe on all backends."""
        self._state = state
        if _WIN:
            if not hasattr(self, '_hwnd'):
                return
            nid = _NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd   = self._hwnd
            nid.uID    = 1
            nid.uFlags = _NIF_ICON | _NIF_TIP
            nid.hIcon  = self._hicons.get(state, self._hicons[self.STATE_DOWN])
            nid.szTip  = f"Maestro — {state}"
            ctypes.windll.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(nid))
        elif _GTK_MODE == 'appindicator3':
            icon_path = _ICON_PATHS.get(state, _ICON_PATHS.get(self.STATE_DOWN))
            GLib.idle_add(self._ind.set_icon_full, icon_path, f"Maestro — {state}")
        elif _GTK_MODE == 'statusicon':
            icon_path = _ICON_PATHS.get(state, _ICON_PATHS.get(self.STATE_DOWN))
            GLib.idle_add(self._si.set_from_file, icon_path)
            GLib.idle_add(self._si.set_tooltip_text, f"Maestro — {state}")
        # headless: state change is silent (URL still accessible in browser)

    # ── Actions ────────────────────────────────────────────────────────────────
    def _open_browser(self):
        try:
            if _WIN:
                os.startfile(_get_url())   # opens default browser, no shell needed
            else:
                subprocess.Popen(
                    ["xdg-open", _get_url()],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def _toggle_autostart(self):
        self._settings = _load_settings()
        new_val = not self._settings.get("autostart", False)
        _set_autostart(new_val)
        self._settings["autostart"] = new_val
        _notify("Maestro", f"Autostart {'enabled' if new_val else 'disabled'}")
        if not _WIN:
            self._refresh_menu()

    def _restart(self):
        self._intentional_stop = False
        self._restart_pending  = False
        self._stop_main()
        time.sleep(0.5)
        self._start_main()
        if not _WIN:
            self._refresh_menu()

    def _shutdown(self):
        self._intentional_stop = True
        url = _get_url()
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{url}/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        time.sleep(1.0)
        self._stop_main()
        self._update_icon(self.STATE_OFF)
        _notify("Maestro", "Shut down.")

        # Exit the main loop (platform-specific)
        self._settings = _load_settings()
        if _WIN:
            if not self._settings.get("trayPersistent", True):
                if hasattr(self, '_hwnd'):
                    ctypes.windll.user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
            # If trayPersistent: keep message loop alive with grey icon;
            # user can restart via tray menu.
        elif _GTK_MODE in ('appindicator3', 'statusicon'):
            if not self._settings.get("trayPersistent", True):
                GLib.idle_add(Gtk.main_quit)
        else:
            self._headless_stop.set()

    def _quit(self):
        """Quit Maestro entirely: stops main.py AND exits the tray. Ignores trayPersistent."""
        self._intentional_stop = True
        url = _get_url()
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{url}/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        time.sleep(1.0)
        self._stop_main()
        self._update_icon(self.STATE_OFF)
        _notify("Maestro", "Quit.")
        # Always exit the tray loop — ignores trayPersistent
        if _WIN:
            if hasattr(self, '_hwnd'):
                ctypes.windll.user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
        elif _GTK_MODE in ('appindicator3', 'statusicon'):
            GLib.idle_add(Gtk.main_quit)
        else:
            self._headless_stop.set()

    # ── main.py subprocess lifecycle ───────────────────────────────────────────
    def _start_main(self):
        # Gracefully stop any orphaned main.py from a previous force-close.
        # When tray.py is killed hard, main.py can survive as an orphan on port 8865.
        url = _get_url()
        try:
            import urllib.request as _ur
            _ur.urlopen(_ur.Request(
                f"{url}/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            ), timeout=2)
            time.sleep(0.8)
        except Exception:
            pass   # nothing orphaned — proceed normally

        self._settings = _load_settings()
        cmd = [sys.executable, str(MAIN_PY), "--data-dir", str(DATA_DIR)]
        try:
            self._main_proc = subprocess.Popen(
                cmd, cwd=str(BIN_DIR), stdin=subprocess.DEVNULL,
            )
            threading.Thread(target=self._watch_main, daemon=True).start()
            _notify("Maestro", "Starting…")
        except Exception as ex:
            print(f"[tray] Failed to start main.py: {ex}", file=sys.stderr)
            self._update_icon(self.STATE_DOWN)

    def _stop_main(self):
        proc = self._main_proc
        self._main_proc = None
        if not proc or proc.poll() is not None:
            return
        try:    proc.terminate()
        except: pass
        def _force():
            time.sleep(3.0)
            if proc.poll() is None:
                try:    proc.kill()
                except: pass
        threading.Thread(target=_force, daemon=True).start()

    def _watch_main(self):
        proc = self._main_proc
        if proc is None:
            return
        proc.wait()
        if self._intentional_stop:
            return
        # An "Update Maestro" request (see main.py's /api/update-maestro)
        # makes main.py exit voluntarily via the same graceful shutdown path
        # as a normal /api/shutdown — this is NOT tray-initiated, so
        # self._intentional_stop is still False here. Check for a pending
        # update marker BEFORE falling through to crash handling, or every
        # update would be misreported as "Stopped unexpectedly".
        if self._check_and_apply_update():
            return
        rc = proc.returncode
        self._update_icon(self.STATE_DOWN)
        _notify("Maestro",
                f"Stopped unexpectedly (exit {rc}). Use the tray menu to restart.")

    # ── Update Maestro (bin/ swap + full-stack restart) ────────────────────────
    def _check_and_apply_update(self) -> bool:
        """
        Check for a pending update request written by main.py's
        POST /api/update-maestro handler. If present, apply it and return
        True so _watch_main() treats this exit as an intentional update
        restart rather than a crash. Returns False if no update is pending.
        """
        if not UPDATE_MARKER.exists():
            return False
        try:
            marker = json.loads(UPDATE_MARKER.read_text())
        except Exception as ex:
            print(f"[tray] ✕ Could not read update marker: {ex}", file=sys.stderr)
            try: UPDATE_MARKER.unlink(missing_ok=True)
            except Exception: pass
            return False
        try: UPDATE_MARKER.unlink(missing_ok=True)
        except Exception: pass
        self._perform_update(marker.get("stagingDir", ""), marker.get("channel", "?"))
        return True

    def _perform_update(self, staging_dir: str, channel: str):
        """
        Apply a staged Maestro update: swap BIN_DIR's contents with the
        already-downloaded-and-validated staged files, then re-exec tray.py
        itself so a change to tray.py itself also takes effect (not just
        main.py/daemon.py, which a simple main.py-only restart would miss).

        No backup is kept after a successful swap — by design (see
        MAESTRO_GUIDE.md "Update Maestro" planned-feature notes: automatic
        backup/rollback was explicitly declined to keep this feature simple).
        The old bin/ is moved aside only transiently, so a failure mid-swap
        can still be recovered from automatically.

        data/ is never touched by any part of this update path.
        """
        staging = Path(staging_dir)
        if not staging.is_dir():
            print(f"[tray] ✕ Update failed — staging dir missing: {staging_dir}", file=sys.stderr)
            _notify("Maestro update failed", "Staged files were not found.")
            self._update_icon(self.STATE_DOWN)
            return

        print(f"[tray] ↻ Applying {channel} update from {staging_dir}…", file=sys.stderr)
        swap_tmp = BIN_DIR.parent / f"bin_swap_tmp_{int(time.time())}"
        try:
            # Step 1: move the current bin/ out of the way
            shutil.move(str(BIN_DIR), str(swap_tmp))
            # Step 2: move the staged files into place as the new bin/
            shutil.move(str(staging), str(BIN_DIR))
            # Step 3: drop the old files — no backup retention, by design
            shutil.rmtree(swap_tmp, ignore_errors=True)
        except Exception as ex:
            print(f"[tray] ✕ Update swap failed: {ex}", file=sys.stderr)
            _notify("Maestro update failed", str(ex))
            # Best-effort restore if step 1 succeeded but step 2 didn't,
            # so a failed update never leaves bin/ missing entirely.
            if swap_tmp.exists() and not BIN_DIR.exists():
                try: shutil.move(str(swap_tmp), str(BIN_DIR))
                except Exception: pass
            self._update_icon(self.STATE_DOWN)
            return

        print(f"[tray] ✓ Update applied — restarting for {channel}", file=sys.stderr)
        _notify("Maestro", f"Update applied ({channel}). Restarting…")
        self._reexec_self()

    def _reexec_self(self):
        """
        Restart tray.py itself from scratch so any change to tray.py's own
        source (just swapped into BIN_DIR) takes effect — not just
        main.py/daemon.py, which _restart() alone would relaunch.

        Linux: os.execv replaces this process's image in place — clean,
        no lingering interpreter, matches start.py's own reasoning for
        preferring execv over Popen+exit on Unix.
        Windows: cannot cleanly execv a GUI/message-loop process the same
        way — spawn a fresh process pointing at the (already swapped-in)
        new tray.py, then exit this one.
        """
        args = [sys.executable, str(BIN_DIR / "tray.py"), "--bin-dir", str(BIN_DIR)]
        if _WIN:
            subprocess.Popen(args, cwd=str(BIN_DIR))
            os._exit(0)
        else:
            os.chdir(str(BIN_DIR))
            os.execv(sys.executable, args)

    # ── Health check loop ──────────────────────────────────────────────────────
    def _health_loop(self):
        import urllib.request
        url = _get_url()
        while True:
            if self._intentional_stop:
                time.sleep(5)
                continue
            try:
                with urllib.request.urlopen(f"{url}/api/state", timeout=3) as resp:
                    resp.read()
                if self._state != self.STATE_UP:
                    self._update_icon(self.STATE_UP)
            except (URLError, OSError):
                if self._state != self.STATE_DOWN and not self._intentional_stop:
                    self._update_icon(self.STATE_DOWN)
            time.sleep(5)

    # ── SSE log listener ───────────────────────────────────────────────────────
    _SSE_FILTERS = [
        ("▶ Started",        lambda line: ("Maestro", _extract_name(line) + " started")),
        ("⚠ Crashed",        lambda line: ("Maestro", _extract_name(line) + " crashed")),
        ("↺ Auto-restart",   lambda line: ("Maestro", _extract_name(line) + " restarting…")),
        ("[UPDATE]",         lambda line: ("Maestro", "Update: " + line.split("]", 2)[-1].strip())),
        ("File logging",     lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
        ("Script not found", lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
        ("Script restored",  lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
    ]

    def _sse_loop(self):
        import urllib.request
        url = _get_url()
        while True:
            if self._intentional_stop:
                time.sleep(5)
                continue
            try:
                req = urllib.request.Request(f"{url}/api/logs/__maestro__")
                with urllib.request.urlopen(req, timeout=None) as resp:
                    buf = b""
                    while True:
                        chunk = resp.read(256)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n\n" in buf:
                            msg, buf = buf.split(b"\n\n", 1)
                            self._handle_sse_message(
                                msg.decode("utf-8", errors="replace")
                            )
            except Exception:
                time.sleep(5)

    def _handle_sse_message(self, msg: str):
        event, data = "log", ""
        for line in msg.splitlines():
            if line.startswith("event:"): event = line[6:].strip()
            elif line.startswith("data:"): data  = line[5:].strip()
        if event != "log" or not data:
            return
        for keyword, builder in self._SSE_FILTERS:
            if keyword in data:
                try:
                    title, body = builder(data)
                    _notify(title, body)
                except Exception:
                    pass
                break

    # ── Platform-specific main loops ───────────────────────────────────────────
    def _run_win32(self):
        """Win32 message loop (blocking). Exits on WM_QUIT (PostQuitMessage)."""
        msg = _MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        # Destroy GDI icon handles
        for hicon in self._hicons.values():
            try:
                ctypes.windll.user32.DestroyIcon(hicon)
            except Exception:
                pass

    def _run_headless(self):
        """
        No tray GUI available — keep main.py alive until SIGINT / SIGTERM.
        Prints the UI URL to stdout so the user can find it.
        """
        import signal as _sig
        try:
            _sig.signal(_sig.SIGTERM, lambda s, f: self._headless_stop.set())
            _sig.signal(_sig.SIGINT,  lambda s, f: self._headless_stop.set())
        except Exception:
            pass
        print(f"[maestro] Tray not available. Open: {_get_url()}", flush=True)
        print("[maestro] Send SIGINT (Ctrl+C) or SIGTERM to stop.", flush=True)
        try:
            self._headless_stop.wait()
        except KeyboardInterrupt:
            pass
        self._shutdown()

    # ── Entry point ────────────────────────────────────────────────────────────
    def run(self):
        self._start_main()

        self._settings = _load_settings()
        if self._settings.get("openBrowserAtLaunch", False):
            def _open_later():
                time.sleep(3.0)
                self._open_browser()
            threading.Thread(target=_open_later, daemon=True).start()

        # Linux autostart .desktop sync (Windows uses registry, handled by _set_autostart)
        if not _WIN and self._settings.get("autostart", False) \
                and not AUTOSTART_DESKTOP.exists():
            _write_autostart()

        threading.Thread(target=self._health_loop, daemon=True).start()
        threading.Thread(target=self._sse_loop,    daemon=True).start()

        if _WIN:
            self._run_win32()
        elif _GTK_MODE in ('appindicator3', 'statusicon'):
            Gtk.main()
        else:
            self._run_headless()


# ── Helpers ────────────────────────────────────────────────────────────────────
def _extract_name(line: str) -> str:
    import re
    m = re.search(r"\[([^\]]+)\]", line)
    if m:
        rest = line[m.end():]
        m2   = re.search(r"\[([^\]]+)\]", rest)
        if m2: return m2.group(1)
        return m.group(1)
    return ""

import urllib.request


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Generate Cairo PNG icons (Linux only; Windows uses GDI icons at runtime)
    if not _WIN:
        _init_icons()

    # Linux autostart .desktop consistency check
    if not _WIN:
        s = _load_settings()
        if s.get("autostart", False) and not AUTOSTART_DESKTOP.exists():
            _write_autostart()
        elif not s.get("autostart", False) and AUTOSTART_DESKTOP.exists():
            _remove_autostart()

    tray = MaestroTray()
    tray.run()


if __name__ == "__main__":
    main()
