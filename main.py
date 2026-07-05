#!/usr/bin/env python3
"""
Maestro main.py — HTTP server and UI layer (v2.0)
Path: main.py

Responsibilities:
  - Resolve BASE_DIR / DATA_DIR and call daemon.init()
  - Read/write settings.json
  - Serve ui.html, ui.js, webui.js, img/
  - HTTP API routes (delegates all process logic to daemon.py)
  - Single-instance lock (platform temp dir / MaestroWeb.pid)
  - Graceful shutdown on SIGTERM / SIGINT

Process management, SSE, config I/O → daemon.py
"""
import http.server, json, os, queue, shutil, signal
import subprocess, sys, tempfile, threading, time, zipfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import daemon

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent      # bin/ — source files, img/, ui assets
PID_FILE = Path(tempfile.gettempdir()) / "MaestroWeb.pid"

IMG_DIR  = BASE_DIR / "img"
UI_FILE  = BASE_DIR / "ui.html"
UI_JS    = BASE_DIR / "ui.js"
WEBUI_JS = BASE_DIR / "webui.js"

# ── MIME types ─────────────────────────────────────────────────────────────────
_MIME = {
    "svg":  "image/svg+xml",  "png": "image/png",
    "ico":  "image/x-icon",   "jpg": "image/jpeg",
    "jpeg": "image/jpeg",     "gif": "image/gif",
    "webp": "image/webp",
}

_TEXT_EXTS = {
    '.py', '.sh', '.txt', '.md', '.json', '.yaml', '.yml', '.toml', '.cfg',
    '.conf', '.ini', '.js', '.ts', '.html', '.css', '.xml', '.log', '.env',
    '.c', '.cpp', '.h', '.rs', '.go', '.rb', '.php', '.java', '.kt', '',
}

# ── Update Maestro (Settings modal "Update Stable" / "Update Latest") ─────────
# See MAESTRO_GUIDE.md → "Update Maestro" feature section for the full design
# rationale. Summary: main.py only downloads + validates + stages the new
# files into a clean temp directory — it never touches bin/ itself. The
# actual bin/ swap and full-stack restart (including tray.py re-exec, since
# tray.py's own file may have changed) is handled by tray.py, triggered via
# a marker file in DATA_DIR that tray.py checks after main.py exits.
_GITHUB_REPO           = "AIdevelopmentPit/Maestro"
_GITHUB_RELEASES_API   = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_GITHUB_LATEST_ZIP_API = f"https://api.github.com/repos/{_GITHUB_REPO}/zipball/main"
_UPDATE_MARKER_NAME    = "update_request.json"

# Runtime files copied from the downloaded zip into the clean staging dir —
# intentionally mirrors install.sh / install.py's SRC_FILES so an update
# never pulls repo-only files (README.md, install.sh, .github/, etc.) into
# a live bin/.
_UPDATE_SRC_FILES = [
    "tray.py", "main.py", "daemon.py",
    "ui.html", "ui.js", "webui.js",
    "start.sh", "start.py", "start.bat",
]
_UPDATE_SRC_IMG_FILES = [
    "img/MaestroLogo.svg",
    "img/MaestroLogo24x24.png",
]


class _NoRedirectHandler(urllib.request.HTTPErrorProcessor):
    """
    Passes 30x responses straight through instead of following them.
    Needed because GitHub's zipball API redirects (api.github.com →
    codeload.github.com / objects.githubusercontent.com) to a pre-signed,
    unauthenticated URL — and urllib's default redirect handling silently
    drops the Authorization header on any cross-host redirect. Letting the
    redirect through lets us grab the Location header ourselves and fetch
    the actual bytes with NO auth header, which is what the signed URL
    expects anyway.
    """
    def http_response(self, request, response):
        return response
    https_response = http_response


def _github_auth_headers(token: str) -> dict:
    h = {"User-Agent": "Maestro-Updater", "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


def _github_api_get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers=_github_auth_headers(token))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _github_download_zip(url: str, token: str, dest_path: str):
    """
    Download a GitHub zipball URL to dest_path, manually following at most
    one redirect without carrying the Authorization header across it —
    see _NoRedirectHandler docstring for why this can't just be a plain
    urlopen() call when a token is involved.
    """
    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(url, headers=_github_auth_headers(token))
    resp = opener.open(req, timeout=30)
    try:
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise RuntimeError(f"GitHub redirect ({resp.status}) had no Location header")
            with urllib.request.urlopen(location, timeout=60) as final_resp, open(dest_path, "wb") as f:
                shutil.copyfileobj(final_resp, f)
        elif resp.status == 200:
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        else:
            raise RuntimeError(f"Unexpected status {resp.status} downloading update zip")
    finally:
        resp.close()


def _resolve_update_zip_api_url(channel: str, token: str) -> str:
    """Resolve the GitHub zipball API URL for the given update channel."""
    if channel == "latest":
        return _GITHUB_LATEST_ZIP_API
    # stable: ask the GitHub API for the latest tagged Release's zipball URL
    # (itself another api.github.com zipball endpoint — same auth handling).
    data = _github_api_get_json(_GITHUB_RELEASES_API, token)
    url = data.get("zipball_url", "")
    if not url:
        raise RuntimeError("GitHub API did not return a zipball_url for the latest release")
    return url


def _stage_update(channel: str) -> Path:
    """
    Download the requested channel's source zip, extract it, validate it
    contains the expected core files, and copy the known runtime file set
    into a clean staging directory.

    Uses the githubToken setting (if configured) for private-repo access —
    see MAESTRO_GUIDE.md for why the token never leaves the server side
    (main.py / settings.json only; never sent to the browser).

    Never touches BASE_DIR/bin — safe to call before deciding to restart.
    Returns the staging directory path on success.
    Raises RuntimeError (or lets the underlying exception propagate) with a
    human-readable message on any failure; the staging dir is cleaned up
    before raising so no partial state is left behind.
    """
    token = str(_settings.get("githubToken", "")).strip()
    url = _resolve_update_zip_api_url(channel, token)

    tmp_zip_fd, tmp_zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_zip_fd)
    extract_dir = Path(tempfile.mkdtemp(prefix="maestro_update_extract_"))
    staging_dir = Path(tempfile.mkdtemp(prefix="maestro_update_stage_"))

    try:
        _github_download_zip(url, token, tmp_zip_path)

        with zipfile.ZipFile(tmp_zip_path) as zf:
            zf.extractall(extract_dir)

        # GitHub zips contain exactly one top-level folder (repo-branch or repo-sha)
        subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(subdirs) != 1:
            raise RuntimeError(f"Unexpected zip layout ({len(subdirs)} top-level folders)")
        repo_root = subdirs[0]

        # Validate the core files exist before trusting this download at all
        required = ["tray.py", "main.py", "daemon.py", "ui.html", "ui.js", "webui.js"]
        missing = [f for f in required if not (repo_root / f).is_file()]
        if missing:
            raise RuntimeError(f"Downloaded archive is missing: {', '.join(missing)}")

        # Copy only the known runtime file set into the clean staging dir
        (staging_dir / "img").mkdir(parents=True, exist_ok=True)
        for fname in _UPDATE_SRC_FILES:
            src = repo_root / fname
            if src.is_file():
                shutil.copy2(src, staging_dir / fname)
        for fpath in _UPDATE_SRC_IMG_FILES:
            src = repo_root / fpath
            if src.is_file():
                shutil.copy2(src, staging_dir / fpath)
        return staging_dir
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    finally:
        try: os.unlink(tmp_zip_path)
        except Exception: pass
        shutil.rmtree(extract_dir, ignore_errors=True)

# ── Settings ───────────────────────────────────────────────────────────────────
_DEFAULT_SETTINGS: dict = {
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

# Populated by _load_settings() in main() — do not access before that point.
_settings:  dict = {}
_DATA_DIR:  Path = None  # set in main() before daemon.init()


def _resolve_data_dir() -> Path:
    """
    Resolve data directory using priority order:
      1. --data-dir CLI argument  (tray.py passes this)
      2. MAESTRO_DATA environment variable
      3. dataDir field in settings.json at the default location
      4. Default: BASE_DIR.parent / "data"
    """
    # Priority 1: CLI arg from tray.py
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--data-dir" and i + 1 < len(args):
            return Path(args[i + 1]).resolve()

    # Priority 2: environment variable
    env = os.environ.get("MAESTRO_DATA", "").strip()
    if env:
        return Path(env).resolve()

    # Priority 3: dataDir stored in settings.json at the default location
    default       = (BASE_DIR.parent / "data").resolve()
    settings_probe = default / "settings.json"
    if settings_probe.exists():
        try:
            dd = json.loads(settings_probe.read_text()).get("dataDir", "").strip()
            if dd:
                return Path(dd).resolve()
        except Exception:
            pass

    # Priority 4: default
    return default


def _load_settings(data_dir: Path) -> dict:
    """
    Read settings.json from data_dir.
    Creates it with _DEFAULT_SETTINGS on the first run.
    Any missing key is backfilled from defaults (forward-compat).
    """
    path = data_dir / "settings.json"
    s    = dict(_DEFAULT_SETTINGS)
    if path.exists():
        try:
            on_disk = json.loads(path.read_text())
            # Only accept known keys; ignore stale/unknown ones
            s.update({k: v for k, v in on_disk.items() if k in _DEFAULT_SETTINGS})
        except Exception:
            pass
    else:
        # First run — persist defaults so tray.py and the user can inspect them
        _write_settings(data_dir, s)
    return s


def _write_settings(data_dir: Path, s: dict):
    """Atomic write of settings dict to data_dir/settings.json."""
    path = data_dir / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(data_dir), suffix=".json", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
            f.write("\n")
        shutil.move(tmp, str(path))
    except Exception as ex:
        daemon._mlog(f"_write_settings error: {ex}")


# ── Server state ───────────────────────────────────────────────────────────────
_server = None


# ── Single instance ────────────────────────────────────────────────────────────
def _check_single() -> bool:
    if PID_FILE.exists():
        try:
            ex = int(PID_FILE.read_text().strip())
            os.kill(ex, 0)
            print(f"[maestro] already running (PID {ex})", file=sys.stderr)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    PID_FILE.write_text(str(os.getpid()))
    return True


def _cleanup():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── HTTP Handler ───────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass   # suppress access log

    # ── Routing ────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/"):
            self._serve_file(UI_FILE, "text/html")
        elif path == "/ui.js":
            self._serve_file(UI_JS,    "application/javascript")
        elif path == "/webui.js":
            self._serve_file(WEBUI_JS, "application/javascript")
        elif path == "/api/state":
            self._api_state()
        elif path.startswith("/api/logs/"):
            self._api_logs(path[len("/api/logs/"):])
        elif path == "/api/browse":
            self._browse()
        elif path == "/api/read-file":
            self._read_file()
        elif path == "/api/settings":
            self._json(_settings)
        elif path.startswith("/img/"):
            fn  = Path(path[len("/img/"):]).name
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            self._serve_file(IMG_DIR / fn, _MIME.get(ext, "application/octet-stream"))
        else:
            self._err(404, "Not found")

    def do_POST(self):
        body  = self._body()
        route = self.path.rstrip("/")
        fn = {
            "/api/launch":         self._launch,
            "/api/kill":           self._kill,
            "/api/toggle":         self._toggle,
            "/api/add":            self._add,
            "/api/remove":         self._remove,
            "/api/reorder":        self._reorder,
            "/api/add-sep":        self._add_sep,
            "/api/update-entry":   self._update_entry,
            "/api/update-script":  self._update_script,
            "/api/clear-log":      self._clear_log,
            "/api/write-file":     self._write_file,
            "/api/open-editor":    self._open_editor,
            "/api/open-app":       self._open_app,
            "/api/toggle-logging": self._toggle_logging,
            "/api/settings":       self._post_settings,
            "/api/shutdown":       self._shutdown,
            "/api/update-maestro": self._update_maestro,
        }.get(route)
        if fn:
            fn(body)
        else:
            self._err(404, "Not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── Low-level helpers ──────────────────────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _body(self) -> dict:
        cl = int(self.headers.get("Content-Length", 0))
        if cl:
            try:    return json.loads(self.rfile.read(cl))
            except: return {}
        return {}

    def _json(self, data, code: int = 200):
        raw = json.dumps(data).encode()
        try:
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _err(self, code: int, msg: str):
        self._json({"error": msg}, code)

    def _serve_file(self, path: Path, ctype: str):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._err(404, f"{path.name} not found")
            return
        try:
            self.send_response(200)
            self._cors()
            self.send_header(
                "Content-Type",
                f"{ctype}; charset=utf-8" if "text" in ctype else ctype,
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ── GET handlers ───────────────────────────────────────────────────────────
    def _api_state(self):
        with daemon._lock:
            data = [e.to_dict(i) for i, e in enumerate(daemon.entries)]
        ram_used, ram_total = daemon._read_sys_ram()
        self._json({
            "entries":        data,
            "loggingEnabled": daemon._logging_enabled,
            "gpu":            daemon._read_gpu(),
            "sysCpu":         daemon._sys_cpu,
            "sysRam":         ram_used,
            "sysRamTotal":    ram_total,
        })

    def _api_logs(self, uid: str):
        if uid == daemon.MAESTRO_UID:
            self._sse_stream(
                daemon._self_subs_lock, daemon._self_buf, daemon._self_subs
            )
            return
        with daemon._lock:
            _, e = daemon._find_by_uid(uid)
        if e is None:
            self._err(404, "not found")
            return
        self._sse_stream(e.subs_lock, e.log_buf, e.subs)

    def _sse_stream(self, lock, buf_deque, subs_list):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type",      "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control",     "no-cache")
        self.send_header("Connection",        "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = queue.Queue(maxsize=500)
        with lock:
            buf = list(buf_deque)
            subs_list.append(q)

        def sse(ev: str, data: str):
            self.wfile.write(f"event: {ev}\ndata: {data}\n\n".encode())
            self.wfile.flush()

        try:
            for line in buf:
                sse("log", line)
            while not daemon._stop.is_set():
                try:
                    item = q.get(timeout=5.0)
                except queue.Empty:
                    self.wfile.write(b": k\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                sse(*item)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with lock:
                try:    subs_list.remove(q)
                except ValueError: pass

    def _browse(self):
        qs        = parse_qs(urlparse(self.path).query)
        raw       = qs.get("path", [str(Path.home())])[0]
        all_files = qs.get("all", ["0"])[0] == "1"
        only_md   = qs.get("md",  ["0"])[0] == "1"
        p = Path(raw).resolve()
        if not p.is_dir():
            self._json({"error": "not a directory"})
            return
        try:
            dirs, files = [], []
            for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                if item.name.startswith("."): continue
                if item.is_dir():
                    dirs.append(item.name)
                elif item.is_file():
                    if all_files:
                        files.append(item.name)
                    elif only_md and item.suffix == ".md":
                        files.append(item.name)
                    elif item.suffix == ".sh":
                        files.append(item.name)
            self._json({
                "path":   str(p),
                "parent": str(p.parent) if str(p) != str(p.parent) else None,
                "dirs":   dirs,
                "files":  files,
            })
        except PermissionError:
            self._json({"error": "permission denied"})

    def _read_file(self):
        qs  = parse_qs(urlparse(self.path).query)
        raw = qs.get("path", [""])[0]
        p   = Path(raw).resolve()
        if not p.is_file():
            self._json({"error": "not a file"})
            return
        if p.stat().st_size > 512 * 1024:
            self._json({"error": "file too large (>512KB)"})
            return
        ext = p.suffix.lower()
        if ext not in _TEXT_EXTS and ext:
            self._json({"error": "binary file"})
            return
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            self._json({"path": str(p), "name": p.name, "content": content, "ext": ext})
        except PermissionError:
            self._json({"error": "permission denied"})

    # ── POST handlers ──────────────────────────────────────────────────────────
    def _launch(self, b: dict):
        uid = b.get("uid")
        if uid:
            with daemon._lock:
                idx, _ = daemon._find_by_uid(uid)
            if idx is None:
                self._err(404, "not found")
                return
        else:
            idx = b.get("idx")
            if idx is None:
                self._err(400, "missing idx/uid")
                return
        threading.Thread(target=daemon.launch, args=(idx,), daemon=True).start()
        self._json({"ok": True})

    def _kill(self, b: dict):
        uid = b.get("uid")
        if uid:
            with daemon._lock:
                idx, _ = daemon._find_by_uid(uid)
            if idx is None:
                self._err(404, "not found")
                return
            daemon.kill_entry(idx)
            self._json({"ok": True})
            return
        idx = b.get("idx")
        if idx is None:
            self._err(400, "missing idx")
            return
        daemon.kill_entry(idx)
        self._json({"ok": True})

    def _toggle(self, b: dict):
        idx = b.get("idx")
        if idx is None:
            self._err(400, "missing idx")
            return
        with daemon._lock:
            if idx >= len(daemon.entries):
                self._err(404, "not found")
                return
            e = daemon.entries[idx]
            if e.status != "SEP":
                e.status = "OFF" if e.status == "ON" else "ON"
                daemon.save_config()
        self._json({"ok": True})

    def _add(self, b: dict):
        path = b.get("path", "").strip()
        if not path:
            self._err(400, "missing path")
            return
        if not os.path.isfile(path):
            self._err(400, f"file not found: {path}")
            return
        e = daemon.ProcessEntry(
            "ON", path,
            name          = b.get("name",          "").strip(),
            launch_hidden = bool(b.get("launchHidden", False)),
            webapps       = b.get("webapps",        []) or [],
            update_script = b.get("updateScript",   "").strip(),
            info_file     = b.get("infoFile",        "").strip(),
        )
        with daemon._lock:
            daemon.entries.append(e)
            daemon.save_config()
        self._json({"ok": True})

    def _remove(self, b: dict):
        idx = b.get("idx")
        if idx is None:
            self._err(400, "missing idx")
            return
        with daemon._lock:
            if idx >= len(daemon.entries):
                self._err(404, "not found")
                return
            e = daemon.entries[idx]
            e.manually_killed = True
            daemon._kill_proc(e)
            daemon._kill_ext(e)
            e.pid = None
            daemon.entries.pop(idx)
            daemon._close_subs(e)
            daemon.save_config()
        self._json({"ok": True})

    def _reorder(self, b: dict):
        a, bv = b.get("a"), b.get("b")
        if a is None or bv is None:
            self._err(400, "missing a/b")
            return
        with daemon._lock:
            if max(a, bv) >= len(daemon.entries):
                self._err(404, "out of range")
                return
            daemon.entries[a], daemon.entries[bv] = daemon.entries[bv], daemon.entries[a]
            daemon.save_config()
        self._json({"ok": True})

    def _add_sep(self, b: dict):
        name  = (b.get("name") or "Separator").strip() or "Separator"
        after = b.get("after", len(daemon.entries))
        sf    = b.get("sepFile", "").strip()
        with daemon._lock:
            pos = min(max(0, int(after) + 1), len(daemon.entries))
            daemon.entries.insert(pos, daemon.ProcessEntry("SEP", name, sep_file=sf))
            daemon.save_config()
        self._json({"ok": True})

    def _update_entry(self, b: dict):
        idx = b.get("idx")
        if idx is None:
            self._err(400, "missing idx")
            return
        with daemon._lock:
            if idx >= len(daemon.entries):
                self._err(404, "not found")
                return
            e = daemon.entries[idx]
            if e.status == "SEP":
                if "label"   in b: e.path     = b["label"].strip() or e.path
                if "name"    in b: e.name     = b["name"].strip()
                if "sepFile" in b: e.sep_file = b["sepFile"].strip()
            else:
                if "name"         in b: e.name          = b["name"].strip()
                if "delay"        in b: e.delay          = max(0, int(b["delay"]))
                if "autoRestart"  in b: e.auto_restart   = bool(b["autoRestart"])
                if "keepOpen"     in b: e.keep_open      = bool(b["keepOpen"])
                if "launchHidden" in b: e.launch_hidden  = bool(b["launchHidden"])
                if "webapps"      in b: e.webapps        = b["webapps"] or []
                if "updateScript" in b: e.update_script  = b["updateScript"].strip()
                if "infoFile"     in b: e.info_file      = b["infoFile"].strip()
                if "openOutside"  in b: e.open_outside   = bool(b["openOutside"])
                if "termColor"    in b and b["termColor"] in daemon.VALID_COLORS:
                    e.term_color = b["termColor"]
                if "status" in b and b["status"] in ("ON", "OFF"):
                    e.status = b["status"]
            daemon.save_config()
        self._json({"ok": True})

    def _update_script(self, b: dict):
        uid = b.get("uid", "")
        with daemon._lock:
            idx, e = daemon._find_by_uid(uid)
        if e is None or e.status == "SEP":
            self._err(404, "not found")
            return
        if e.update_script and Path(e.update_script).exists():
            up = Path(e.update_script)
        else:
            up = Path(e.path).parent / "update.sh"
        if not up.exists():
            self._json({"ok": False, "error": f"Not found: {up}"})
            return
        daemon._append_log(e, f"[UPDATE] ↗ Launching update in external terminal: {up}")
        proc = daemon._run_in_external_terminal(e, str(up), "Update")
        if proc:
            threading.Thread(
                target=daemon._monitor_update_ext, args=(e, proc), daemon=True
            ).start()
        self._json({"ok": bool(proc)})

    def _clear_log(self, b: dict):
        uid = b.get("uid", "")
        if uid == daemon.MAESTRO_UID:
            daemon._self_buf.clear()
            self._json({"ok": True})
            return
        with daemon._lock:
            _, e = daemon._find_by_uid(uid)
        if e is None:
            self._err(404, "not found")
            return
        with e.subs_lock:
            e.log_buf.clear()
        self._json({"ok": True})

    def _write_file(self, b: dict):
        path    = b.get("path", "").strip()
        content = b.get("content", "")
        if not path:
            self._err(400, "missing path")
            return
        try:
            Path(path).write_text(content, encoding="utf-8")
            self._json({"ok": True})
        except Exception as ex:
            self._json({"ok": False, "error": str(ex)})

    def _open_editor(self, b: dict):
        path = b.get("path", "").strip()
        if not path:
            self._err(400, "missing path")
            return
        for ed in ["gnome-text-editor", "gedit", "mousepad", "xed", "kate"]:
            try:
                subprocess.Popen(
                    [ed, path], start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._json({"ok": True, "editor": ed})
                return
            except FileNotFoundError:
                continue
        self._json({"ok": False, "error": "No text editor found"})

    def _open_app(self, b: dict):
        app  = b.get("app", "").strip()
        path = b.get("path", "").strip()
        if not app:
            self._err(400, "missing app")
            return
        try:
            cmd = [app]
            if path:
                cmd.append(path)
            subprocess.Popen(
                cmd, start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._json({"ok": True})
        except FileNotFoundError:
            self._json({"ok": False, "error": f"{app} not found"})
        except Exception as ex:
            self._json({"ok": False, "error": str(ex)})

    def _toggle_logging(self, _b: dict):
        """
        Legacy endpoint — kept for UI backward compat during v1.4→v2.0 transition.
        Delegates to daemon.toggle_logging() and syncs the result to settings.json.
        The v2.0 settings modal will call POST /api/settings instead.
        """
        new_state = daemon.toggle_logging()
        _settings["logsRecorder"] = new_state
        _write_settings(_DATA_DIR, _settings)
        self._json({"ok": True, "enabled": new_state})

    def _post_settings(self, b: dict):
        """
        Update one or more settings keys and persist to settings.json.
        Applies immediate in-process side effects where relevant:
          logsRecorder  → daemon._logging_enabled
          autostart     → tray.py handles .desktop file (just persisted here)
          dataDir       → migration handled by tray.py   (just persisted here)
          host / port   → require restart to take effect  (just persisted here)
        """
        changed = False
        for key, val in b.items():
            if key not in _DEFAULT_SETTINGS:
                continue
            _settings[key] = val
            changed = True
            if key == "logsRecorder":
                daemon._logging_enabled = bool(val)
        if changed:
            _write_settings(_DATA_DIR, _settings)
        self._json({"ok": True, "settings": _settings})

    def _shutdown(self, _b: dict):
        self._json({"ok": True})

        def _do():
            time.sleep(0.2)
            daemon.kill_all()
            daemon._stop.set()
            with daemon._lock:
                for e in daemon.entries:
                    daemon._close_subs(e)
            with daemon._self_subs_lock:
                for q in daemon._self_subs:
                    try: q.put_nowait(None)
                    except: pass
            if _server:
                threading.Thread(target=_server.shutdown, daemon=True).start()

        threading.Thread(target=_do, daemon=True).start()

    def _update_maestro(self, b: dict):
        """
        Download + stage a Maestro update from the requested channel
        ("stable" = latest tagged Release, "latest" = main branch HEAD).

        Responds only AFTER the download + extraction + validation have
        all succeeded — a failure (bad network, GitHub rate limit,
        malformed archive) is reported directly to the caller with an
        error message, instead of triggering a restart that could never
        recover. Only on success is the marker file written and the
        graceful full-stack shutdown triggered; tray.py picks up the
        marker after main.py exits and performs the actual bin/ swap +
        restart (see tray.py's _check_and_apply_update / _perform_update).

        bin/ itself is never touched by this handler — only DATA_DIR
        (the marker file) and temp directories are written here.
        """
        channel = str(b.get("channel", "")).strip()
        if channel not in ("stable", "latest"):
            self._err(400, "channel must be 'stable' or 'latest'")
            return

        try:
            staging_dir = _stage_update(channel)
        except Exception as ex:
            daemon._mlog(f"✕ Update staging failed ({channel}): {ex}")
            self._json({"ok": False, "error": str(ex)})
            return

        marker = {
            "channel":     channel,
            "stagingDir":  str(staging_dir),
            "requestedAt": time.time(),
        }
        try:
            (_DATA_DIR / _UPDATE_MARKER_NAME).write_text(json.dumps(marker, indent=2))
        except Exception as ex:
            shutil.rmtree(staging_dir, ignore_errors=True)
            daemon._mlog(f"✕ Could not write update marker: {ex}")
            self._json({"ok": False, "error": f"Could not write update marker: {ex}"})
            return

        daemon._mlog(f"✓ Update staged ({channel}) — restarting to apply…")
        self._json({"ok": True, "message": "Update staged — Maestro is restarting to apply it."})

        def _do():
            time.sleep(0.3)
            daemon.kill_all()
            daemon._stop.set()
            with daemon._lock:
                for e in daemon.entries:
                    daemon._close_subs(e)
            with daemon._self_subs_lock:
                for q in daemon._self_subs:
                    try: q.put_nowait(None)
                    except: pass
            if _server:
                threading.Thread(target=_server.shutdown, daemon=True).start()

        threading.Thread(target=_do, daemon=True).start()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    import atexit
    global _server, _settings, _DATA_DIR

    # ── 1. Resolve data directory ──────────────────────────────────────────────
    _DATA_DIR = _resolve_data_dir()
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 2. Initialize daemon (must happen before any daemon call) ──────────────
    daemon.init(BASE_DIR, _DATA_DIR)
    daemon._install_excepthook()

    # ── 3. Load settings (creates settings.json with defaults on first run) ────
    _settings = _load_settings(_DATA_DIR)

    # ── 4. Apply settings to daemon state ─────────────────────────────────────
    daemon._logging_enabled = _settings.get("logsRecorder", False)

    # ── 5. Read server bind parameters from settings ───────────────────────────
    host = _settings.get("host", "127.0.0.1")
    port = int(_settings.get("port", 8865))

    # ── 6. Single-instance lock ────────────────────────────────────────────────
    if not _check_single():
        sys.exit(1)

    daemon._mlog(f"Starting — PID {os.getpid()}")
    daemon._mlog(f"BASE_DIR : {BASE_DIR}")
    daemon._mlog(f"DATA_DIR : {_DATA_DIR}")

    # ── 7. Load config and reconnect surviving PIDs ────────────────────────────
    daemon.load_config()
    daemon._mlog(f"Config loaded — {len(daemon.entries)} entries")

    for e in daemon.entries:
        if e.pid is not None and e.status != "SEP":
            threading.Thread(
                target=daemon._watch_existing_pid, args=(e,), daemon=True
            ).start()

    # ── 8. Start background threads ────────────────────────────────────────────
    atexit.register(daemon.kill_all)
    threading.Thread(target=daemon._metrics_loop, daemon=True).start()

    # ── 9. Start HTTP server ───────────────────────────────────────────────────
    _server = http.server.ThreadingHTTPServer((host, port), Handler)
    _server.daemon_threads = True

    def _sig(sig, frame):
        daemon._mlog("Signal — shutting down")
        daemon.kill_all()
        daemon._stop.set()
        threading.Thread(target=_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT,  _sig)

    daemon._mlog(f"Listening on http://{host}:{port}")
    print(f"[maestro] http://{host}:{port}", file=sys.stderr)

    try:
        _server.serve_forever()
    except Exception as ex:
        daemon._mlog(f"Server error: {ex}")
    finally:
        daemon.kill_all()
        _cleanup()
        daemon._mlog("Stopped.")


if __name__ == "__main__":
    main()
