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
import collections, json, os, queue, re, shutil
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

try:    CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
except: CLK_TCK = 100
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

    class _MSX(ctypes.Structure):
        """MEMORYSTATUSEX (sysinfoapi.h) — system-wide RAM."""
        _fields_ = [
            ('dwLength',                ctypes.wintypes.DWORD),
            ('dwMemoryLoad',            ctypes.wintypes.DWORD),
            ('ullTotalPhys',            ctypes.c_uint64),
            ('ullAvailPhys',            ctypes.c_uint64),
            ('ullTotalPageFile',        ctypes.c_uint64),
            ('ullAvailPageFile',        ctypes.c_uint64),
            ('ullTotalVirtual',         ctypes.c_uint64),
            ('ullAvailVirtual',         ctypes.c_uint64),
            ('ullAvailExtendedVirtual', ctypes.c_uint64),
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

_sys_cpu       = 0.0
_prev_cpu_stat = None   # platform-specific: (total, idle) on Linux; (idle_t, total_t) on Windows

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
            for i in range(LOG_KEEP, 0, -1):
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
            except: dead.append(q)
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
        self.crash           = False
        self.manually_killed = False
        self.generation      = 0          # incremented on each launch to invalidate old threads
        self.unreachable     = False      # True when self.path does not exist on disk

        # SSE / metrics
        self.subs_lock = threading.Lock()
        self.log_buf   = collections.deque(maxlen=BUF_LINES)
        self.subs      = []               # list[queue.Queue] — live SSE clients
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
    except: return d


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
        path  = p[1].strip()  if len(p) > 1  else ""
        pid_s = p[2].strip()  if len(p) > 2  else ""
        delay = _int(p[3])    if len(p) > 3  else 0
        ar    = (_int(p[4]) != 0) if len(p) > 4  else False
        ko    = (_int(p[5]) != 0) if len(p) > 5  else True
        name  = p[6].strip()  if len(p) > 6  else ""
        lh    = (_int(p[8]) != 0) if len(p) > 8  else False
        wb    = _parse_webapps(p[9].strip()  if len(p) > 9  else "")
        us    = p[10].strip() if len(p) > 10 else ""
        sf    = p[11].strip() if len(p) > 11 else ""
        inf   = p[12].strip() if len(p) > 12 else ""
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
                        f"SEP|{e.path}|||0|1|{e.name}|0|0|[]"
                        f"||{e.sep_file}|{e.info_file}|0|default\n"
                    )
                else:
                    ar = 1 if e.auto_restart  else 0
                    ko = 1 if e.keep_open     else 0
                    lh = 1 if e.launch_hidden else 0
                    oo = 1 if e.open_outside  else 0
                    wb = json.dumps(e.webapps, separators=(',', ':'))
                    tc = e.term_color if e.term_color in VALID_COLORS else "default"
                    f.write(
                        f"{e.status}|{e.path}|{ps}|{e.delay}|{ar}|{ko}"
                        f"|{e.name}|0|{lh}|{wb}|{e.update_script}"
                        f"|{e.sep_file}|{e.info_file}|{oo}|{tc}\n"
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
            except: dead.append(q)
        for q in dead:
            e.subs.remove(q)
    _write_log(e.display_name(), line)


def _pub(e, event: str, data: str):
    """Broadcast a non-log SSE event to all live subscribers of entry e."""
    with e.subs_lock:
        for q in e.subs:
            try: q.put_nowait((event, data))
            except: pass


def _close_subs(e):
    """Send the sentinel None to all subscribers so their SSE loops exit."""
    with e.subs_lock:
        for q in e.subs:
            try: q.put_nowait(None)
            except: pass


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
        e.metrics   = {"cpu": 0.0, "ram": 0}
        e._cpu_prev = None
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _read_sys_cpu():
    """Update _sys_cpu global from system-wide CPU delta."""
    global _sys_cpu, _prev_cpu_stat
    if _WIN:
        try:
            idle = _FILETIME(); kernel = _FILETIME(); user = _FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            idle_t  = _ft_int(idle)
            total_t = _ft_int(kernel) + _ft_int(user)
            if _prev_cpu_stat is not None:
                pi, pt  = _prev_cpu_stat
                d_idle  = idle_t  - pi
                d_total = total_t - pt
                if d_total > 0:
                    _sys_cpu = round((1.0 - d_idle / d_total) * 100, 1)
            _prev_cpu_stat = (idle_t, total_t)
        except Exception:
            pass
        return
    # Linux: /proc/stat delta
    try:
        line  = Path("/proc/stat").read_text().splitlines()[0]
        vals  = list(map(int, line.split()[1:]))
        total = sum(vals)
        idle  = vals[3]
        if _prev_cpu_stat is None:
            _prev_cpu_stat = (total, idle)
            return
        pt, pi = _prev_cpu_stat
        dt = total - pt
        di = idle  - pi
        _prev_cpu_stat = (total, idle)
        if dt > 0:
            _sys_cpu = round((1.0 - di / dt) * 100, 1)
    except Exception:
        pass


def _read_sys_ram() -> tuple:
    """Return (used_mb, total_mb) for system RAM."""
    if _WIN:
        try:
            stat = _MSX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_mb = stat.ullTotalPhys >> 20
            avail_mb = stat.ullAvailPhys >> 20
            return total_mb - avail_mb, total_mb
        except Exception:
            return 0, 0
    # Linux: /proc/meminfo
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            try:
                info[k.strip()] = int(v.split()[0]) // 1024
            except Exception:
                pass
        total = info.get("MemTotal",     0)
        avail = info.get("MemAvailable", 0)
        return total - avail, total
    except Exception:
        return 0, 0


def _read_gpu() -> dict:
    """AMD GPU stats from Linux sysfs. Returns zeros on Windows or on failure."""
    if _WIN:
        return {"gpu": 0, "vramUsed": 0, "vramTotal": 0}
    try:
        for card in sorted(Path("/sys/class/drm").iterdir()):
            if not card.name.startswith("card") or "_" in card.name:
                continue
            dev = card / "device"
            try:
                gpu = int((dev / "gpu_busy_percent").read_text().strip())
                vu  = int((dev / "mem_info_vram_used").read_text().strip())  >> 20
                vt  = int((dev / "mem_info_vram_total").read_text().strip()) >> 20
                return {"gpu": gpu, "vramUsed": vu, "vramTotal": vt}
            except (FileNotFoundError, ValueError, PermissionError):
                continue
    except Exception:
        pass
    return {"gpu": 0, "vramUsed": 0, "vramTotal": 0}


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
# Linux terminal candidates (tried in order)
_EXT_CANDIDATES = [
    lambda s, cwd: (["gnome-terminal", "--wait", "--", "bash", "-c", s],   {"cwd": cwd}),
    lambda s, cwd: (["xfce4-terminal", "--hold", "-e", f"bash -c '{s}'"],  {"cwd": cwd}),
    lambda s, cwd: (["xterm", "-hold", "-e", f"bash -c '{s}'"],            {"cwd": cwd}),
    lambda s, cwd: (["konsole", "--hold", "-e", "bash", "-c", s],          {"cwd": cwd}),
    lambda s, cwd: (["lxterminal", "-e", f"bash -c '{s}'"],                {"cwd": cwd}),
    lambda s, cwd: (["mate-terminal", "--window", "-e", f"bash -c '{s}'"], {"cwd": cwd}),
]


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
                except: pass
        threading.Thread(target=_force, daemon=True).start()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:    proc.terminate()
            except: pass

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
    bash = _get_bash_win()
    if not bash:
        _append_log(e,
            "[MAESTRO] ✕ No bash found — install WSL or Git Bash to launch scripts")
        _mlog(f"[{e.display_name()}] ✕ No bash found (WSL / Git Bash required)")
        return None

    tail    = 'echo ""; echo "--- Done (exit: $?) ---"; read -p "Press Enter to close..."'
    cmd_str = f'bash "{script_path}"; {tail}'
    cwd     = str(Path(script_path).parent)

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
    tail    = 'echo ""; echo "--- Done (exit: $?) ---"; read -p "Press Enter to close..."'
    cmd_str = f'bash "{script_path}"; {tail}'
    cwd     = str(Path(script_path).parent)
    for builder in _EXT_CANDIDATES:
        cmd, kwargs = builder(cmd_str, cwd)
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
            _append_log(e,
                f"[MAESTRO] ↗ {label} in external terminal — PID {proc.pid} ({cmd[0]})")
            _mlog(f"[{e.display_name()}] ↗ {label}: {cmd[0]} PID {proc.pid}")
            return proc
        except FileNotFoundError:
            continue
        except Exception as ex:
            _mlog(f"[{e.display_name()}] ✕ External {label} ({cmd[0]}): {ex}")
            return None
    _append_log(
        e,
        "[MAESTRO] ✕ No terminal emulator found "
        "(tried: gnome-terminal, xfce4-terminal, xterm, konsole, lxterminal, mate-terminal)"
    )
    _mlog(f"[{e.display_name()}] ✕ No terminal emulator found")
    return None


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
            except: pass


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

    # Resolve bash and build platform-specific Popen kwargs
    if _WIN:
        bash = _get_bash_win()
        if not bash:
            _mlog_event(e,
                "[MAESTRO] ✕ Launch failed — bash not found (install WSL or Git Bash)")
            return
        proc_cmd = [bash, e.path]
        proc_kw  = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, bufsize=1,
            cwd=str(Path(e.path).parent),
            errors='replace',
            creationflags=_CF_NO_WINDOW,   # suppress console flash on Windows
        )
    else:
        proc_cmd = ['bash', e.path]
        proc_kw  = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            text=True, bufsize=1,
            cwd=str(Path(e.path).parent),
            errors='replace',
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
        e.pid = proc.pid
        save_config()
    threading.Thread(target=_monitor_external, args=(e, proc, gen), daemon=True).start()


def _reader(e, gen: int):
    """Read stdout/stderr from the internal subprocess and push lines to SSE."""
    try:
        for line in e.proc.stdout:
            if e.generation != gen:
                break
            _append_log(e, line.rstrip("\n"))
    except Exception:
        pass


def _monitor(e, gen: int):
    """Wait for the internal subprocess to exit. Handles crash, close_panel, auto-restart."""
    try:    e.proc.wait()
    except: pass
    if e.generation != gen:
        return
    rc = e.proc.returncode
    with _lock:
        e.pid   = None
        e.crash = False
        save_config()
    if e.manually_killed:
        _append_log(e, f"[MAESTRO] ■ Stopped (exit {rc})")
        _mlog(f"[{e.display_name()}] ■ Stopped (exit {rc})")
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
    Wait for the external terminal process to exit.
    Updates e.pid and fires SSE events so the UI stays in sync.
    """
    try:    proc.wait()
    except: pass
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
    # Always fire crash event so the frontend poll triggers and sidebar updates
    _pub(e, "crash", str(rc))
    if not e.keep_open:
        _pub(e, "close_panel", "")


def _monitor_update_ext(e, proc):
    """Wait for an update script terminal to exit and log the result."""
    try:    proc.wait()
    except: pass
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
                    except: pass
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
      - updates system-wide CPU
      - checks entry path reachability (unreachable state transitions)
      - updates per-process CPU/RAM metrics
    """
    while not _stop.wait(2.0):
        _read_sys_cpu()
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
