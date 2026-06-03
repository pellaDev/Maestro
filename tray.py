#!/usr/bin/env python3
"""
Maestro tray.py — GTK system tray daemon (v2.0)
Path: tray.py

Entry point launched by start.sh:
    python3 tray.py --bin-dir <path>

Responsibilities:
  - Launch main.py as a subprocess (watchdog — does NOT auto-restart on crash)
  - AppIndicator3 tray icon with Cairo dot overlay (green/red/grey)
  - Right-click context menu via Gtk.Menu
  - Health check loop (GET /api/state every 5s)
  - SSE listener on /api/logs/__maestro__ → desktop notifications
  - Autostart .desktop file management
  - Read/write settings.json

Dependencies (all zero-pip, present on Fedora GNOME with appindicator extension):
  gi.repository.Gtk, gi.repository.AppIndicator3, gi.repository.GLib
  cairo — for generating dot icon PNGs
  subprocess, threading, urllib.request — Python stdlib only
  notify-send — libnotify
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
from urllib.error import URLError

import gi
gi.require_version("Gtk",          "3.0")
gi.require_version("AppIndicator3","0.1")
gi.require_version("GLib",         "2.0")
from gi.repository import Gtk, AppIndicator3, GLib

import cairo

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
AUTOSTART_DESKTOP = Path.home() / ".config" / "autostart" / "maestro.desktop"

# Temp dir for generated dot icons (created once at startup)
_ICON_TMPDIR = Path(tempfile.mkdtemp(prefix="maestro_tray_"))

# ── Default settings ───────────────────────────────────────────────────────────
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
    return f"http://{s.get('host','127.0.0.1')}:{int(s.get('port',8865))}"

# ── Desktop notifications ──────────────────────────────────────────────────────
def _notify(title: str, body: str):
    cmd = ["notify-send", title, body, "--expire-time", "4000"]
    if ICON_PNG.exists():
        cmd += ["--icon", str(ICON_PNG)]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ── Autostart ──────────────────────────────────────────────────────────────────
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
    AUTOSTART_DESKTOP.write_text(_DESKTOP_TEMPLATE.format(start_sh=str(START_SH)))

def _remove_autostart():
    try:    AUTOSTART_DESKTOP.unlink(missing_ok=True)
    except: pass

def _set_autostart(enabled: bool):
    s = _load_settings()
    s["autostart"] = enabled
    _write_settings(s)
    if enabled: _write_autostart()
    else:       _remove_autostart()

# ── Icon generation with Cairo ─────────────────────────────────────────────────
# AppIndicator3 uses file paths, not pixbufs.
# We generate 3 small PNG files once at startup (24x24):
# the Maestro logo background + a coloured dot bottom-right.
_DOT_GREEN = (0.0,   0.902, 0.463)   # #00e676
_DOT_RED   = (0.906, 0.298, 0.235)   # #e74c3c
_DOT_GREY  = (0.365, 0.427, 0.494)   # #5d6d7e

_ICON_PATHS: dict = {}   # state -> str path


def _generate_icon_png(dot_color: tuple, out_path: Path):
    """
    Draw a 24x24 PNG: Maestro logo (if available) + coloured dot bottom-right.
    Falls back to a plain dark square if the logo PNG is missing.
    """
    size = 24
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx     = cairo.Context(surface)

    # Background: try to load the logo PNG via cairo directly
    logo_loaded = False
    if ICON_PNG.exists():
        try:
            logo_surface = cairo.ImageSurface.create_from_png(str(ICON_PNG))
            # Scale logo to fit 24x24
            sw = logo_surface.get_width()
            sh = logo_surface.get_height()
            if sw > 0 and sh > 0:
                ctx.save()
                ctx.scale(size / sw, size / sh)
                ctx.set_source_surface(logo_surface, 0, 0)
                ctx.paint()
                ctx.restore()
                logo_loaded = True
        except Exception:
            pass

    if not logo_loaded:
        # Plain dark background #0e0808
        ctx.set_source_rgb(0.055, 0.031, 0.031)
        ctx.rectangle(0, 0, size, size)
        ctx.fill()

    # Dot: filled circle radius 4 at (18, 18), dark border radius 5
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
    """Generate the 3 state icon PNGs into the temp directory."""
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

        # Build AppIndicator3
        self._ind = AppIndicator3.Indicator.new(
            "maestro",
            _ICON_PATHS.get(self.STATE_DOWN, "dialog-information"),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._ind.set_menu(self._build_menu())

    # ── Menu ───────────────────────────────────────────────────────────────────
    def _build_menu(self) -> Gtk.Menu:
        """Build and return the right-click context menu."""
        self._settings = _load_settings()
        menu = Gtk.Menu()

        item_open = Gtk.MenuItem(label="Open in Browser")
        item_open.connect("activate", lambda _: self._open_browser())
        menu.append(item_open)

        label_restart = "⚠ Restart to apply changes" if self._restart_pending else "Restart Maestro"
        item_restart = Gtk.MenuItem(label=label_restart)
        item_restart.connect("activate", lambda _: self._restart())
        menu.append(item_restart)

        menu.append(Gtk.SeparatorMenuItem())

        autostart_on = self._settings.get("autostart", False)
        item_auto = Gtk.MenuItem(label=f"Autostart with OS: {'ON' if autostart_on else 'OFF'}")
        item_auto.connect("activate", lambda _: self._toggle_autostart())
        menu.append(item_auto)

        menu.append(Gtk.SeparatorMenuItem())

        item_off = Gtk.MenuItem(label="Shut Down Maestro")
        item_off.connect("activate", lambda _: self._shutdown())
        menu.append(item_off)

        menu.show_all()
        return menu

    def _refresh_menu(self):
        """Rebuild and re-attach the menu (called after state changes)."""
        GLib.idle_add(lambda: self._ind.set_menu(self._build_menu()))

    # ── Icon update ────────────────────────────────────────────────────────────
    def _update_icon(self, state: str):
        self._state = state
        icon_path   = _ICON_PATHS.get(state, _ICON_PATHS.get(self.STATE_DOWN))
        GLib.idle_add(self._ind.set_icon_full, icon_path, f"Maestro — {state}")

    # ── Actions ────────────────────────────────────────────────────────────────
    def _open_browser(self):
        try:
            subprocess.Popen(["xdg-open", _get_url()],
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _toggle_autostart(self):
        self._settings = _load_settings()
        new_val = not self._settings.get("autostart", False)
        _set_autostart(new_val)
        self._settings["autostart"] = new_val
        _notify("Maestro", f"Autostart {'enabled' if new_val else 'disabled'}")
        self._refresh_menu()

    def _restart(self):
        self._intentional_stop = False
        self._restart_pending  = False
        self._stop_main()
        time.sleep(0.5)
        self._start_main()
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
        self._settings = _load_settings()
        if not self._settings.get("trayPersistent", True):
            GLib.idle_add(Gtk.main_quit)

    # ── main.py subprocess lifecycle ───────────────────────────────────────────
    def _start_main(self):
        self._settings = _load_settings()
        cmd = [sys.executable, str(MAIN_PY), "--data-dir", str(DATA_DIR)]
        try:
            self._main_proc = subprocess.Popen(cmd, cwd=str(BIN_DIR),
                                               stdin=subprocess.DEVNULL)
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
        rc = proc.returncode
        self._update_icon(self.STATE_DOWN)
        _notify("Maestro", f"Stopped unexpectedly (exit {rc}). Use the tray menu to restart.")

    # ── Health check loop ──────────────────────────────────────────────────────
    def _health_loop(self):
        import urllib.request
        url = _get_url()
        while True:
            if self._intentional_stop:
                time.sleep(5); continue
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
        ("▶ Started",       lambda line: ("Maestro", _extract_name(line) + " started")),
        ("⚠ Crashed",       lambda line: ("Maestro", _extract_name(line) + " crashed")),
        ("↺ Auto-restart",  lambda line: ("Maestro", _extract_name(line) + " restarting…")),
        ("[UPDATE]",        lambda line: ("Maestro", "Update: " + line.split("]", 2)[-1].strip())),
        ("File logging",    lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
        ("Script not found",lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
        ("Script restored", lambda line: ("Maestro", line.split("] ", 1)[-1].strip())),
    ]

    def _sse_loop(self):
        import urllib.request
        url = _get_url()
        while True:
            if self._intentional_stop:
                time.sleep(5); continue
            try:
                req = urllib.request.Request(f"{url}/api/logs/__maestro__")
                with urllib.request.urlopen(req, timeout=None) as resp:
                    buf = b""
                    while True:
                        chunk = resp.read(256)
                        if not chunk: break
                        buf += chunk
                        while b"\n\n" in buf:
                            msg, buf = buf.split(b"\n\n", 1)
                            self._handle_sse_message(msg.decode("utf-8", errors="replace"))
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

    # ── Entry point ────────────────────────────────────────────────────────────
    def run(self):
        self._start_main()

        self._settings = _load_settings()
        if self._settings.get("openBrowserAtLaunch", False):
            def _open_later():
                time.sleep(3.0)
                self._open_browser()
            threading.Thread(target=_open_later, daemon=True).start()

        if self._settings.get("autostart", False) and not AUTOSTART_DESKTOP.exists():
            _write_autostart()

        threading.Thread(target=self._health_loop, daemon=True).start()
        threading.Thread(target=self._sse_loop,    daemon=True).start()

        Gtk.main()


# ── Helpers ────────────────────────────────────────────────────────────────────
def _extract_name(line: str) -> str:
    import re
    m = re.search(r"\[([^\]]+)\]", line)
    if m:
        rest = line[m.end():]
        m2 = re.search(r"\[([^\]]+)\]", rest)
        if m2: return m2.group(1)
        return m.group(1)
    return ""

import urllib.request


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Generate tray icon PNGs before Gtk.main()
    _init_icons()

    # Autostart .desktop consistency
    s = _load_settings()
    if s.get("autostart", False) and not AUTOSTART_DESKTOP.exists():
        _write_autostart()
    elif not s.get("autostart", False) and AUTOSTART_DESKTOP.exists():
        _remove_autostart()

    tray = MaestroTray()
    tray.run()


if __name__ == "__main__":
    main()
