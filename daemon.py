#!/usr/bin/env python3
"""
Maestro daemon.py — Process manager, SSE broker, metrics, config I/O.
Path: daemon.py

Must be initialized by main.py via daemon.init(base_dir, data_dir)
before any other function is called.

Responsibilities:
  - ProcessEntry lifecycle (launch, kill, crash, auto-restart)
  - SSE pub/sub for log streaming
  - System and per-process metrics
  - Config read/write (config.txt)
  - Unreachable entry detection (path missing on disk)
  - Desktop notifications (Linux: notify-send; Windows: tray callback)

Cross-platform: Linux + Windows via ctypes, zero external dependencies.
main.py owns HTTP routing and serves UI files.
daemon.py owns all process state.
"""
import codecs, collections, json, locale, os, queue, re, shutil
import signal, subprocess, sys, tempfile, threading, time, traceback, uuid
from pathlib import Path

# ── Platform ───────────────────────────────────────────────────────────────────
_WIN = sys.platform == 'win32'

if _WIN:
    import ctypes
    import ctypes.wintypes
    # Process creation flags (Windows, used only inside _WIN code paths)
    _CF_NO_WINDOW   = 0x08000000   # CREATE_NO_WINDOW  — background proc, no console flash
    _CF_NEW_CONSOLE = 0x00000010   # CREATE_NEW_CONSOLE — visible window for external procs
    # Process access rights
    _PROC_QUERY     = 0x0400       # PROCESS_QUERY_INFORMATION
    _PROC_VM_READ   = 0x0010       # PROCESS_VM_READ
    _PROC_TERMINATE = 0x0001       # PROCESS_TERMINATE
    _STILL_ACTIVE   = 259          # GetExitCodeProcess sentinel: process is running

# ── Constants ──────────────────────────────────────────────────────────────────
MAESTRO_UID  = "__maestro__"
BUF_LINES    = 1000
LOG_MAX_B    = 5_242_880
LOG_KEEP     = 2
CRASH_WAIT   = 3.0
VALID_COLORS = {"default", "navy", "forest", "plum", "teal", "ember"}
_MAX_SSE     = 64   # Fix #11: cap on concurrent SSE subscribers
_sse_count       = 0
_sse_count_lock  = threading.Lock()

try:    CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
except (KeyError, ValueError, OSError, AttributeError): CLK_TCK = 100
_NUM_CPUS = max(1, os.cpu_count() or 1)

_ANSI = re.compile(r"\x1b(?:\[[0-9;]*[mGKHFA-Za-z]|\][^\x07]*\x07|[()][AB012])")

# ── Windows ctypes structures (defined only when _WIN is True) ─────────────────
if _WIN:
    class _FILETIME(ctypes.Structure):
        """Windows FILETIME: 100-nanosecond intervals since Jan 1, 1601."""
        _fields_ = [
            ('dwLowDateTime',  ctypes.wintypes.DWORD),
            ('dwHighDateTime', ctypes.wintypes.DWORD),
        ]

    def _ft_int(ft: '_FILETIME') -> int:
        """Convert FILETIME to a plain integer (100 ns ticks)."""
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    class _PMC(ctypes.Structure):
        """PROCESS_MEMORY_COUNTERS (psapi.h) — WorkingSetSize gives RSS."""
        _fields_ = [
            ('cb',                          ctypes.wintypes.DWORD),
            ('PageFaultCount',              ctypes.wintypes.DWORD),
            ('PeakWorkingSetSize',           ctypes.c_size_t),
            ('WorkingSetSize',               ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage',      ctypes.c_size_t),
            ('QuotaPagedPoolUsage',          ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage',   ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage',       ctypes.c_size_t),
            ('PagefileUsage',                ctypes.c_size_t),
            ('PeakPagefileUsage',            ctypes.c_size_t),
        ]

# ── Path variables (set by init()) ────────────────────────────────────────────
_BASE_DIR   = None
_DATA_DIR   = None
CONFIG_FILE = None
LOG_DIR     = None
GLOBAL_LOG  = None


def init(base_dir: Path, data_dir: Path):
    """
    Initialize path constants. Must be the first call from main.py.

    Args:
        base_dir: directory containing source files (bin/)
        data_dir: directory for user data: config.txt, logs/, settings.json (data/)
    """
    global _BASE_DIR, _DATA_DIR, CONFIG_FILE, LOG_DIR, GLOBAL_LOG
    _BASE_DIR   = Path(base_dir)
    _DATA_DIR   = Path(data_dir)
    CONFIG_FILE = _DATA_DIR / "config.txt"
    LOG_DIR     = _DATA_DIR / "logs"
    GLOBAL_LOG  = _DATA_DIR / "logs" / "maestro.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Global state ───────────────────────────────────────────────────────────────
_lock            = threading.RLock()
entries: list    = []
_stop            = threading.Event()   # set by main.py shutdown to stop all loops
_logging_enabled = True

_self_buf        = collections.deque(maxlen=BUF_LINES)
_self_subs: list = []
_self_subs_lock  = threading.Lock()

# Windows bash path cache: Ellipsis = unchecked; None = not found; str = found path
_bash_win_cache = ...


# ── Logging ────────────────────────────────────────────────────────────────────
def _write_log(name: str, line: str):
    """Append a line to the rotating global log file (honours _logging_enabled)."""
    if not _logging_enabled:
        return
    try:
        ts = time.strftime("%H:%M:%S")
        if GLOBAL_LOG.exists() and GLOBAL_LOG.stat().st_size >= LOG_MAX_B:
            # Fix #19: rotate correctly for LOG_KEEP files — drop the oldest
            # (maestro.<LOG_KEEP>.log) before shifting, so exactly LOG_KEEP
            # rotated files are ever kept (previously kept LOG_KEEP+1).
            oldest = LOG_DIR / f"maestro.{LOG_KEEP}.log"
            if oldest.exists():
                try: oldest.unlink()
                except OSError: pass
            for i in range(LOG_KEEP - 1, 0, -1):
                src = LOG_DIR / f"maestro.{i}.log"
                dst = LOG_DIR / f"maestro.{i + 1}.log"
                if src.exists():
                    src.rename(dst)
            GLOBAL_LOG.rename(LOG_DIR / "maestro.1.log")
        with open(GLOBAL_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{ts}][{name}] {line}\n")
    except OSError:
        pass


def _mlog(msg: str):
    """Log to the Maestro internal channel (SSE __maestro__ + file)."""
    ts   = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _self_buf.append(line)
    _write_log("MAESTRO", msg)
    with _self_subs_lock:
        dead = []
        for q in _self_subs:
            try:    q.put_nowait(("log", line))
            except queue.Full: dead.append(q)
        for q in dead:
            _self_subs.remove(q)


def _mlog_event(e, msg: str):
    """Log to both the entry's own channel and the Maestro internal channel."""
    _append_log(e, msg)
    _mlog(f"[{e.display_name()}] {msg}")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _install_excepthook():
    """Route unhandled exceptions to the Maestro log before the default handler."""
    orig = sys.excepthook
    def _h(et, ev, tb):
        _mlog("UNHANDLED:\n" + "".join(traceback.format_exception(et, ev, tb)))
        orig(et, ev, tb)
    sys.excepthook = _h


def toggle_logging() -> bool:
    """Toggle file logging on/off. Returns the new state."""
    global _logging_enabled
    _logging_enabled = not _logging_enabled
    _mlog(f"File logging {'enabled' if _logging_enabled else 'disabled'}")
    return _logging_enabled


# ── Config field escaping (Fix #8) ─────────────────────────────────────────────
# config.txt is pipe-delimited with no quoting; a literal "|" or newline inside
# a path/name field silently corrupts every field after it on load. Percent-
# encoding is round-trip safe and backward compatible: old files with no
# percent-sequences decode completely unchanged.
def _enc(s: str) -> str:
    return str(s).replace("%", "%25").replace("|", "%7C").replace("\n", "%0A")


def _dec(s: str) -> str:
    return s.replace("%0A", "\n").replace("%7C", "|").replace("%25", "%")


# ── ProcessEntry ───────────────────────────────────────────────────────────────
class ProcessEntry:
    def __init__(self, status, path, pid=None, delay=0,
                 auto_restart=False, keep_open=True, name="",
                 launch_hidden=False, webapps=None,
                 update_script="", sep_file="", info_file="",
                 open_outside=False, term_color="default"):
        self.uid           = uuid.uuid4().hex[:10]
        self.status        = status
        self.path          = path
        self.name          = name
        self.delay         = delay
        self.auto_restart  = auto_restart
        self.keep_open     = keep_open
        self.launch_hidden = launch_hidden
        self.webapps       = webapps or []
        self.update_script = update_script
        self.sep_file      = sep_file
        self.info_file     = info_file
        self.open_outside  = open_outside
        self.term_color    = term_color if term_color in VALID_COLORS else "default"

        # Runtime state (not persisted)
        self.pid             = pid
        self.proc            = None       # internal subprocess Popen
        self.ext_proc        = None       # external terminal Popen
        self.ext_rc_file     = None       # file the wrapper writes the script's exit code to
        self.ext_pid_file    = None       # file the wrapper writes the script's shell PID to
        self.crash           = False
        self.manually_killed = False
        self.generation      = 0          # incremented on each launch to invalidate old threads
        self.unreachable     = False      # True when self.path does not exist on disk

        # SSE / metrics
        self.subs_lock = threading.Lock()
        self.log_buf   = collections.deque(maxlen=BUF_LINES)
        self.subs      = []               # list[queue.Queue] — live SSE clients
        self.partial   = None             # in-progress line ('\r' progress output), guarded by subs_lock
        self.metrics   = {"cpu": 0.0, "ram": 0}
        self._cpu_prev = None             # platform-specific CPU delta state

    def display_name(self) -> str:
        if self.name:
            return self.name
        p = Path(self.path).parts
        return f"{p[-2]}/{p[-1]}" if len(p) >= 2 else (p[-1] if p else self.path)

    def _has_update(self) -> bool:
        if self.status == "SEP":
            return False
        if self.update_script:
            return Path(self.update_script).exists()
        try:
            return (Path(self.path).parent / "update.sh").exists()
        except Exception:
            return False

    def to_dict(self, idx: int) -> dict:
        return {
            "uid":         self.uid,
            "idx":         idx,
            "status":      self.status,
            "path":        self.path,
            "name":        self.name,
            "displayName": self.display_name(),
            "pid":         self.pid,
            "delay":       self.delay,
            "autoRestart": self.auto_restart,
            "keepOpen":    self.keep_open,
            "launchHidden":self.launch_hidden,
            "webapps":     self.webapps,
            "updateScript":self.update_script,
            "hasUpdate":   self._has_update(),
            "sepFile":     self.sep_file,
            "infoFile":    self.info_file,
            "hasInfo":     bool(self.info_file and Path(self.info_file).exists()),
            "openOutside": self.open_outside,
            "termColor":   self.term_color,
            "crash":       self.crash,
            "metrics":     dict(self.metrics),
            "unreachable": self.unreachable,
        }


# ── PID check ──────────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """Return True if the given PID is a live, non-zombie process."""
    if _WIN:
        try:
            h = ctypes.windll.kernel32.OpenProcess(_PROC_QUERY, False, pid)
            if not h:
                return False
            code = ctypes.wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == _STILL_ACTIVE
        except Exception:
            return False
    # Linux: /proc/<pid>/status
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("State:"):
                return "Z" not in line
        return True
    except (FileNotFoundError, PermissionError):
        return False


# ── Config I/O ─────────────────────────────────────────────────────────────────
def _int(s, d: int = 0) -> int:
    try:    return max(0, int(str(s).strip()))
    except Exception: return d


def _parse_webapps(raw: str) -> list:
    if not raw or raw in ("[]", ""):
        return []
    try:
        data = json.loads(raw)
        return [{"url": item["url"]} for item in data if "url" in item]
    except Exception:
        return []


def load_config():
    """Parse config.txt into the global entries list."""
    global entries
    new = []
    if not CONFIG_FILE or not CONFIG_FILE.exists():
        entries = new
        return
    for raw in CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        p     = line.split("|", 14)
        st    = p[0].strip()
        path  = _dec(p[1].strip())  if len(p) > 1  else ""
        pid_s = p[2].strip()  if len(p) > 2  else ""
        delay = _int(p[3])    if len(p) > 3  else 0
        ar    = (_int(p[4]) != 0) if len(p) > 4  else False
        ko    = (_int(p[5]) != 0) if len(p) > 5  else True
        name  = _dec(p[6].strip())  if len(p) > 6  else ""
        lh    = (_int(p[8]) != 0) if len(p) > 8  else False
        wb    = _parse_webapps(p[9].strip()  if len(p) > 9  else "")
        us    = _dec(p[10].strip()) if len(p) > 10 else ""
        sf    = _dec(p[11].strip()) if len(p) > 11 else ""
        inf   = _dec(p[12].strip()) if len(p) > 12 else ""
        oo    = (_int(p[13]) != 0) if len(p) > 13 else False
        tc    = p[14].strip() if len(p) > 14 else "default"
        if st == "SEP":
            new.append(ProcessEntry("SEP", path, name=name, sep_file=sf, info_file=inf))
            continue
        if not path:
            continue
        pid = None
        if pid_s:
            try:
                c = int(pid_s)
                if _pid_alive(c):
                    pid = c
            except ValueError:
                pass
        new.append(ProcessEntry(st, path, pid, delay, ar, ko, name, lh, wb, us, sf, inf, oo, tc))
    entries = new


def save_config():
    """Atomic write of entries to config.txt via tempfile + shutil.move."""
    if not CONFIG_FILE:
        return
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                ps = str(e.pid) if e.pid is not None else ""
                if e.status == "SEP":
                    f.write(
                        f"SEP|{_enc(e.path)}|||0|1|{_enc(e.name)}|0|0|[]"
                        f"||{_enc(e.sep_file)}|{_enc(e.info_file)}|0|default\n"
                    )
                else:
                    ar = 1 if e.auto_restart  else 0
                    ko = 1 if e.keep_open     else 0
                    lh = 1 if e.launch_hidden else 0
                    oo = 1 if e.open_outside  else 0
                    wb = json.dumps(e.webapps, separators=(',', ':'))
                    tc = e.term_color if e.term_color in VALID_COLORS else "default"
                    f.write(
                        f"{e.status}|{_enc(e.path)}|{ps}|{e.delay}|{ar}|{ko}"
                        f"|{_enc(e.name)}|0|{lh}|{wb}|{_enc(e.update_script)}"
                        f"|{_enc(e.sep_file)}|{_enc(e.info_file)}|{oo}|{tc}\n"
                    )
        shutil.move(tmp, str(CONFIG_FILE))
    except Exception as ex:
        _mlog(f"save_config error: {ex}")


# ── Pub/sub ────────────────────────────────────────────────────────────────────
def _append_log(e, line: str):
    """Strip ANSI, push to entry log buffer and all live SSE subscribers."""
    line = strip_ansi(line)
    with e.subs_lock:
        e.log_buf.append(line)
        dead = []
        for q in e.subs:
            try:    q.put_nowait(("log", line))
            except queue.Full: dead.append(q)
        for q in dead:
            e.subs.remove(q)
    _write_log(e.display_name(), line)


def _pub(e, event: str, data: str):
    """Broadcast a non-log SSE event to all live subscribers of entry e."""
    with e.subs_lock:
        for q in e.subs:
            try: q.put_nowait((event, data))
            except queue.Full: pass


def _close_subs(e):
    """Send the sentinel None to all subscribers so their SSE loops exit."""
    with e.subs_lock:
        for q in e.subs:
            try: q.put_nowait(None)
            except queue.Full: pass


# ── Metrics ────────────────────────────────────────────────────────────────────
def _update_metrics(e):
    """Update per-process CPU% and RAM. Dispatches to platform implementation."""
    if _WIN:
        _update_metrics_win(e)
    else:
        _update_metrics_linux(e)


def _update_metrics_linux(e):
    """Linux: read CPU/RAM from /proc/<pid>/stat and /proc/<pid>/status."""
    pid = e.pid
    if pid is None:
        e.metrics   = {"cpu": 0.0, "ram": 0}
        e._cpu_prev = None
        return
    try:
        after = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
        tj  = int(after[11]) + int(after[12])
        now = time.monotonic()
        raw = (
            (tj - e._cpu_prev[0]) / CLK_TCK
            / max(now - e._cpu_prev[1], 0.001) * 100
        ) if e._cpu_prev else 0.0
        e._cpu_prev = (tj, now)
        ram = 0
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                ram = int(line.split()[1]) // 1024
                break
        e.metrics = {"cpu": round(raw / _NUM_CPUS, 1), "ram": ram}
    except Exception:
        e.metrics   = {"cpu": 0.0, "ram": 0}
        e._cpu_prev = None


def _update_metrics_win(e):
    """
    Windows: per-process CPU% via GetProcessTimes and RAM via K32GetProcessMemoryInfo.
    All via ctypes kernel32 — zero external dependencies.
    """
    pid = e.pid
    if pid is None:
        e.metrics   = {"cpu": 0.0, "ram": 0}
        e._cpu_prev = None
        return
    h = ctypes.windll.kernel32.OpenProcess(_PROC_QUERY | _PROC_VM_READ, False, pid)
    if not h:
        e.metrics   = {"cpu": 0.0, "ram": 0}
        e._cpu_prev = None
        return
    try:
        # CPU: delta of (kernel + user) FILETIME ticks (100 ns each)
        ct = _FILETIME(); et = _FILETIME()
        kt = _FILETIME(); ut = _FILETIME()
        ctypes.windll.kernel32.GetProcessTimes(
            h, ctypes.byref(ct), ctypes.byref(et),
            ctypes.byref(kt), ctypes.byref(ut),
        )
        proc_ticks = _ft_int(kt) + _ft_int(ut)
        now = time.monotonic()
        raw = 0.0
        if e._cpu_prev:
            prev_ticks, prev_now = e._cpu_prev
            delta_s     = max(now - prev_now, 0.001)
            delta_ticks = proc_ticks - prev_ticks
            raw = (delta_ticks / 1e7) / delta_s * 100 / _NUM_CPUS
        e._cpu_prev = (proc_ticks, now)

        # RAM: WorkingSetSize (physical RAM in bytes)
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if ctypes.windll.kernel32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            ram_mb = pmc.WorkingSetSize >> 20
        else:
            ram_mb = 0

        e.metrics = {"cpu": round(raw, 1), "ram": ram_mb}
    except Exception:
        e.metrics = {"cpu": 0.0, "ram": 0}
        # Fix #17: leave e._cpu_prev intact on a transient exception so CPU%
        # can resume on the next good sample, instead of resetting the whole
        # delta baseline (which previously caused a full extra sample of 0%
        # after any momentary blip, not just a PID-gone condition).
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


# ── Notifications ──────────────────────────────────────────────────────────────
def _notify(title: str, body: str):
    """
    Desktop notification.
    Linux: notify-send subprocess (best-effort, silent if absent).
    Windows: no-op — daemon.py runs in a separate process from the tray; all
    notifications reach the user via tray.py's SSE listener → balloon instead.
    """
    if _WIN:
        return
    icon = str(_BASE_DIR / "img" / "MaestroLogo24x24.png") if _BASE_DIR else ""
    cmd  = ["notify-send", title, body, "--expire-time", "4000"]
    if icon and Path(icon).exists():
        cmd += ["--icon", icon]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ── Unreachable entry state ────────────────────────────────────────────────────
def _check_paths():
    """
    Check all non-SEP entries for path reachability via os.path.isfile().
    Fires a one-shot notification on each reachable↔unreachable transition.
    Called from _metrics_loop every cycle (~2s). Single-writer — no extra lock needed.
    """
    with _lock:
        snap = [(i, e) for i, e in enumerate(entries) if e.status != "SEP"]
    for _, e in snap:
        was         = e.unreachable
        now_missing = not os.path.isfile(e.path)
        if now_missing == was:
            continue
        e.unreachable = now_missing
        if now_missing:
            _mlog(f"[{e.display_name()}] ⚠ Script not found: {e.path}")
            _notify("Maestro: Script not found",
                    f"{e.display_name()} — {e.path}")
        else:
            _mlog(f"[{e.display_name()}] ✓ Script restored: {e.path}")
            _notify("Maestro: Script restored",
                    f"{e.display_name()} — {e.path}")


# ── Windows bash detection ─────────────────────────────────────────────────────
def _get_bash_win():
    """
    Find and cache bash on Windows. Checks PATH, WSL bash.exe, and Git Bash.
    Returns the executable path (str) or None if not found.
    Only called when _WIN is True.
    """
    global _bash_win_cache
    if _bash_win_cache is not ...:
        return _bash_win_cache
    candidates = [
        'bash',                                          # PATH (WSL, Git Bash if configured)
        r'C:\Windows\System32\bash.exe',                 # WSL
        r'C:\Program Files\Git\bin\bash.exe',            # Git Bash (64-bit)
        r'C:\Program Files (x86)\Git\bin\bash.exe',      # Git Bash (32-bit)
    ]
    found = None
    for c in candidates:
        try:
            r = subprocess.run(
                [c, '--version'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if r.returncode == 0:
                found = c
                break
        except Exception:
            continue
    _bash_win_cache = found
    return found


# ── External terminal helpers ─────────────────────────────────────────────────
# Linux terminal candidates (tried in order). Each builder receives the full
# bash -c command string s (already wrapped by _run_in_external_terminal with
# the exit-code/PID-file wrapper) and the working directory. s is always passed
# as a single argv element — never interpolated inside quotes — so spaces and
# special characters in paths cannot break quoting.
_EXT_CANDIDATES = [
    ("gnome-terminal",    lambda s, cwd: (["gnome-terminal", "--wait", "--", "bash", "-c", s], {"cwd": cwd})),
    ("xfce4-terminal",    lambda s, cwd: (["xfce4-terminal", "--hold", "-e", "bash", "-c", s], {"cwd": cwd})),
    ("xterm",             lambda s, cwd: (["xterm", "-hold", "-e", "bash", "-c", s],            {"cwd": cwd})),
    ("konsole",           lambda s, cwd: (["konsole", "--hold", "-e", "bash", "-c", s],         {"cwd": cwd})),
    ("lxterminal",        lambda s, cwd: (["lxterminal", "-e", "bash", "-c", s],                {"cwd": cwd})),
    ("mate-terminal",     lambda s, cwd: (["mate-terminal", "--window", "-e", "bash", "-c", s], {"cwd": cwd})),
    # ptyxis (default terminal on Fedora Workstation, GTK4/VTE). Two verified
    # behaviors drive this entry:
    #   1. It does not propagate the command's exit code to its own process —
    #      crash detection therefore relies on the rc-file written by the
    #      wrapper, not on proc.returncode.
    #   2. Without -s it forwards the command to an existing instance and the
    #      launching process exits immediately; with -s this Popen stays the
    #      process hosting the command for its whole duration, which keeps
    #      _monitor_external correct and makes SIGTERM delivery effective.
    ("ptyxis",            lambda s, cwd: (["ptyxis", "-s", "-d", cwd, "--", "bash", "-c", s],   {"cwd": cwd})),
    # Generic portal fallback, last resort (may not exist on the system).
    ("xdg-terminal-exec", lambda s, cwd: (["xdg-terminal-exec", "--", "bash", "-c", s],         {"cwd": cwd})),
]


def _generic_builder(name):
    """Fallback builder for a terminal command supplied by the user."""
    return lambda s, cwd: ([name, "--", "bash", "-c", s], {"cwd": cwd})


def _terminal_preference() -> str:
    """
    Preferred terminal for open_outside entries, checked in order:
      1. MAESTRO_TERMINAL environment variable
      2. "terminal" key in settings.json
    Empty string = auto-detect through _EXT_CANDIDATES.
    """
    v = os.environ.get("MAESTRO_TERMINAL", "").strip()
    if v:
        return v
    try:
        if _DATA_DIR:
            on_disk = json.loads((_DATA_DIR / "settings.json").read_text(encoding="utf-8"))
            v = str(on_disk.get("terminal", "")).strip()
            if v:
                return v
    except Exception:
        pass
    return ""


def _kill_ext(e):
    """
    Terminate the external terminal associated with entry e.
    Clears e.ext_proc before sending the signal to avoid _monitor_external races.
    """
    proc       = e.ext_proc
    e.ext_proc = None
    if not proc or proc.poll() is not None:
        return
    if _WIN:
        try:
            proc.terminate()
        except Exception:
            pass
        def _force():
            time.sleep(2.0)
            if proc.poll() is None:
                try:    proc.kill()
                except Exception: pass
        threading.Thread(target=_force, daemon=True).start()
    else:
        # Prefer the script's own shell PID recorded by the wrapper: for
        # client/server terminals (ptyxis) the Popen PID may not be the process
        # actually running the script, so the process-group kill alone can miss it.
        try:
            if e.ext_pid_file and e.ext_pid_file.exists():
                os.kill(int(e.ext_pid_file.read_text().strip()), signal.SIGTERM)
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:    proc.terminate()
            except Exception: pass

        def _force():
            time.sleep(2.0)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        threading.Thread(target=_force, daemon=True).start()


def _run_in_external_terminal_win(e, script_path: str, label: str):
    """
    Windows: launch a script in a visible terminal window.
    Tries Windows Terminal (wt.exe) first, then bash with CREATE_NEW_CONSOLE.
    Requires bash (WSL or Git Bash) — logs an error and returns None if absent.
    _kill_ext(e) is assumed to have already been called by the caller.
    """
    cwd = str(Path(script_path).parent)
    if Path(script_path).suffix.lower() in (".exe", ".com", ".bat", ".cmd", ".ps1", ".py", ".jar"):
        # Native Windows file: no bash needed, run it directly in a console window
        argv, _x, err = _resolve_command(script_path)
        if err:
            _append_log(e, f"[MAESTRO] ✕ {label} failed: {err}")
            return None
        candidates = [
            (['wt.exe', '-d', cwd] + argv, {}),
            (argv, {'cwd': cwd, 'creationflags': _CF_NEW_CONSOLE}),
        ]
    else:
        bash = _get_bash_win()
        if not bash:
            _append_log(e,
                "[MAESTRO] ✕ No bash found — install WSL or Git Bash to launch scripts")
            _mlog(f"[{e.display_name()}] ✕ No bash found (WSL / Git Bash required)")
            return None

        tail    = 'echo ""; echo "--- Done (exit: $?) ---"; read -p "Press Enter to close..."'
        cmd_str = f'bash "{script_path}"; {tail}'

        # Candidates: Windows Terminal (best UX), then bash with a new console window
        candidates = [
            (['wt.exe', '-d', cwd, bash, '-c', cmd_str], {}),
            ([bash, '-c', cmd_str], {'cwd': cwd, 'creationflags': _CF_NEW_CONSOLE}),
        ]

    for cmd, kwargs in candidates:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            with _lock:
                e.ext_proc = proc
            _append_log(e,
                f"[MAESTRO] ↗ {label} in external terminal — PID {proc.pid} ({cmd[0]})")
            _mlog(f"[{e.display_name()}] ↗ {label}: {cmd[0]} PID {proc.pid}")
            return proc
        except FileNotFoundError:
            continue
        except Exception as ex:
            _mlog(f"[{e.display_name()}] ✕ External {label} ({cmd[0]}): {ex}")
            return None

    _append_log(e,
        "[MAESTRO] ✕ No terminal found — install Windows Terminal or ensure bash is in PATH")
    _mlog(f"[{e.display_name()}] ✕ No terminal found on Windows")
    return None


def _run_in_external_terminal(e, script_path: str, label: str = "Launch"):
    """
    Launch script_path in an OS terminal emulator. Dispatches to platform implementation.
    Always calls _kill_ext(e) first to clean up any previous external terminal.
    Returns the Popen object on success, None on failure.
    """
    _kill_ext(e)

    if _WIN:
        return _run_in_external_terminal_win(e, script_path, label)

    # Linux: try emulators in order
    argv, needs_exec, err = _resolve_command(script_path)
    if err:
        _append_log(e, f"[MAESTRO] ✕ {label} failed: {err}")
        _mlog(f"[{e.display_name()}] ✕ {label} failed: {err}")
        return None
    if needs_exec:
        _ensure_exec(script_path)
    cwd = str(Path(script_path).parent)

    # State files under data/: the wrapper writes the script's own shell PID and
    # its exit status to files the daemon can read afterward. Needed because some
    # emulators (ptyxis above all) do not propagate the command's exit code, so
    # proc.returncode alone cannot distinguish "finished" from "crashed", and
    # because client/server terminals report a PID that is not the shell actually
    # running the script. The window stays open until a keypress in every case,
    # so a crashed script can be inspected before it closes.
    state_dir = _DATA_DIR / "terminal_state"
    try: state_dir.mkdir(parents=True, exist_ok=True)
    except Exception: pass
    rc_file  = state_dir / f"{e.uid}.rc"
    pid_file = state_dir / f"{e.uid}.pid"
    for st in (rc_file, pid_file):
        try:
            if st.exists(): st.unlink()
        except Exception: pass
    e.ext_rc_file  = rc_file
    e.ext_pid_file = pid_file

    cmd_str = (
        f'echo $$ > {_shq(str(pid_file))}; '
        f"R=130; trap 'echo \"$R\" > {_shq(str(rc_file))}' EXIT; "
        f'{" ".join(_shq(a) for a in argv)}; '
        f'R=$?; echo "$R" > {_shq(str(rc_file))}; '
        f'echo ""; echo "--- Done (exit: $R) ---"; '
        f'read -p "Press Enter to close..."; exit $R'
    )

    named      = {name: builder for name, builder in _EXT_CANDIDATES}
    candidates = []
    pref = _terminal_preference()
    if pref:
        candidates.append((pref, named.get(pref) or _generic_builder(pref)))
    candidates.extend(_EXT_CANDIDATES)
    env_term = os.environ.get("TERMINAL", "").strip()
    if env_term and env_term != pref:
        candidates.append((env_term, _generic_builder(env_term)))

    tried = []
    for name, builder in candidates:
        cmd, kwargs = builder(cmd_str, cwd)
        tried.append(name)
        try:
            proc = subprocess.Popen(
                cmd, start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs
            )
            with _lock:
                e.ext_proc = proc
            # The wrapper records the script's real shell PID almost immediately;
            # pick it up so the entry PID points at the process running the script.
            script_pid = None
            for _ in range(10):
                if not pid_file.exists():
                    time.sleep(0.1)
                    continue
                try: script_pid = int(pid_file.read_text().strip())
                except Exception: pass
                break
            shown = script_pid or proc.pid
            _append_log(e,
                f"[MAESTRO] ↗ {label} in external terminal — PID {shown} ({name})")
            _mlog(f"[{e.display_name()}] ↗ {label}: {name} PID {proc.pid}")
            return proc
        except FileNotFoundError:
            continue
        except Exception as ex:
            _mlog(f"[{e.display_name()}] ✕ External {label} ({cmd[0]}): {ex}")
            return None

    _append_log(
        e,
        "[MAESTRO] ✕ No terminal emulator found (tried: " + ", ".join(tried) + "). "
        "Set the 'terminal' key in settings.json (Settings → Server) or export "
        "MAESTRO_TERMINAL, or install one of the terminals tried."
    )
    _mlog(f"[{e.display_name()}] ✕ No terminal emulator found (tried: {', '.join(tried)})")
    return None


# ── Launchable file types ─────────────────────────────────────────────────────
# Anything that can be added as an entry besides plain shell scripts. Used by the
# file browser filter (main.py) and by _resolve_command() below.
LAUNCHABLE_EXTS = {
    ".sh", ".bash",                    # shell scripts (bash)
    ".appimage", ".run", ".bin",       # Linux executables
    ".py", ".jar",                     # interpreters
    ".exe", ".com", ".bat", ".cmd", ".ps1",   # Windows
}


def _is_elf(path: str) -> bool:
    """True if the file starts with the ELF magic (native Linux executable, AppImage, ...)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _resolve_command(path: str):
    """
    Decide how to run an entry from its file type.
    Returns (argv, needs_exec, error): `needs_exec` means the file is launched
    directly and must have the executable bit (see _ensure_exec); `error` is a
    human-readable reason when the file cannot be run on this platform.
    Backward compatible: anything unrecognised still runs through bash, as every
    entry did before this resolver existed.
    """
    ext = Path(path).suffix.lower()
    if _WIN:
        if ext in (".exe", ".com"):  return [path], False, None
        if ext in (".bat", ".cmd"):  return ["cmd", "/c", path], False, None
        if ext == ".ps1":
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], False, None
        if ext == ".py":             return [sys.executable, path], False, None
        if ext == ".jar":            return ["java", "-jar", path], False, None
        if ext == ".appimage":       return None, False, "AppImage files only run on Linux"
        bash = _get_bash_win()
        if not bash:
            return None, False, "bash not found (install WSL or Git Bash to run .sh scripts)"
        return [bash, path], False, None
    # Linux
    if ext == ".py":
        return [sys.executable, path], False, None
    if ext == ".jar":
        java = shutil.which("java")
        return ([java, "-jar", path], False, None) if java else (None, False, "java not found in PATH")
    if ext in (".exe", ".com"):
        wine = shutil.which("wine")
        return ([wine, path], False, None) if wine else (None, False, "Windows executable: wine is not installed")
    if ext in (".bat", ".cmd", ".ps1"):
        return None, False, f"{ext} files only run on Windows"
    if _is_elf(path):                       # AppImage, .run, .bin, extension-less binaries
        return [path], True, None
    return ["bash", path], False, None


def _ensure_exec(path: str):
    """Make sure `path` has the executable bit (downloaded AppImages usually don't). Returns (ok, note)."""
    if os.access(path, os.X_OK):
        return True, ""
    try:
        os.chmod(path, os.stat(path).st_mode | 0o100)   # u+x
        return True, "File was not executable — applied chmod u+x"
    except OSError as ex:
        return False, str(ex)


def _shq(arg: str) -> str:
    """Double-quote one argument for the 'bash -c' strings of the external terminals."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


# ── Process management ─────────────────────────────────────────────────────────
def _kill_proc(e):
    """Send a termination signal to the internal subprocess."""
    if not (e.proc and e.proc.poll() is None):
        return
    if _WIN:
        try:
            e.proc.terminate()   # calls TerminateProcess on Windows
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(e.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:    e.proc.terminate()
            except Exception: pass


def launch(idx: int, *, _from_restart: bool = False):
    """
    Launch the entry at index idx. Routes to _launch_external() if open_outside=True.
    Refuses silently if the entry is unreachable (script file missing).
    Thread-safe. Spawns reader + monitor threads internally.
    """
    with _lock:
        if idx < 0 or idx >= len(entries):
            return
        e = entries[idx]
    if e.status == "SEP":
        return

    # Refuse launch if script file is missing
    if e.unreachable:
        _mlog(f"[{e.display_name()}] ✕ Launch refused — script not found: {e.path}")
        return

    with _lock:
        if e.proc and e.proc.poll() is None:
            e.manually_killed = True
            _kill_proc(e)
        _kill_ext(e)
        e.crash           = False
        e.manually_killed = False
        e.generation     += 1
        gen               = e.generation

    if e.delay > 0 and not _from_restart:
        time.sleep(e.delay)

    # Abort if kill arrived during the delay
    if _stop.is_set() or e.manually_killed:
        return

    if e.open_outside:
        _launch_external(e, gen)
        return

    # Resolve the command from the file type (.sh, .AppImage, .exe, ...)
    proc_cmd, needs_exec, err = _resolve_command(e.path)
    if err:
        _mlog_event(e, f"[MAESTRO] ✕ Launch failed: {err}")
        return
    if needs_exec:
        ok, note = _ensure_exec(e.path)
        if not ok:
            _mlog_event(e, f"[MAESTRO] ✕ Launch failed: not executable and chmod failed ({note})")
            return
        if note:
            _append_log(e, f"[MAESTRO] {note}")

    # Platform-specific Popen kwargs
    if _WIN:
        proc_kw  = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,   # raw bytes: '\r' must reach _pump_output untouched
            cwd=str(Path(e.path).parent),
            creationflags=_CF_NO_WINDOW,   # suppress console flash on Windows
        )
    else:
        proc_kw  = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            bufsize=0,   # raw bytes: '\r' must reach _pump_output untouched
            cwd=str(Path(e.path).parent),
        )

    try:
        proc = subprocess.Popen(proc_cmd, **proc_kw)
    except Exception as ex:
        _mlog_event(e, f"[MAESTRO] ✕ Launch failed: {ex}")
        return

    with _lock:
        # Check again — kill may have arrived between Popen and here
        if e.manually_killed or e.generation != gen:
            try:
                if _WIN:
                    proc.terminate()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            return
        e.proc = proc
        e.pid  = proc.pid
        save_config()

    _set_partial(e, "")
    _append_log(e, f"[MAESTRO] ▶ Started — PID {proc.pid}")
    _mlog(f"[{e.display_name()}] ▶ Started — PID {proc.pid}")
    threading.Thread(target=_reader,  args=(e, gen), daemon=True).start()
    threading.Thread(target=_monitor, args=(e, gen), daemon=True).start()


def _launch_external(e, gen: int):
    proc = _run_in_external_terminal(e, e.path, "Launch")
    if not proc:
        return
    with _lock:
        if e.manually_killed or e.generation != gen:
            _kill_ext(e)
            return
        # Prefer the script's own shell PID recorded by the wrapper, when it
        # has already appeared (see _run_in_external_terminal).
        script_pid = None
        try:
            if e.ext_pid_file and e.ext_pid_file.exists():
                script_pid = int(e.ext_pid_file.read_text().strip())
        except Exception:
            pass
        e.pid = script_pid or proc.pid
        save_config()
    threading.Thread(target=_monitor_external, args=(e, proc, gen), daemon=True).start()


def _reader(e, gen: int):
    """Read stdout/stderr from the internal subprocess and push lines to SSE."""
    # Fix #14: snapshot e.proc under the lock rather than reading the
    # attribute directly from a background thread with no synchronization.
    with _lock:
        proc = e.proc
    if proc is None:
        return
    try:
        _pump_output(e, gen, proc)
    except Exception:
        pass


def _monitor(e, gen: int):
    """Wait for the internal subprocess to exit. Handles crash, close_panel, auto-restart."""
    with _lock:
        proc = e.proc
    if proc is None:
        return
    try:    proc.wait()
    except Exception: pass
    if e.generation != gen:
        return
    rc = proc.returncode
    with _lock:
        e.pid   = None
        e.crash = False
        save_config()
    if e.manually_killed:
        _append_log(e, f"[MAESTRO] ■ Stopped (exit {rc})")
        _mlog(f"[{e.display_name()}] ■ Stopped (exit {rc})")
        return
    if rc == 0:
        # A clean exit is not a crash: the program simply finished (utilities,
        # installers, an AppImage the user quit). No crash styling, no auto-restart.
        _mlog_event(e, "[MAESTRO] ■ Finished (exit 0)")
        _pub(e, "finished", "0")
        if not e.keep_open:
            _pub(e, "close_panel", "")
        return
    e.crash = True
    _mlog_event(e, f"[MAESTRO] ⚠ Crashed — exit {rc}")
    _pub(e, "crash", str(rc))
    if not e.keep_open:
        _pub(e, "close_panel", "")
    if e.auto_restart and e.status == "ON" and not _stop.is_set():
        _mlog_event(e, f"[MAESTRO] ↺ Auto-restart in {CRASH_WAIT:.0f}s…")
        _stop.wait(CRASH_WAIT)
        if _stop.is_set() or e.status != "ON":
            return
        _pub(e, "restart", "")
        with _lock:
            try:   idx = entries.index(e)
            except ValueError: return
        launch(idx, _from_restart=True)


def _monitor_external(e, proc, gen: int):
    """
    Wait for an external terminal run to finish and publish the exit state.

    Two shapes behave differently:
      - Hosting terminals (ptyxis -s, xterm, ...): the Popen child lives for
        the whole command, so its exit is the completion signal.
      - Client-to-instance terminals (ptyxis when an instance is already
        running): the CLI exits 0 immediately while the window keeps running
        the command elsewhere — its returncode says nothing about the command.

    The wrapper is ground truth whenever it can run: it records the script's
    PID and writes the rc-file when the command ends (an EXIT trap also fires
    if the window is closed or the shell is killed mid-run). So the rc-file
    wins over the CLI exit code, and we keep waiting for it while the wrapper
    is live.
    """
    pid_file, rc_file = e.ext_pid_file, e.ext_rc_file
    if pid_file is None:
        try:    proc.wait()
        except Exception: pass
        rc = proc.returncode if proc.returncode is not None else "?"
    else:
        rc = None
        while e.generation == gen and not _stop.is_set() and not rc_file.exists():
            if e.manually_killed:
                break
            try:
                # Adopt the script's real shell PID once the wrapper wrote it.
                if pid_file.exists() and e.pid == proc.pid:
                    sp = int(pid_file.read_text().strip())
                    with _lock:
                        if e.generation == gen: e.pid = sp
            except Exception:
                pass
            if proc.poll() is not None:
                # CLI exited. If the wrapper is actually running in a window,
                # the command may still be in progress: wait for its rc-file.
                try: live = pid_file.exists()
                except Exception: live = False
                end = time.time() + (600 if live else 5)
                while (time.time() < end and e.generation == gen
                       and not _stop.is_set() and not rc_file.exists()):
                    if e.manually_killed:
                        break
                    time.sleep(0.2)
                break
            time.sleep(0.2)
        # A kill can race with the trap's rc write: give it one last moment.
        deadline = time.time() + 1.0
        while not rc_file.exists() and time.time() < deadline:
            time.sleep(0.1)
        try:
            if rc_file.exists():
                rc = int(rc_file.read_text().strip())
        except Exception:
            pass
        if rc is None:
            rc = proc.returncode if proc.returncode is not None else "?"
    if e.generation != gen:
        return
    with _lock:
        e.pid   = None
        e.crash = False
        if e.ext_proc is proc:
            e.ext_proc = None
        save_config()
    rc = proc.returncode if proc.returncode is not None else "?"
    _append_log(e, f"[MAESTRO] ■ External terminal closed (exit {rc})")
    _mlog(f"[{e.display_name()}] ■ External closed (exit {rc})")
    # Always fire an event so the frontend poll triggers and the sidebar updates;
    # exit 0 is a normal finish, anything else keeps the crash styling.
    _pub(e, "finished" if rc == 0 else "crash", str(rc))
    if not e.keep_open:
        _pub(e, "close_panel", "")


def _monitor_update_ext(e, proc):
    """Wait for an update script terminal to exit and log the result."""
    try:    proc.wait()
    except Exception: pass
    if e.ext_proc is proc:
        e.ext_proc = None
    rc = proc.returncode if proc.returncode is not None else "?"
    _append_log(e, f"[UPDATE] ■ External terminal closed (exit {rc})")
    _mlog(f"[{e.display_name()}] [UPDATE] ■ External closed")


def kill_entry(idx: int):
    """Kill the process (internal + external) at the given index."""
    with _lock:
        if idx < 0 or idx >= len(entries):
            return
        e = entries[idx]
        e.manually_killed = True
        _kill_proc(e)
        _kill_ext(e)
        # For open_outside scripts, also kill the script PID directly —
        # the terminal emulator may not propagate the signal to the child.
        # NOTE (Fix #12, documented limitation): for several terminal
        # emulators (notably gnome-terminal), e.pid is the terminal's PID,
        # not necessarily the script's — some emulators fork into a
        # pre-existing server process and return immediately, so this kill
        # can target a dead or unrelated PID while the real script keeps
        # running. No clean fix exists across all six supported emulators
        # without materially more complexity; this is a best-effort kill.
        if e.open_outside and e.pid is not None:
            if _WIN:
                try:
                    h = ctypes.windll.kernel32.OpenProcess(
                        _PROC_TERMINATE, False, e.pid
                    )
                    if h:
                        ctypes.windll.kernel32.TerminateProcess(h, 1)
                        ctypes.windll.kernel32.CloseHandle(h)
                except Exception:
                    pass
            else:
                try:
                    os.killpg(os.getpgid(e.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:    os.kill(e.pid, signal.SIGTERM)
                    except Exception: pass
        e.pid   = None
        e.crash = False
        save_config()


def kill_all():
    """Kill every running process (internal + external) across all entries."""
    with _lock:
        for e in entries:
            if e.proc and e.proc.poll() is None:
                e.manually_killed = True
                _kill_proc(e)
            _kill_ext(e)
            e.pid   = None
            e.crash = False
        save_config()


def _watch_existing_pid(e):
    """
    Poll every 5s for a PID that was already running when Maestro started.
    Fires a crash event if the PID disappears.
    """
    pid = e.pid
    _mlog_event(e, f"[MAESTRO] ⚠ Reconnected to existing PID {pid}")
    while not _stop.is_set():
        if e.pid != pid:
            return
        if not _pid_alive(pid):
            with _lock:
                if e.pid == pid:
                    e.pid   = None
                    e.crash = True
                    save_config()
            _mlog_event(e, f"[MAESTRO] ⚠ PID {pid} vanished")
            _pub(e, "crash", "?")
            return
        _stop.wait(5.0)


# ── Metrics loop ───────────────────────────────────────────────────────────────
def _metrics_loop():
    """
    Background daemon thread. Runs every 2s:
      - checks entry path reachability (unreachable state transitions)
      - updates per-process CPU/RAM metrics
    """
    while not _stop.wait(2.0):
        _check_paths()
        with _lock:
            snap = list(entries)
        for e in snap:
            if e.status != "SEP":
                _update_metrics(e)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _find_by_uid(uid: str) -> tuple:
    """Return (index, ProcessEntry) for the given uid, or (None, None) if not found."""
    for i, e in enumerate(entries):
        if e.uid == uid:
            return i, e
    return None, None
