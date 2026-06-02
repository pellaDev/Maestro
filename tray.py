#!/usr/bin/env python3
"""
Maestro tray.py — GTK system tray daemon (v2.0)
Path: tray.py

Entry point launched by start.sh:
    python3 tray.py --bin-dir <path>

Responsibilities:
  - Launch main.py as a subprocess (watchdog — does NOT auto-restart on crash)
  - GTK StatusIcon with Cairo dot overlay (green/red/grey)
  - Right-click context menu
  - Health check loop (GET /api/state every 5s)
  - SSE listener on /api/logs/__maestro__ → desktop notifications
  - Autostart .desktop file management
  - Read/write settings.json (dataDir, autostart, openBrowserAtLaunch, trayPersistent)

Dependencies (all zero-pip, present on any Fedora GNOME/XFCE):
  gi.repository.Gtk, gi.repository.GdkPixbuf, gi.repository.GLib
  cairo (via gi.repository)
  subprocess, threading, urllib.request — Python stdlib only
  notify-send — libnotify (present on all Fedora GNOME installs)
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GdkPixbuf, GLib

# cairo is always available alongside GTK on Fedora
import cairo

# ── CLI args ───────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--bin-dir", default="")
_args, _ = _parser.parse_known_args()

BIN_DIR = Path(_args.bin_dir).resolve() if _args.bin_dir else Path(__file__).parent

# ── Path resolution ────────────────────────────────────────────────────────────
# Mirrors the same 3-level priority used in main.py._resolve_data_dir()
def _resolve_data_dir() -> Path:
    # Priority 1: MAESTRO_DATA env var
    env = os.environ.get("MAESTRO_DATA", "").strip()
    if env:
        return Path(env).resolve()
    # Priority 2: dataDir stored in settings.json at default location
    default       = (BIN_DIR.parent / "data").resolve()
    settings_probe = default / "settings.json"
    if settings_probe.exists():
        try:
            dd = json.loads(settings_probe.read_text()).get("dataDir", "").strip()
            if dd:
                return Path(dd).resolve()
        except Exception:
            pass
    # Priority 3: default
    return default


DATA_DIR     = _resolve_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"

ICON_PNG  = BIN_DIR / "img" / "MaestroLogo24x24.png"
ICON_SVG  = BIN_DIR / "img" / "MaestroLogo.svg"
MAIN_PY   = BIN_DIR / "main.py"
START_SH  = BIN_DIR / "start.sh"
AUTOSTART_DESKTOP = Path.home() / ".config" / "autostart" / "maestro.desktop"

# ── Default settings (subset tray.py cares about) ─────────────────────────────
_DEFAULT_SETTINGS = {
    "fileExplorer":        False,
    "logsRecorder":        False,
    "openBrowserAtLaunch": False,
    "autostart":           False,
    "trayPersistent":      True,
    "host":                "127.0.0.1",
    "port":                8865,
    "dataDir":             "",
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
    host = s.get("host", "127.0.0.1")
    port = int(s.get("port", 8865))
    return f"http://{host}:{port}"


# ── Desktop notifications ──────────────────────────────────────────────────────
def _notify(title: str, body: str):
    """Best-effort notify-send. Non-blocking. Silent on failure."""
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


# ── Autostart .desktop management ─────────────────────────────────────────────
_DESKTOP_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Maestro
Exec=bash {start_sh}
Hidden=false
X-GNOME-Autostart-enabled=true
"""


def _write_autostart():
    AUTOSTART_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_DESKTOP.write_text(
        _DESKTOP_TEMPLATE.format(start_sh=str(START_SH))
    )


def _remove_autostart():
    try:
        AUTOSTART_DESKTOP.unlink(missing_ok=True)
    except Exception:
        pass


def _set_autostart(enabled: bool):
    s = _load_settings()
    s["autostart"] = enabled
    _write_settings(s)
    if enabled:
        _write_autostart()
    else:
        _remove_autostart()


# ── Icon rendering with Cairo ──────────────────────────────────────────────────
# Dot colours matching the Maestro CSS palette
_DOT_GREEN  = (0.0, 0.902, 0.463)   # #00e676
_DOT_RED    = (0.906, 0.298, 0.235)  # #e74c3c
_DOT_GREY   = (0.365, 0.427, 0.494)  # #5d6d7e

_ICON_SIZE  = 24   # px


def _make_icon_pixbuf(dot_color: tuple) -> GdkPixbuf.Pixbuf:
    """
    Render the Maestro logo PNG with a coloured dot overlaid in the
    bottom-right corner using Cairo. Returns a GdkPixbuf.
    Falls back to a plain coloured square if the PNG is missing.
    """
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, _ICON_SIZE, _ICON_SIZE)
    ctx     = cairo.Context(surface)

    # Draw base icon (PNG preferred, SVG fallback via plain rect)
    if ICON_PNG.exists():
        try:
            base = GdkPixbuf.Pixbuf.new_from_file_at_size(
                str(ICON_PNG), _ICON_SIZE, _ICON_SIZE
            )
            Gtk.render_icon_pixbuf   # ensure Gtk is initialised
            # Paint pixbuf onto cairo surface
            img_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, _ICON_SIZE, _ICON_SIZE)
            img_ctx     = cairo.Context(img_surface)
            Gdk_cairo_set_source_pixbuf(img_ctx, base, 0, 0)
            img_ctx.paint()
            ctx.set_source_surface(img_surface, 0, 0)
            ctx.paint()
        except Exception:
            _draw_fallback_bg(ctx, dot_color)
    else:
        _draw_fallback_bg(ctx, dot_color)

    # Dot: 7px circle, bottom-right, with 1px dark border
    r, g, b = dot_color
    dx, dy  = _ICON_SIZE - 6, _ICON_SIZE - 6
    radius  = 3.5

    ctx.arc(dx, dy, radius + 1, 0, 2 * 3.14159)
    ctx.set_source_rgba(0.055, 0.031, 0.031, 0.85)  # #0e0808 bg
    ctx.fill()

    ctx.arc(dx, dy, radius, 0, 2 * 3.14159)
    ctx.set_source_rgb(r, g, b)
    ctx.fill()

    # Convert cairo surface → GdkPixbuf
    return _surface_to_pixbuf(surface)


def _draw_fallback_bg(ctx, dot_color):
    """Plain dark square fallback when no PNG is available."""
    ctx.set_source_rgb(0.055, 0.031, 0.031)
    ctx.rectangle(0, 0, _ICON_SIZE, _ICON_SIZE)
    ctx.fill()


def Gdk_cairo_set_source_pixbuf(ctx, pixbuf, x, y):
    """
    Paint a GdkPixbuf onto a Cairo context.
    Uses the canonical GDK helper via ctypes if available, otherwise
    converts manually through a temporary PNG buffer.
    """
    try:
        import ctypes
        gdklib = ctypes.CDLL("libgdk-3.so.0")
        gdklib.gdk_cairo_set_source_pixbuf(
            ctypes.c_void_p(ctx._pointer.contents.value),
            ctypes.c_void_p(pixbuf.__gpointer__),
            ctypes.c_double(x),
            ctypes.c_double(y),
        )
    except Exception:
        # Safe fallback: encode pixbuf as PNG, decode into cairo surface
        _ok, buf = pixbuf.save_to_bufferv("png", [], [])
        if _ok:
            import io
            png_surface = cairo.ImageSurface.create_from_png(io.BytesIO(buf))
            ctx.set_source_surface(png_surface, x, y)


def _surface_to_pixbuf(surface: cairo.ImageSurface) -> GdkPixbuf.Pixbuf:
    """Convert a cairo ARGB32 surface to a GdkPixbuf (handles byte order)."""
    width  = surface.get_width()
    height = surface.get_height()
    data   = surface.get_data()

    # Cairo ARGB32 is BGRA on little-endian; GdkPixbuf expects RGBA
    import array as _array
    buf = _array.array("B", data)
    for i in range(0, len(buf), 4):
        buf[i],     buf[i + 2] = buf[i + 2], buf[i]   # swap R and B

    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(buf.tobytes()),
        GdkPixbuf.Colorspace.RGB,
        True,   # has_alpha
        8,      # bits_per_sample
        width,
        height,
        width * 4,  # rowstride
    )


# ── Tray application ───────────────────────────────────────────────────────────
class MaestroTray:
    # Icon dot states
    STATE_UP   = "up"     # green  — HTTP server responding
    STATE_DOWN = "down"   # red    — server not responding / crashed
    STATE_OFF  = "off"    # grey   — intentionally shut down by user

    def __init__(self):
        self._state             = self.STATE_DOWN
        self._main_proc         = None     # subprocess.Popen for main.py
        self._intentional_stop  = False    # set when user clicks "Shut Down"
        self._restart_pending   = False    # menu hint for host/port changes
        self._settings          = _load_settings()

        # Build tray icon
        self._icon = Gtk.StatusIcon()
        self._icon.set_tooltip_text("Maestro")
        self._icon.connect("popup-menu", self._on_popup)
        self._icon.connect("activate",   self._on_activate)
        self._update_icon(self.STATE_DOWN)

    # ── Icon ───────────────────────────────────────────────────────────────────
    def _update_icon(self, state: str):
        self._state = state
        color = {
            self.STATE_UP:   _DOT_GREEN,
            self.STATE_DOWN: _DOT_RED,
            self.STATE_OFF:  _DOT_GREY,
        }[state]
        pixbuf = _make_icon_pixbuf(color)
        GLib.idle_add(self._icon.set_from_pixbuf, pixbuf)
        tip = {
            self.STATE_UP:   "Maestro — running",
            self.STATE_DOWN: "Maestro — stopped / crashed",
            self.STATE_OFF:  "Maestro — shut down",
        }[state]
        GLib.idle_add(self._icon.set_tooltip_text, tip)

    # ── Activation (left click / double click) ─────────────────────────────────
    def _on_activate(self, icon):
        url = _get_url()
        try:
            subprocess.Popen(
                ["xdg-open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # ── Context menu ───────────────────────────────────────────────────────────
    def _on_popup(self, icon, button, activate_time):
        menu = Gtk.Menu()
        self._settings = _load_settings()

        # Open in Browser
        item_open = Gtk.MenuItem(label="Open in Browser")
        item_open.connect("activate", lambda _: self._on_activate(icon))
        menu.append(item_open)

        # Restart Maestro
        item_restart = Gtk.MenuItem(
            label="⚠ Restart to apply changes" if self._restart_pending else "Restart Maestro"
        )
        item_restart.connect("activate", lambda _: self._restart())
        menu.append(item_restart)

        menu.append(Gtk.SeparatorMenuItem())

        # Autostart toggle
        autostart_on = self._settings.get("autostart", False)
        item_auto = Gtk.MenuItem(
            label=f"Autostart with OS: {'ON' if autostart_on else 'OFF'}"
        )
        item_auto.connect("activate", lambda _: self._toggle_autostart())
        menu.append(item_auto)

        menu.append(Gtk.SeparatorMenuItem())

        # Shut Down Maestro
        item_off = Gtk.MenuItem(label="Shut Down Maestro")
        item_off.connect("activate", lambda _: self._shutdown())
        menu.append(item_off)

        menu.show_all()
        menu.popup(None, None, None, None, button, activate_time)

    # ── Actions ────────────────────────────────────────────────────────────────
    def _toggle_autostart(self):
        self._settings = _load_settings()
        new_val        = not self._settings.get("autostart", False)
        _set_autostart(new_val)
        self._settings["autostart"] = new_val
        state = "enabled" if new_val else "disabled"
        _notify("Maestro", f"Autostart {state}")

    def _restart(self):
        """Kill main.py subprocess and relaunch it."""
        self._intentional_stop = False
        self._restart_pending  = False
        self._stop_main()
        time.sleep(0.5)
        self._start_main()

    def _shutdown(self):
        """
        Attempt a graceful shutdown via /api/shutdown, then kill the subprocess.
        If trayPersistent is False, exit the tray process too.
        """
        self._intentional_stop = True
        url = _get_url()
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{url}/api/shutdown",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        # Give main.py time to shut down cleanly before we SIGTERM it
        time.sleep(1.0)
        self._stop_main()
        self._update_icon(self.STATE_OFF)
        _notify("Maestro", "Shut down.")

        self._settings = _load_settings()
        if not self._settings.get("trayPersistent", True):
            GLib.idle_add(Gtk.main_quit)

    # ── main.py subprocess lifecycle ───────────────────────────────────────────
    def _start_main(self):
        """Launch main.py as a subprocess. Passes --data-dir so it uses the same data directory."""
        self._settings  = _load_settings()
        cmd = [
            sys.executable, str(MAIN_PY),
            "--data-dir", str(DATA_DIR),
        ]
        try:
            self._main_proc = subprocess.Popen(
                cmd,
                cwd=str(BIN_DIR),
                stdin=subprocess.DEVNULL,
                # stdout/stderr go to the terminal that launched tray.py
                # (useful for debugging); redirect to /dev/null for daemon use
            )
            threading.Thread(target=self._watch_main, daemon=True).start()
            _notify("Maestro", "Starting…")
        except Exception as ex:
            print(f"[tray] Failed to start main.py: {ex}", file=sys.stderr)
            self._update_icon(self.STATE_DOWN)

    def _stop_main(self):
        """Terminate the main.py subprocess."""
        proc = self._main_proc
        self._main_proc = None
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        # Give it 3s before SIGKILL
        def _force():
            time.sleep(3.0)
            if proc.poll() is None:
                try:    proc.kill()
                except Exception: pass
        threading.Thread(target=_force, daemon=True).start()

    def _watch_main(self):
        """
        Block until main.py subprocess exits.
        If the exit was NOT intentional, set icon red and notify once.
        Does NOT auto-restart — the user must click Restart in the menu.
        """
        proc = self._main_proc
        if proc is None:
            return
        proc.wait()
        if self._intentional_stop:
            return
        rc = proc.returncode
        self._update_icon(self.STATE_DOWN)
        _notify("Maestro", f"Stopped unexpectedly (exit {rc}). Use the tray menu to restart.")

    # ── Health check loop ──────────────────────────────────────────────────────
    def _health_loop(self):
        """
        Poll /api/state every 5s. Updates icon colour.
        Runs in a daemon thread — never touches GTK directly (uses GLib.idle_add).
        """
        url = _get_url()
        while True:
            if self._intentional_stop:
                time.sleep(5)
                continue
            try:
                with urlopen(f"{url}/api/state", timeout=3) as resp:
                    resp.read()
                if self._state != self.STATE_UP:
                    self._update_icon(self.STATE_UP)
            except (URLError, OSError):
                if self._state != self.STATE_DOWN and not self._intentional_stop:
                    self._update_icon(self.STATE_DOWN)
            time.sleep(5)

    # ── SSE log listener for desktop notifications ─────────────────────────────
    # Keywords in Maestro log lines that warrant a desktop notification.
    # Each tuple: (match_string, notification_body_template)
    _SSE_FILTERS = [
        ("▶ Started",       lambda line: ("Maestro", _extract_name(line) + " started")),
        ("⚠ Crashed",       lambda line: ("Maestro", _extract_name(line) + " crashed")),
        ("↺ Auto-restart",  lambda line: ("Maestro", _extract_name(line) + " restarting…")),
        ("[UPDATE]",        lambda line: ("Maestro", "Update: " + line.split("]", 2)[-1].strip())),
        ("File logging",    lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
        ("Script not found",lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
        ("Script restored", lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
    ]

    def _sse_loop(self):
        """
        Connect to /api/logs/__maestro__ via plain HTTP streaming and
        forward matching lines as desktop notifications.
        Reconnects automatically on disconnect. Runs in a daemon thread.
        """
        import socket
        url   = _get_url()
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
                            self._handle_sse_message(msg.decode("utf-8", errors="replace"))
            except Exception:
                time.sleep(5)   # wait before reconnecting

    def _handle_sse_message(self, msg: str):
        """Parse a raw SSE message block and fire a notification if relevant."""
        event = "log"
        data  = ""
        for line in msg.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event != "log" or not data:
            return
        for keyword, builder in self._SSE_FILTERS:
            if keyword in data:
                try:
                    title, body = builder(data)
                    _notify(title, body)
                except Exception:
                    pass
                break   # one notification per line max

    # ── Entry point ────────────────────────────────────────────────────────────
    def run(self):
        # Start main.py
        self._start_main()

        # Open browser if configured
        self._settings = _load_settings()
        if self._settings.get("openBrowserAtLaunch", False):
            # Give main.py 3s to bind the port before opening the browser
            def _open_browser():
                time.sleep(3.0)
                try:
                    subprocess.Popen(
                        ["xdg-open", _get_url()],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
            threading.Thread(target=_open_browser, daemon=True).start()

        # Restore autostart .desktop if setting is on but file is missing
        if self._settings.get("autostart", False) and not AUTOSTART_DESKTOP.exists():
            _write_autostart()

        # Background threads
        threading.Thread(target=self._health_loop, daemon=True).start()
        threading.Thread(target=self._sse_loop,    daemon=True).start()

        Gtk.main()


# ── Helpers ────────────────────────────────────────────────────────────────────
def _extract_name(line: str) -> str:
    """
    Extract the process display name from a Maestro log line.
    Log format: [HH:MM:SS] [DisplayName] message
    Returns the name in brackets, or empty string if not found.
    """
    import re
    m = re.search(r"\[([^\]]+)\]", line)
    # Skip the timestamp bracket, get the second one
    if m:
        rest = line[m.end():]
        m2 = re.search(r"\[([^\]]+)\]", rest)
        if m2:
            return m2.group(1)
        return m.group(1)
    return ""


# ── urllib import needed in _sse_loop ─────────────────────────────────────────
import urllib.request


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Restore autostart .desktop consistency on startup
    s = _load_settings()
    if s.get("autostart", False) and not AUTOSTART_DESKTOP.exists():
        _write_autostart()
    elif not s.get("autostart", False) and AUTOSTART_DESKTOP.exists():
        # settings says off but file exists — remove it to stay consistent
        _remove_autostart()

    tray = MaestroTray()
    tray.run()


if __name__ == "__main__":
    main()
