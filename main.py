#!/usr/bin/env python3
"""
Maestro main.py — HTTP server and UI layer
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
import subprocess, sys, tempfile, threading, time, zipfile, re, base64
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

import daemon

# ── Security: internal-call marker header (see Fix #1) ────────────────────────
# tray.py's own urllib-based calls into main.py's API set this header so
# _origin_ok() can trust them even though they carry no Origin/Referer (they
# aren't a browser). Safe specifically because no Access-Control-Allow-Origin
# header is ever sent back (see Handler._cors) — a real cross-origin browser
# fetch attempting to attach a custom header triggers a CORS preflight, which
# is refused with no ACAO header present, so the real request never sends.
_INTERNAL_HEADER = "X-Maestro-Internal"

# ── Security: settings mutation lock (Fix #15) ─────────────────────────────────
_settings_lock = threading.Lock()

# ── Security: single-update-at-a-time lock (Fix #20) ───────────────────────────
_update_lock = threading.Lock()

# ── Security: sensitive filenames never served via /api/read-file (Fix #4) ────
_SENSITIVE_NAMES = {
    '.netrc', '.npmrc', '.pgpass', '.bash_history', '.zsh_history',
    '.python_history', '.git-credentials', 'known_hosts', 'authorized_keys',
    'shadow', 'passwd',
}

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

# ── Update Maestro (Settings modal "Update Latest" / "Update Pre-release") ────
# Versioning convention (must stay in sync with .github/workflows/release.yml):
#   vX.Y     (two numeric parts)   -> tagged as a normal GitHub Release (not
#                                     prerelease). GitHub's own "Latest" flag
#                                     always lands on the newest one of these.
#   vX.Y.Z   (three numeric parts) -> tagged as a GitHub prerelease. Never
#                                     eligible for the "Latest" flag, however
#                                     recent.
# "CHECK LATEST" only ever considers vX.Y tags (via GitHub's own
# /releases/latest, which by definition excludes anything flagged
# prerelease). "CHECK PRE-RELEASE" only ever considers vX.Y.Z tags (the most
# recently published release explicitly flagged prerelease:true) — it no
# longer looks at the main branch HEAD at all, so an untagged commit on main
# is invisible to both channels until it's cut as a vX.Y.Z (or vX.Y) tag.
_GITHUB_REPO         = "pellaDev/Maestro"
_GITHUB_RELEASES_API = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_GITHUB_ALL_RELEASES_API = f"https://api.github.com/repos/{_GITHUB_REPO}/releases?per_page=100"
_UPDATE_MARKER_NAME  = "update_request.json"

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
    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(url, headers=_github_auth_headers(token))
    resp = opener.open(req, timeout=30)
    try:
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise RuntimeError(f"GitHub redirect ({resp.status}) had no Location header")
            # Fix #10: only follow redirects to hosts GitHub actually uses for
            # zipball downloads — refuse anything else outright.
            rhost = (urlparse(location).hostname or "")
            if not (rhost == "codeload.github.com" or rhost.endswith(".githubusercontent.com")):
                raise RuntimeError(f"Refusing to follow untrusted redirect to: {location}")
            with urllib.request.urlopen(location, timeout=60) as final_resp, open(dest_path, "wb") as f:
                shutil.copyfileobj(final_resp, f)
        elif resp.status == 200:
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        else:
            raise RuntimeError(f"Unexpected status {resp.status} downloading update zip")
    finally:
        resp.close()


def _github_resolve_channel_release(channel: str, token: str) -> dict:
    """
    Return the GitHub release JSON (tag_name, zipball_url, ...) for the
    requested channel:
      - "release" (Latest)      -> GitHub's own /releases/latest — this
        endpoint is defined by GitHub itself to exclude anything flagged
        prerelease, so it always resolves to the newest vX.Y tag.
      - "main" (Pre-release)    -> the most recently published release that
        is explicitly flagged prerelease:true (a vX.Y.Z tag, per the
        tagging convention enforced by release.yml). Raises if none exists.
    """
    if channel == "release":
        return _github_api_get_json(_GITHUB_RELEASES_API, token)

    # Pre-release: /releases (list) is already sorted newest-first by
    # creation date — take the first one flagged prerelease:true.
    releases = _github_api_get_json(_GITHUB_ALL_RELEASES_API, token)
    if not isinstance(releases, list):
        raise RuntimeError("GitHub API did not return a release list")
    for rel in releases:
        if rel.get("prerelease"):
            return rel
    raise RuntimeError(
        "No pre-release (vX.Y.Z) tag found on GitHub — cut one with "
        "'git tag vX.Y.Z && git push origin vX.Y.Z' first."
    )


def _resolve_update_zip_api_url(channel: str = None, token: str = "", tag: str = None) -> str:
    """Return the GitHub zipball URL for a channel ("release"/"main") or,
    when an explicit tag is given (any installable version — including
    rollback to an older one), for that exact release."""
    if tag:
        releases = _github_api_get_json(_GITHUB_ALL_RELEASES_API, token)
        if not isinstance(releases, list):
            raise RuntimeError("GitHub API did not return a release list")
        for rel in releases:
            if (rel.get("tag_name") or "").strip() == tag.strip():
                url = rel.get("zipball_url", "")
                if not url:
                    raise RuntimeError(f"GitHub API did not return a zipball_url for tag '{tag}'")
                return url
        raise RuntimeError(f"Tag '{tag}' not found among the releases on GitHub")
    rel = _github_resolve_channel_release(channel, token)
    url = rel.get("zipball_url", "")
    if not url:
        raise RuntimeError(f"GitHub API did not return a zipball_url for channel '{channel}'")
    return url


def _stage_update(channel: str = None, tag: str = None, token: str = None) -> Path:
    """Download + validate the requested release into a temp staging dir.
    `token` defaults to the githubToken from settings (main.py context);
    tray.py passes it explicitly since its own settings copy is separate."""
    if token is None:
        token = str(_settings.get("githubToken", "")).strip()
    url = _resolve_update_zip_api_url(channel, token, tag=tag)

    tmp_zip_fd, tmp_zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_zip_fd)
    extract_dir = Path(tempfile.mkdtemp(prefix="maestro_update_extract_"))
    staging_dir = Path(tempfile.mkdtemp(prefix="maestro_update_stage_"))

    try:
        _github_download_zip(url, token, tmp_zip_path)

        with zipfile.ZipFile(tmp_zip_path) as zf:
            # Fix #9: defense in depth for a very-high-blast-radius operation
            # (the extracted content becomes the next bin/) — reject any
            # member that would escape extract_dir, and reject symlink
            # entries outright (a zip-slip check alone doesn't stop a member
            # from *being* a symlink that then gets followed on copy).
            extract_root = extract_dir.resolve()
            for info in zf.infolist():
                member_path = (extract_root / info.filename).resolve()
                if not str(member_path).startswith(str(extract_root)):
                    raise RuntimeError(f"Zip contains an unsafe path: {info.filename}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError(f"Zip contains a symlink, refusing: {info.filename}")
            zf.extractall(extract_dir)

        subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(subdirs) != 1:
            raise RuntimeError(f"Unexpected zip layout ({len(subdirs)} top-level folders)")
        repo_root = subdirs[0]

        required = ["tray.py", "main.py", "daemon.py", "ui.html", "ui.js", "webui.js"]
        missing = [f for f in required if not (repo_root / f).is_file()]
        if missing:
            raise RuntimeError(f"Downloaded archive is missing: {', '.join(missing)}")

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


_VERSION_BADGE_RE = re.compile(r'class="ver">v([0-9][0-9A-Za-z.\-]*)')


def _read_local_badge_version() -> str:
    try:
        html = UI_FILE.read_text(encoding="utf-8", errors="replace")
        m = _VERSION_BADGE_RE.search(html)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _fetch_remote_badge_version(ref: str, token: str) -> str:
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/ui.html?ref={quote(ref)}"
    data = _github_api_get_json(url, token)
    content_b64 = data.get("content", "")
    if not content_b64:
        raise RuntimeError("GitHub API did not return ui.html's content")
    html = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    m = _VERSION_BADGE_RE.search(html)
    if not m:
        raise RuntimeError("Could not find a version badge in the remote ui.html")
    return m.group(1)


def _version_tuple(v: str) -> tuple:
    """Parse 'X.Y[.Z]' into a zero-padded 3-tuple for numeric comparison."""
    parts = []
    for p in v.strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _cmp_versions(a: str, b: str) -> int:
    """Compare dotted numeric versions: -1 if a<b, 0 if equal, 1 if a>b."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    return (ta > tb) - (ta < tb)


# ── Settings ───────────────────────────────────────────────────────────────────
_DEFAULT_SETTINGS: dict = {
    "logsRecorder":        False,
    "openBrowserAtLaunch": False,
    "autostart":           False,
    "trayPersistent":      True,
    "systemNotifications": True,
    "autoUpdate":          False,
    "tabKeepalive":        True,
    "screenWakeLock":      True,
    "webAppAutoSuspendMin": 10,
    "host":                "127.0.0.1",
    "port":                8865,
    "dataDir":             "",
    "githubToken":         "",
    "terminal":            "",
}

_settings:  dict = {}
_DATA_DIR:  Path = None


def _resolve_data_dir() -> Path:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--data-dir" and i + 1 < len(args):
            return Path(args[i + 1]).resolve()

    env = os.environ.get("MAESTRO_DATA", "").strip()
    if env:
        return Path(env).resolve()

    default       = (BASE_DIR.parent / "data").resolve()
    settings_probe = default / "settings.json"
    if settings_probe.exists():
        try:
            dd = json.loads(settings_probe.read_text()).get("dataDir", "").strip()
            if dd:
                return Path(dd).resolve()
        except Exception:
            pass

    return default


def _load_settings(data_dir: Path) -> dict:
    path = data_dir / "settings.json"
    s    = dict(_DEFAULT_SETTINGS)
    if path.exists():
        try:
            on_disk = json.loads(path.read_text())
            s.update({k: v for k, v in on_disk.items() if k in _DEFAULT_SETTINGS})
        except Exception:
            pass
    else:
        _write_settings(data_dir, s)
    return s


def _write_settings(data_dir: Path, s: dict):
    path = data_dir / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(data_dir), suffix=".json", text=True)
        # Fix #13: settings.json holds the GitHub token — make the restrictive
        # mode explicit rather than incidental, so it's correct regardless of
        # umask. Windows has no POSIX chmod bits; skip there.
        if not sys.platform.startswith("win"):
            try: os.chmod(tmp, 0o600)
            except OSError: pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
            f.write("\n")
        shutil.move(tmp, str(path))
    except Exception as ex:
        daemon._mlog(f"_write_settings error: {ex}")


# ── Server state ───────────────────────────────────────────────────────────────
_server = None


# ── Single instance ────────────────────────────────────────────────────────────
def _is_maestro_process(pid: int) -> bool:
    """
    Fix #16: verify a live PID actually belongs to a Maestro main.py process
    before trusting it — otherwise a stale PID from an unclean shutdown that
    got recycled by an unrelated OS process would make Maestro wrongly refuse
    to start. Linux-only check (via /proc); on Windows or if /proc is
    unavailable, fall back to the old trust-any-live-PID behavior.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        return b"main.py" in cmdline
    except Exception:
        return True  # can't verify — keep previous (safe-by-default) behavior


def _check_single() -> bool:
    if PID_FILE.exists():
        try:
            ex = int(PID_FILE.read_text().strip())
            os.kill(ex, 0)
            if _is_maestro_process(ex):
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


# ── Security helper: safe integer index parsing (Fix #7) ───────────────────────
def _as_index(v):
    """Parse a request-supplied index safely — never raises, never accepts bool."""
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── Native file / folder dialog (POST /api/pick-path) ──────────────────────────
# A browser page can never read the real path of a file chosen with <input type=file>,
# and Maestro needs real paths (it runs the files). So the server — which runs on the
# user's own desktop session — opens the operating system's own dialog and returns
# the chosen path. Linux tries zenity, kdialog, a GTK dialog and Tk, in that order;
# Windows uses the .NET dialogs through PowerShell (Windows path not tested yet).
_IS_WIN    = sys.platform == "win32"
_pick_lock = threading.Lock()          # one dialog at a time

_PICK_FILTERS = {
    "launchable": sorted(daemon.LAUNCHABLE_EXTS),
    "sh":         [".sh", ".bash"],
    "md":         [".md"],
}

_GTK_PICK_SCRIPT = r"""
import sys, json
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except Exception:
    sys.exit(3)
if not Gtk.init_check()[0]:
    sys.exit(4)
kind, title, start, patterns = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
action = Gtk.FileChooserAction.SELECT_FOLDER if kind == "folder" else Gtk.FileChooserAction.OPEN
dlg = Gtk.FileChooserDialog(title=title, action=action)
dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
if kind == "file" and patterns:
    f = Gtk.FileFilter(); f.set_name("Supported files")
    for p in patterns: f.add_pattern(p)
    dlg.add_filter(f)
    a = Gtk.FileFilter(); a.set_name("All files"); a.add_pattern("*")
    dlg.add_filter(a)
if start:
    dlg.set_current_folder(start)
dlg.set_keep_above(True)
rc = dlg.run()
if rc == Gtk.ResponseType.OK:
    print(dlg.get_filename())
sys.exit(0 if rc == Gtk.ResponseType.OK else 1)
"""

_TK_PICK_SCRIPT = r"""
import sys, json
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    sys.exit(3)
kind, title, start, patterns = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
try:
    root = tkinter.Tk()
except Exception:
    sys.exit(4)
root.withdraw()
root.attributes("-topmost", True)
if kind == "folder":
    path = filedialog.askdirectory(title=title, initialdir=start or None)
else:
    types = []
    if patterns:
        types.append(("Supported files", " ".join(patterns)))
    types.append(("All files", "*"))
    path = filedialog.askopenfilename(title=title, initialdir=start or None, filetypes=types)
if path:
    print(path)
    sys.exit(0)
sys.exit(1)
"""

_PS_PICK_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
if ($env:MAESTRO_PICK_KIND -eq 'folder') {
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description = $env:MAESTRO_PICK_TITLE
  if ($env:MAESTRO_PICK_START) { $d.SelectedPath = $env:MAESTRO_PICK_START }
} else {
  $d = New-Object System.Windows.Forms.OpenFileDialog
  $d.Title = $env:MAESTRO_PICK_TITLE
  if ($env:MAESTRO_PICK_START) { $d.InitialDirectory = $env:MAESTRO_PICK_START }
  if ($env:MAESTRO_PICK_FILTER) { $d.Filter = $env:MAESTRO_PICK_FILTER }
}
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
  if ($env:MAESTRO_PICK_KIND -eq 'folder') { [Console]::Out.Write($d.SelectedPath) } else { [Console]::Out.Write($d.FileName) }
  exit 0
}
exit 1
"""


def _pick_start_dir(start: str) -> str:
    """Directory the dialog opens in: the given path (or its parent), else the home directory."""
    try:
        p = Path(start).expanduser() if start else Path.home()
        if p.is_file():
            p = p.parent
        return str(p) if p.is_dir() else str(Path.home())
    except Exception:
        return str(Path.home())


def _glob_patterns(exts: list) -> list:
    """['.appimage'] -> ['*.appimage', '*.APPIMAGE', '*.Appimage', '*.AppImage'] (dialog globs are case-sensitive)."""
    out = []
    for e in exts:
        e = e.lower()
        variants = [e, e.upper(), "." + e[1:].capitalize()]
        if e == ".appimage":
            variants.append(".AppImage")
        for v in variants:
            if "*" + v not in out:
                out.append("*" + v)
    return out


def _pick_candidates(kind: str, title: str, exts: list, start: str):
    """Ordered list of (backend name, argv, extra Popen kwargs) to try."""
    patterns = _glob_patterns(exts) if kind == "file" else []
    if _IS_WIN:
        flt = ""
        if patterns:
            flt = "Supported files|" + ";".join(patterns) + "|All files|*.*"
        env = dict(os.environ, MAESTRO_PICK_KIND=kind, MAESTRO_PICK_TITLE=title,
                   MAESTRO_PICK_START=start, MAESTRO_PICK_FILTER=flt)
        enc = base64.b64encode(_PS_PICK_SCRIPT.encode("utf-16-le")).decode("ascii")
        return [("powershell",
                 ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                 {"env": env, "creationflags": 0x08000000})]   # CREATE_NO_WINDOW
    c = []
    if shutil.which("zenity"):
        a = ["zenity", "--file-selection", "--title=" + title,
             "--filename=" + start.rstrip("/") + "/"]
        if kind == "folder":
            a.append("--directory")
        elif patterns:
            a += ["--file-filter=Supported files | " + " ".join(patterns), "--file-filter=All files | *"]
        c.append(("zenity", a, {}))
    if shutil.which("kdialog"):
        a = ["kdialog", "--title", title]
        if kind == "folder":
            a += ["--getexistingdirectory", start]
        else:
            a += ["--getopenfilename", start]
            if patterns:
                a.append(" ".join(patterns) + "|Supported files\n*|All files")
        c.append(("kdialog", a, {}))
    for name, script in (("gtk", _GTK_PICK_SCRIPT), ("tk", _TK_PICK_SCRIPT)):
        c.append((name, [sys.executable, "-c", script, kind, title, start, json.dumps(patterns)], {}))
    return c


def _run_picker(candidates) -> tuple:
    """Try each backend until one shows a dialog. Returns (path, cancelled, error)."""
    last_err = ""
    for name, argv, kw in candidates:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=600, **kw)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return "", False, "The file dialog timed out"
        except OSError as ex:
            last_err = f"{name}: {ex}"
            continue
        err = (r.stderr or "").strip()
        out = (r.stdout or "").strip()
        if r.returncode == 3 and name in ("gtk", "tk"):
            continue                            # python module for that backend is not installed
        if r.returncode == 0:
            path = out.splitlines()[0].strip() if out else ""
            return (path, False, "") if path else ("", True, "")
        low = err.lower()
        no_display = ("display" in low and ("cannot" in low or "unable" in low or "no " in low))
        if r.returncode == 1 and not no_display:
            return "", True, ""                 # dialog closed without choosing
        last_err = f"{name}: {err[:200] or 'exit ' + str(r.returncode)}"
    return "", False, last_err or (
        "No file dialog available — install zenity (Fedora: sudo dnf install zenity)")


# ── Multiplexed log stream: subscriber adapter ─────────────────────────────────
class _TaggedQueue:
    """
    Stands in for a per-entry subscriber queue (daemon.py only ever calls
    put_nowait() on whatever sits in an entry's `subs` list) and forwards every
    item, tagged with itself, into ONE shared queue owned by a single
    /api/logs-all stream. This lets one HTTP connection carry the logs of
    every entry without any change to daemon.py's pub/sub code.
    Raises queue.Full exactly like a real queue, so daemon.py's existing
    "drop slow subscriber" handling keeps working.
    """
    __slots__ = ("uid", "out")

    def __init__(self, uid: str, out: "queue.Queue"):
        self.uid = uid
        self.out = out

    def put_nowait(self, item):
        self.out.put_nowait((self, item))


# ── HTTP Handler ───────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass   # suppress access log

    # ── Routing ────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        # Origin/Referer check only applies to data-bearing /api/* routes.
        # Static assets (/, /ui.js, /webui.js, /img/*) MUST stay exempt: a
        # direct browser navigation or hard refresh sends no Origin header
        # and often no Referer either, so gating those would make the UI
        # itself unloadable. They carry no secrets and are pure read-only
        # shell/asset files, so this exemption doesn't reopen Fix #1 — the
        # sensitive surface (settings, file contents, process control) is
        # entirely under /api/*, and stays protected below.
        if path.startswith("/api/") and not self._origin_ok():
            self._err(403, "cross-origin request denied")
            return
        if path in ("", "/"):
            self._serve_file(UI_FILE, "text/html")
        elif path == "/ui.js":
            self._serve_file(UI_JS,    "application/javascript")
        elif path == "/webui.js":
            self._serve_file(WEBUI_JS, "application/javascript")
        elif path == "/api/state":
            self._api_state()
        elif path == "/api/logs-all":
            self._sse_stream_all()
        elif path.startswith("/api/logs/"):
            self._api_logs(path[len("/api/logs/"):])
        elif path == "/api/read-file":
            self._read_file()
        elif path == "/api/settings":
            # Fix #2: never return the real token to the browser — a full
            # unauthenticated GET must not leak it. Signal presence instead.
            safe = dict(_settings)
            if safe.get("githubToken"):
                safe["githubToken"]    = ""
                safe["githubTokenSet"] = True
            else:
                safe["githubTokenSet"] = False
            self._json(safe)
        elif path == "/api/update-check":
            self._api_update_check()
        elif path == "/api/update-check-all":
            self._api_update_check_all()
        elif path == "/api/export":
            self._api_export()
        elif path.startswith("/img/"):
            fn  = Path(path[len("/img/"):]).name
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            self._serve_file(IMG_DIR / fn, _MIME.get(ext, "application/octet-stream"))
        else:
            self._err(404, "Not found")

    def do_POST(self):
        if not self._origin_ok():
            self._err(403, "cross-origin request denied")
            return
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
            "/api/pick-path":      self._pick_path,
            "/api/toggle-logging": self._toggle_logging,
            "/api/settings":       self._post_settings,
            "/api/shutdown":       self._shutdown,
            "/api/update-maestro": self._update_maestro,
            "/api/import":         self._import_state,
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
        # Intentionally NO Access-Control-Allow-Origin header at all: this API
        # has no auth, so any cross-origin readability is a full data/credential
        # takeover risk (see MAESTRO_AUDIT_FINAL.md Fix #1). ui.js only ever
        # makes same-origin relative-path fetches and never needs this header.
        # Do not add it back, even conditionally on Origin — that single header
        # is the entire vulnerability. _origin_ok() below is a second,
        # independent layer against blind (non-reading) cross-origin POSTs,
        # which CORS headers never protected against anyway.
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Maestro-Internal")

    def _origin_ok(self) -> bool:
        """
        Reject cross-origin state-changing/reading requests (Fix #1). A real
        browser attaches Origin (or at least Referer) on both same- and
        cross-origin requests in modern browsers; Maestro's own tray.py calls
        (urllib, not a browser) carry no Origin at all but set _INTERNAL_HEADER
        instead — trustworthy specifically because _cors() never sends back an
        Access-Control-Allow-Origin header, so a real cross-origin browser
        fetch can never successfully attach a custom header here (its
        preflight would be refused).
        """
        if self.headers.get(_INTERNAL_HEADER) == "1":
            return True
        raw = self.headers.get("Origin") or self.headers.get("Referer")
        if not raw:
            return False  # no Origin/Referer and no internal marker — reject
        u = urlparse(raw)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        exp_host = str(_settings.get("host", "127.0.0.1"))
        exp_port = int(_settings.get("port", 8865))
        return host in (exp_host, "127.0.0.1", "localhost", "::1") and port == exp_port

    _MAX_BODY = 16 * 1024 * 1024  # 16 MB ceiling (Fix #18)

    def _body(self) -> dict:
        try:
            cl = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {}
        if cl <= 0:
            return {}
        if cl > self._MAX_BODY:
            self._err(413, "payload too large")
            return {}
        try:
            return json.loads(self.rfile.read(cl))
        except Exception:
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
        self._json({
            "entries":        data,
            "loggingEnabled": daemon._logging_enabled,
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

        # Fix #11: cap concurrent SSE subscribers — ThreadingHTTPServer spawns
        # one thread per connection and each SSE stream blocks its thread
        # indefinitely, so with no cap a pile of idle streams (local multi-tab
        # or LAN access) can pin the whole thread pool.
        with daemon._sse_count_lock:
            if daemon._sse_count >= daemon._MAX_SSE:
                self.send_response(503)
                self._cors()
                self.end_headers()
                return
            daemon._sse_count += 1

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
            with daemon._sse_count_lock:
                daemon._sse_count -= 1
            with lock:
                try:    subs_list.remove(q)
                except ValueError: pass

    def _sse_stream_all(self):
        """
        ONE SSE connection carrying the logs of every entry plus the Maestro
        internal log (replaces one connection per open panel — see
        README "Connection resilience", H2/S2: Chrome allows only 6 concurrent
        HTTP/1.1 connections per origin, and N panels used to starve /api/state).

        Every event's data is JSON {"u": <uid>, "d": <payload>}. Events:
          reset      a uid was (re)subscribed: client must drop its copy of that
                     uid's log, a full replay of the buffer follows
          log        one finished log line
          partial    the line currently being rewritten with '\r' (progress output);
                     replaces the previous partial, "" = none. It becomes a normal
                     `log` event when the program ends the line
          crash / restart / close_panel   same meaning as on the per-uid stream
          gone       the uid no longer exists
          ping       idle heartbeat every ~5s (data "1"), lets the client tell
                     an idle stream from a dead one
        New entries (add / import) are picked up by a 1s subscription sync;
        /api/logs/<uid> is unchanged and still used by tray.py.
        """
        with daemon._sse_count_lock:
            if daemon._sse_count >= daemon._MAX_SSE:
                self._err(503, "too many SSE streams")
                return
            daemon._sse_count += 1

        out        = queue.Queue(maxsize=5000)   # shared by all entries, hence larger than the per-uid 500
        subscribed = {}                          # uid -> (adapter, lock, subs_list)
        last_io    = time.monotonic()

        def frame(ev: str, uid: str, data: str) -> bytes:
            payload = json.dumps({"u": uid, "d": data}, ensure_ascii=False)
            return f"event: {ev}\ndata: {payload}\n\n".encode("utf-8", "replace")

        def send(chunks: list):
            nonlocal last_io
            self.wfile.write(b"".join(chunks))
            self.wfile.flush()
            last_io = time.monotonic()

        def sources() -> dict:
            with daemon._lock:
                srcs = {e.uid: (e.subs_lock, e.log_buf, e.subs, (lambda e=e: e.partial))
                        for e in daemon.entries if e.status != "SEP"}
            srcs[daemon.MAESTRO_UID] = (
                daemon._self_subs_lock, daemon._self_buf, daemon._self_subs, (lambda: None))
            return srcs

        def sync():
            """Attach to new/dropped entries (reset + replay), detach from removed ones."""
            srcs = sources()
            for uid in list(subscribed):
                if uid not in srcs:
                    adapter, lock, subs = subscribed.pop(uid)
                    with lock:
                        try:    subs.remove(adapter)
                        except ValueError: pass
                    send([frame("gone", uid, "")])
            for uid, (lock, buf, subs, get_partial) in srcs.items():
                cur = subscribed.get(uid)
                if cur is not None:
                    with lock:
                        alive = cur[0] in subs
                    if alive:
                        continue
                    # daemon.py dropped us after a queue overflow: resubscribe
                adapter = _TaggedQueue(uid, out)
                with lock:                       # snapshot + subscribe atomically, no gap/dup
                    lines = list(buf)
                    partial = get_partial()      # line still being rewritten ('\r' progress), if any
                    subs.append(adapter)
                subscribed[uid] = (adapter, lock, subs)
                chunks = [frame("reset", uid, "")] + [frame("log", uid, l) for l in lines]
                if partial:
                    chunks.append(frame("partial", uid, partial))
                send(chunks)

        try:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type",      "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control",     "no-cache")
            self.send_header("Connection",        "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            last_sync = 0.0
            while not daemon._stop.is_set():
                now = time.monotonic()
                if now - last_sync >= 1.0:
                    sync()
                    last_sync = now
                try:
                    adapter, item = out.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last_io >= 5.0:
                        send([b"event: ping\ndata: 1\n\n"])
                    continue
                cur = subscribed.get(adapter.uid)
                # None = entry closed (handled by sync); a stale adapter = pre-resubscribe leftover
                if item is None or cur is None or cur[0] is not adapter:
                    continue
                send([frame(item[0], adapter.uid, item[1])])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            for adapter, lock, subs in subscribed.values():
                with lock:
                    try:    subs.remove(adapter)
                    except ValueError: pass
            with daemon._sse_count_lock:
                daemon._sse_count -= 1

    def _read_file(self):
        qs  = parse_qs(urlparse(self.path).query)
        raw = qs.get("path", [""])[0]
        p   = Path(raw).resolve()
        if not p.is_file():
            self._json({"error": "not a file"})
            return
        if p.name.lower() in _SENSITIVE_NAMES or p.name.lower().startswith(('id_rsa', 'id_ed25519')):
            self._json({"error": "refusing to read a sensitive file"})
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

    def _api_update_check(self):
        qs = parse_qs(urlparse(self.path).query)
        channel = qs.get("channel", [""])[0]
        if channel not in ("release", "main"):
            self._err(400, "channel must be 'release' or 'main'")
            return

        token = str(_settings.get("githubToken", "")).strip()
        try:
            rel = _github_resolve_channel_release(channel, token)
            ref = rel.get("tag_name", "")
            if not ref:
                raise RuntimeError(f"GitHub API did not return a tag_name for channel '{channel}'")

            remote_ver = _fetch_remote_badge_version(ref, token)
            local_ver  = _read_local_badge_version()
            available  = bool(remote_ver) and remote_ver != local_ver

            if available:
                message = f"v{remote_ver} available (you have v{local_ver or '?'})"
            else:
                message = f"Already up to date (v{local_ver or remote_ver})"

            self._json({
                "ok":            True,
                "channel":       channel,
                "available":     available,
                "remoteVersion": remote_ver,
                "localVersion":  local_ver,
                "message":       message,
            })
        except Exception as ex:
            self._json({"ok": False, "error": str(ex)})

    def _api_update_check_all(self):
        """List every installable release on GitHub (newest first) with the
        local version, so the UI can offer any of them — including rollback.
        Versions come from tag names (no per-release ui.html fetch)."""
        token = str(_settings.get("githubToken", "")).strip()
        try:
            releases = _github_api_get_json(_GITHUB_ALL_RELEASES_API, token)
            if not isinstance(releases, list):
                raise RuntimeError("GitHub API did not return a release list")
            local_ver = _read_local_badge_version()
            out = []
            for rel in releases:
                tag = (rel.get("tag_name") or "").strip()
                if not tag.startswith("v") and not tag.startswith("V"):
                    continue
                ver  = tag.lstrip("vV")
                out.append({
                    "tag":         tag,
                    "version":     ver,
                    "prerelease":  bool(rel.get("prerelease", False)),
                    "publishedAt": rel.get("published_at") or "",
                    "isCurrent":   bool(local_ver) and local_ver == ver,
                })
            out.sort(key=lambda r: _version_tuple(r["version"]), reverse=True)
            self._json({"ok": True, "localVersion": local_ver, "releases": out})
        except Exception as ex:
            self._json({"ok": False, "error": str(ex)})

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
        idx = _as_index(b.get("idx"))
        if idx is None:
            self._err(400, "idx must be an integer")
            return
        with daemon._lock:
            if not (0 <= idx < len(daemon.entries)):
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
        _argv, _needs_exec, why = daemon._resolve_command(path)
        if why:
            self._err(400, why)
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
        idx = _as_index(b.get("idx"))
        if idx is None:
            self._err(400, "idx must be an integer")
            return
        with daemon._lock:
            if not (0 <= idx < len(daemon.entries)):
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
        a  = _as_index(b.get("a"))
        bv = _as_index(b.get("b"))
        if a is None or bv is None:
            self._err(400, "a/b must be integers")
            return
        with daemon._lock:
            n = len(daemon.entries)
            if not (0 <= a < n and 0 <= bv < n):
                self._err(404, "out of range")
                return
            daemon.entries[a], daemon.entries[bv] = daemon.entries[bv], daemon.entries[a]
            daemon.save_config()
        self._json({"ok": True})

    def _add_sep(self, b: dict):
        name  = (b.get("name") or "Separator").strip() or "Separator"
        after = _as_index(b.get("after"))
        if after is None:
            after = len(daemon.entries)
        sf    = b.get("sepFile", "").strip()
        with daemon._lock:
            pos = min(max(0, after + 1), len(daemon.entries))
            daemon.entries.insert(pos, daemon.ProcessEntry("SEP", name, sep_file=sf))
            daemon.save_config()
        self._json({"ok": True})

    def _update_entry(self, b: dict):
        idx = _as_index(b.get("idx"))
        if idx is None:
            self._err(400, "idx must be an integer")
            return
        with daemon._lock:
            if not (0 <= idx < len(daemon.entries)):
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
            e.partial = None
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

    def _pick_path(self, b: dict):
        """Open the OS file/folder dialog and return the chosen path.
        Body: {kind: "file"|"folder", title?, filter?: "launchable"|"sh"|"md"|"any", start?}.
        Reply: {ok, path, cancelled} or {ok: false, error}."""
        kind  = "folder" if str(b.get("kind", "")).strip() == "folder" else "file"
        title = str(b.get("title") or ("Select folder" if kind == "folder" else "Select file"))[:120]
        exts  = _PICK_FILTERS.get(str(b.get("filter", "")).strip(), [])
        start = _pick_start_dir(str(b.get("start", "") or ""))
        if not _pick_lock.acquire(blocking=False):
            self._json({"ok": False, "error": "A file dialog is already open"})
            return
        try:
            path, cancelled, err = _run_picker(_pick_candidates(kind, title, exts, start))
        finally:
            _pick_lock.release()
        if err:
            daemon._mlog(f"File dialog failed: {err}")
            self._json({"ok": False, "error": err})
            return
        self._json({"ok": True, "path": os.path.abspath(path) if path else "", "cancelled": cancelled})

    def _toggle_logging(self, _b: dict):
        new_state = daemon.toggle_logging()
        _settings["logsRecorder"] = new_state
        _write_settings(_DATA_DIR, _settings)
        self._json({"ok": True, "enabled": new_state})

    def _post_settings(self, b: dict):
        # Fix #6: reject a bad port/host before it ever reaches disk — an
        # invalid value here would otherwise only surface as a crash on the
        # *next* startup, requiring manual settings.json editing to recover.
        if "port" in b:
            try:
                p = int(b["port"])
                if not (1 <= p <= 65535):
                    raise ValueError
                b["port"] = p
            except (TypeError, ValueError):
                self._err(400, "port must be an integer 1-65535")
                return
        if "host" in b and not isinstance(b["host"], str):
            self._err(400, "host must be a string")
            return
        if "terminal" in b:
            if not isinstance(b["terminal"], str):
                self._err(400, 'terminal must be a string')
                return
            b["terminal"] = b["terminal"].strip()
        if "webAppAutoSuspendMin" in b:
            try:
                m = int(b["webAppAutoSuspendMin"])
                if not (0 <= m <= 1440):
                    raise ValueError
                b["webAppAutoSuspendMin"] = m
            except (TypeError, ValueError):
                self._err(400, "webAppAutoSuspendMin must be an integer 0-1440")
                return

        # Fix #15: serialize settings mutation against concurrent saves.
        with _settings_lock:
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
                    except Exception: pass
            if _server:
                threading.Thread(target=_server.shutdown, daemon=True).start()

        threading.Thread(target=_do, daemon=True).start()

    def _update_maestro(self, b: dict):
        # Fix #20: reject a concurrent update attempt outright rather than
        # letting two staging runs race and leak temp directories.
        if not _update_lock.acquire(blocking=False):
            self._json({"ok": False, "error": "An update is already in progress."})
            return
        try:
            channel = str(b.get("channel", "")).strip()
            tag     = str(b.get("tag", "")).strip()
            if not tag and channel not in ("release", "main"):
                self._err(400, "provide 'channel' ('release'/'main') or an explicit 'tag'")
                return
            target  = tag or channel

            try:
                staging_dir = _stage_update(channel=channel or None, tag=tag or None)
            except Exception as ex:
                daemon._mlog(f"✕ Update staging failed ({target}): {ex}")
                self._json({"ok": False, "error": str(ex)})
                return

            marker = {
                "channel":     target,
                "tag":         tag,
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

            daemon._mlog(f"✓ Update staged ({target}) — restarting to apply…")
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
                        except Exception: pass
                if _server:
                    threading.Thread(target=_server.shutdown, daemon=True).start()

            threading.Thread(target=_do, daemon=True).start()
        finally:
            _update_lock.release()

    # ── Export / Import (full state bundle) ────────────────────────────────────
    def _api_export(self):
        """Return the full Maestro state as a JSON bundle: settings + every
        config entry (SEP rows included). Runtime-only fields (pid, metrics,
        crash counters) are never exported."""
        with daemon._lock:
            snap = list(daemon.entries)
        entries_out = []
        for e in snap:
            entries_out.append({
                "status":      e.status,
                "path":        e.path,
                "name":        e.name,
                "delay":       e.delay,
                "autoRestart": e.auto_restart,
                "keepOpen":    e.keep_open,
                "launchHidden":e.launch_hidden,
                "webapps":     list(e.webapps),
                "updateScript":e.update_script,
                "sepFile":     e.sep_file,
                "infoFile":    e.info_file,
                "openOutside": e.open_outside,
                "termColor":   e.term_color,
            })
        self._json({
            "app":        "Maestro",
            "format":     1,
            "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "version":    _read_local_badge_version(),
            "settings":   dict(_settings),
            "entries":    entries_out,
        })

    def _import_state(self, b: dict):
        """Replace the full Maestro state with an exported bundle.
        Kills every running script first; imported entries all start OFF —
        the user relaunches them explicitly."""
        if not isinstance(b, dict) or b.get("app") != "Maestro":
            self._err(400, "not a valid Maestro export file")
            return
        raw_entries = b.get("entries")
        if not isinstance(raw_entries, list):
            self._err(400, "missing 'entries' list")
            return
        new_entries = []
        for i, d in enumerate(raw_entries):
            if not isinstance(d, dict):
                self._err(400, f"entry {i} is not an object")
                return
            status = str(d.get("status", "")).strip()
            path   = str(d.get("path", ""))
            name   = str(d.get("name", ""))
            if status == "SEP":
                new_entries.append(daemon.ProcessEntry(
                    "SEP", path, name=name,
                    sep_file=str(d.get("sepFile", "")),
                    info_file=str(d.get("infoFile", ""))))
                continue
            if status not in ("ON", "OFF"):
                self._err(400, f"entry {i} has unknown status '{status}'")
                return
            if not path:
                self._err(400, f"entry {i} has no path")
                return
            wb = d.get("webapps") or []
            if not isinstance(wb, list):
                self._err(400, f"entry {i} has invalid webapps")
                return
            try:
                delay = int(d.get("delay", 0) or 0)
            except (TypeError, ValueError):
                delay = 0
            # Entries are always imported stopped (OFF): runtime state is
            # never transferred, and paths may not exist on this machine.
            new_entries.append(daemon.ProcessEntry(
                "OFF", path, delay=delay,
                auto_restart=bool(d.get("autoRestart", False)),
                keep_open=bool(d.get("keepOpen", True)),
                name=name,
                launch_hidden=bool(d.get("launchHidden", False)),
                webapps=[str(x) for x in wb],
                update_script=str(d.get("updateScript", "")),
                sep_file=str(d.get("sepFile", "")),
                info_file=str(d.get("infoFile", "")),
                open_outside=bool(d.get("openOutside", False)),
                term_color=str(d.get("termColor", "default")),
            ))
        raw_settings = b.get("settings") or {}
        if not isinstance(raw_settings, dict):
            self._err(400, "'settings' is not an object")
            return
        changed = False
        for k, v in raw_settings.items():
            if k not in _DEFAULT_SETTINGS:
                continue   # ignore unknown keys from newer/other builds
            if _settings.get(k) != v:
                _settings[k] = v
                changed = True
        with daemon._lock:
            daemon.kill_all()
            daemon.entries = new_entries
            daemon.save_config()
        if isinstance(raw_settings.get("logsRecorder"), bool):
            daemon._logging_enabled = raw_settings["logsRecorder"]
        if changed:
            _write_settings(_DATA_DIR, _settings)
        self._json({"ok": True, "entriesImported": len(new_entries)})


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
    if not isinstance(host, str) or not host.strip():
        host = "127.0.0.1"
    # Fix #6: defensive fallback in case settings.json somehow already has a
    # bad port (e.g. edited by hand) — never let a corrupt value crash startup.
    try:
        port = int(_settings.get("port", 8865))
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        daemon._mlog(f"Invalid port in settings.json ({_settings.get('port')!r}) — using 8865")
        port = 8865

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
