# Maestro v2.0 — Developer Reference
> `V2_0_MAESTRO_GUIDE.md`

> Privacy-first, localhost-only process orchestrator for Linux.
> Pure Python stdlib backend. Vanilla JS frontend. Zero npm, zero pip, zero CDN.

---

## ── AI BOOTSTRAP (read this first in a new chat) ────────────────────────────

### What is Maestro?
Maestro is a personal process orchestrator running on HYPERION (Fedora GNOME).
It manages a list of shell scripts, launches them, streams their output via SSE,
and exposes a browser UI at `http://127.0.0.1:8865`.

### Current stable version: v2.0
Three-file Python backend (tray.py + main.py + daemon.py). Flat JS frontend.
No build step, no venv, no npm.

### File locations
```
Source repo (git, never run directly):
/disks/linux/EXT4/ENVIRONMENTS/AI/Projects/Maestro/
├── tray.py                  ← GTK tray daemon (entry point)
├── main.py                  ← HTTP server + UI layer
├── daemon.py                ← process manager, SSE broker, metrics
├── ui.html                  ← frontend shell (structure + CSS)
├── ui.js                    ← all frontend JS
├── webui.js                 ← WebApp iframe lifecycle
├── start.sh                 ← entry point: launches tray.py
├── install.sh               ← one-shot installer
├── img/
│   ├── MaestroLogo.svg
│   └── MaestroLogo24x24.png
├── README.md
├── V1_4_MAESTRO_GUIDE.md   ← previous version reference (keep for history)
└── V2_0_MAESTRO_GUIDE.md   ← this file

Runtime (deployed from GitHub via install.sh):
/disks/linux/EXT4/ENVIRONMENTS/Maestro/
├── bin/                     ← copy of source files
└── data/
    ├── settings.json
    ├── config.txt
    └── logs/
        └── maestro.log
```

### How to start
```bash
bash /disks/linux/EXT4/ENVIRONMENTS/Maestro/bin/start.sh
# or from source repo (dev):
bash /disks/linux/EXT4/ENVIRONMENTS/AI/Projects/Maestro/start.sh
```
`start.sh` launches `tray.py --bin-dir <dir>`, which spawns `main.py --data-dir <dir>`.
Maestro writes a PID lock to `/tmp/MaestroWeb.pid` — only one instance at a time.

### Key coding rules
- Pure Python 3.11+ stdlib only in `*.py` — no pip installs
- Vanilla JS only in `ui.js` / `ui.html` / `webui.js` — no npm, no CDN, no frameworks
- camelCase in JS; snake_case in Python; English comments
- Never hardcode absolute paths in source — paths via `--bin-dir`, `--data-dir`, or `settings.json`
- Always provide complete files when modifying existing ones
- Always specify save path in the response and as a comment at top of file
- All process actions (launch, kill) use `uid` not `idx` in API calls
- `renderGrid()` must be called BEFORE `connectSSE()` so panel DOM exists when log events arrive
- DOM ref constants (`fQ`, `fUrl`, `fT`) declared before first `poll()` call
- `daemon.init(base_dir, data_dir)` must be the first call in `main.py`

### Known gotchas
- `_find_by_uid()` is a module-level function in `daemon.py`, referenced as `daemon._find_by_uid()` from `main.py`
- `_applyExplorerSetting()` is defined after `_fetchSettings()` in `ui.js` — safe because both are hoisted `function` declarations; no synchronous top-level call exists
- Cairo `Gdk_cairo_set_source_pixbuf()` in `tray.py` uses a ctypes fallback to a PNG buffer if the GDK native call fails — this is intentional and safe
- `_check_paths()` in `daemon.py` runs every 2s inside `_metrics_loop()` — lightweight `os.path.isfile()` only, no I/O
- `daemon._logging_enabled` is kept in sync with `settings.json["logsRecorder"]` by both `main.py` at startup and `POST /api/settings`
- After any change to `ui.js`, hard refresh (Ctrl+Shift+R) to clear cached JS

---

## Hardware / OS Context

- **Machine**: HYPERION — Fedora GNOME, AMD Ryzen 7 5800X, RX 6800 XT, 64GB RAM
- **Install path**: `/disks/linux/EXT4/ENVIRONMENTS/Maestro/`
- **Source repo**: `/disks/linux/EXT4/ENVIRONMENTS/AI/Projects/Maestro/`
- **Python**: 3.11+ (system Python, no venv)
- **Port**: `127.0.0.1:8865` (default, configurable in settings.json)
- **PID lock**: `/tmp/MaestroWeb.pid`

---

## Architecture

### v2.0 (current)
```
start.sh
  └─ tray.py  (GTK daemon, entry point, watchdog)
        ├─ spawns ──► main.py  (HTTP server, UI layer, API)
        │                └─ imports ──► daemon.py  (process manager, SSE, metrics)
        ├─ health check GET /api/state every 5s
        └─ SSE listener /api/logs/__maestro__ → notify-send
```

### Responsibility split

| File | Owns |
|------|------|
| `tray.py` | GTK loop, system tray icon, health check, notifications, autostart, watchdog |
| `main.py` | HTTP routing, serve UI files, settings.json read/write, single-instance lock |
| `daemon.py` | All process state: entries[], launch, kill, SSE pub/sub, metrics, config I/O, unreachable detection |
| `ui.html` | Layout, CSS, modal HTML, static structure |
| `ui.js` | All frontend logic: poll, render, SSE client, settings modal, explorer toggle |
| `webui.js` | WebApp iframe lifecycle only (tabIframes, suspend/resume, ping) |

### Thread model
- **GTK main thread**: `Gtk.main()` in `tray.py`
- **tray health loop**: daemon thread, polls `/api/state` every 5s
- **tray SSE loop**: daemon thread, streams `/api/logs/__maestro__`
- **main.py main thread**: `HTTPServer.serve_forever()`
- **Per-request thread**: `ThreadingHTTPServer` spawns one per HTTP connection
- **SSE threads**: each SSE client holds a thread (blocks on `queue.get(timeout=5)`)
- **Reader threads**: one per running internal process (reads stdout/stderr pipe)
- **Monitor threads**: one per process (waits for exit, handles crash/restart)
- **External monitor threads**: one per `open_outside` process
- **Metrics loop**: single daemon thread in `daemon.py`, every 2s
- **Locking**: `daemon._lock = threading.RLock()` guards `entries[]`; per-entry `subs_lock` guards `log_buf` and `subs[]`

---

## Data directory resolution

Three-layer priority — identical logic in both `tray.py` and `main.py`:

1. `MAESTRO_DATA` environment variable (highest priority)
2. `dataDir` field in `settings.json` at the default location (if non-empty)
3. Default: `BIN_DIR/../data/` (created on first run)

`main.py` also accepts `--data-dir <path>` as a CLI argument (passed by `tray.py`).

---

## File Structure

```
bin/
├── tray.py        620 lines
├── main.py        739 lines
├── daemon.py      808 lines
├── ui.html        ~430 lines
├── ui.js         1211 lines
├── webui.js        138 lines
├── start.sh          9 lines
├── install.sh      ~220 lines
└── img/
    ├── MaestroLogo.svg
    └── MaestroLogo24x24.png

data/
├── settings.json
├── config.txt
└── logs/
    ├── maestro.log       (active, max 5MB)
    ├── maestro.1.log     (rotation backup 1)
    └── maestro.2.log     (rotation backup 2)
```

---

## Config format (config.txt)

One entry per line, pipe-delimited, 15 fields (0-indexed). Backward-compatible.

```
STATUS|PATH|PID|DELAY|AR|KO|NAME|_|LH|WEBAPPS_JSON|UPDATE_SCRIPT|SEP_FILE|INFO_FILE|OPEN_OUTSIDE|TERM_COLOR
```

| # | Field | Type | Description |
|---|-------|------|-------------|
| 0 | STATUS | `ON`/`OFF`/`SEP` | Favourite flag / separator |
| 1 | PATH | string | Script path (or separator label for SEP) |
| 2 | PID | int or empty | Last known PID (validated via `/proc` at load) |
| 3 | DELAY | int | Launch delay in seconds |
| 4 | AR | `0`/`1` | Auto-restart on crash |
| 5 | KO | `0`/`1` | Keep panel open on crash |
| 6 | NAME | string | Custom display name |
| 7 | _ | — | Legacy (ignored) |
| 8 | LH | `0`/`1` | Launch hidden |
| 9 | WEBAPPS_JSON | JSON | `[{"url":"http://127.0.0.1:8188"}]` |
| 10 | UPDATE_SCRIPT | string | Custom update script path |
| 11 | SEP_FILE | string | Separator attached text file |
| 12 | INFO_FILE | string | Read-only `.md` info panel |
| 13 | OPEN_OUTSIDE | `0`/`1` | Launch in OS terminal emulator |
| 14 | TERM_COLOR | string | `default`/`navy`/`forest`/`plum`/`teal`/`ember` |

---

## settings.json schema

```json
{
  "fileExplorer":        false,
  "logsRecorder":        false,
  "openBrowserAtLaunch": false,
  "autostart":           false,
  "trayPersistent":      true,
  "host":                "127.0.0.1",
  "port":                8865,
  "dataDir":             ""
}
```

| Key | Default | Side effects on change |
|-----|---------|----------------------|
| `fileExplorer` | false | `_applyExplorerSetting()` in ui.js shows/hides tree + explorer panel |
| `logsRecorder` | false | `daemon._logging_enabled` synced immediately |
| `openBrowserAtLaunch` | false | `tray.py` reads at startup; takes effect on next start |
| `autostart` | false | `tray.py` creates/removes `~/.config/autostart/maestro.desktop` |
| `trayPersistent` | true | `tray.py` reads on shutdown: if false, exits GTK main loop |
| `host` | 127.0.0.1 | Requires restart — `main.py` reads at boot only |
| `port` | 8865 | Requires restart — `main.py` reads at boot only |
| `dataDir` | "" | Migration handled by user; tray.py reads at next boot |

---

## API Reference

### GET endpoints

| Path | Query | Response |
|------|-------|----------|
| `/` | — | Serves `ui.html` |
| `/ui.js` | — | Serves `ui.js` |
| `/webui.js` | — | Serves `webui.js` |
| `/img/<file>` | — | Serves from `img/` dir |
| `/api/state` | — | `{entries, loggingEnabled, gpu, sysCpu, sysRam, sysRamTotal}` |
| `/api/logs/<uid>` | — | SSE stream |
| `/api/browse` | `path=`, `all=0/1`, `md=0/1` | `{path, parent, dirs[], files[]}` |
| `/api/read-file` | `path=` | `{path, name, content, ext}` or `{error}` |
| `/api/settings` | — | Returns `settings.json` contents |

### POST endpoints

| Path | Body | Effect |
|------|------|--------|
| `/api/launch` | `{uid}` or `{idx}` | Launch script (prefer uid) |
| `/api/kill` | `{uid}` or `{idx}` | Kill process + ext terminal |
| `/api/toggle` | `{idx}` | Toggle ON/OFF favourite |
| `/api/add` | `{path, name?, launchHidden?, webapps?, updateScript?, infoFile?}` | Add entry |
| `/api/remove` | `{idx}` | Kill + remove entry |
| `/api/reorder` | `{a, b}` | Swap entries at indices a and b |
| `/api/add-sep` | `{name, after, sepFile?}` | Insert separator |
| `/api/update-entry` | `{idx, name?, delay?, autoRestart?, keepOpen?, launchHidden?, webapps?, updateScript?, sepFile?, infoFile?, status?, openOutside?, termColor?}` | Update fields |
| `/api/update-script` | `{uid}` | Run update script in OS terminal |
| `/api/clear-log` | `{uid}` | Clear log buffer |
| `/api/write-file` | `{path, content}` | Write text file |
| `/api/open-editor` | `{path}` | Launch text editor subprocess |
| `/api/open-app` | `{app, path}` | Launch app subprocess |
| `/api/toggle-logging` | `{}` | Legacy toggle (kept for compat) — prefer `/api/settings` |
| `/api/settings` | `{key: value, ...}` | Update settings, persist to settings.json |
| `/api/shutdown` | `{}` | Kill all, stop server |

### SSE Events (`/api/logs/<uid>`)

| Event | Data | Description |
|-------|------|-------------|
| `log` | string | Log line (ANSI stripped) |
| `clear` | empty | Reset terminal |
| `crash` | exit code | Process exited unexpectedly |
| `close_panel` | empty | Close panel (keepOpen=false) |
| `restart` | empty | Auto-restart starting |

Special UIDs: `__maestro__` → Maestro internal log stream

---

## daemon.py — Key Functions

| Function | Description |
|----------|-------------|
| `init(base_dir, data_dir)` | **Must be called first.** Sets CONFIG_FILE, LOG_DIR, GLOBAL_LOG. |
| `load_config()` | Parse config.txt into `entries[]`. Validates PIDs via `/proc`. |
| `save_config()` | Atomic write via tempfile + shutil.move. |
| `launch(idx, _from_restart)` | Launch entry. Refuses if `e.unreachable`. Routes to `_launch_external()` if `open_outside=True`. Race-safe: checks `manually_killed` before and after Popen. |
| `kill_entry(idx)` | Sets `manually_killed`, kills `proc` and `ext_proc`. |
| `kill_all()` | Kills all running entries. |
| `_check_paths()` | Runs every 2s. Detects file reachability transitions. Fires `_notify()` once per transition. |
| `_notify(title, body)` | Best-effort `notify-send`. Non-blocking. Uses `MaestroLogo24x24.png`. |
| `_metrics_loop()` | Background thread: `_read_sys_cpu()` + `_check_paths()` + `_update_metrics()` every 2s. |
| `_find_by_uid(uid)` | Returns `(index, ProcessEntry)` or `(None, None)`. |
| `_append_log(e, line)` | Strip ANSI, push to log buffer and all SSE subscribers. |
| `_pub(e, event, data)` | Broadcast non-log SSE event. |
| `_close_subs(e)` | Send sentinel `None` to all SSE subscribers. |
| `_mlog(msg)` | Log to Maestro internal channel + file. |
| `toggle_logging()` | Flip `_logging_enabled`, log the change, return new state. |
| `_read_sys_cpu()` | Update `_sys_cpu` from `/proc/stat` delta. |
| `_read_sys_ram()` | Return `(used_mb, total_mb)` from `/proc/meminfo`. |
| `_read_gpu()` | AMD GPU stats from sysfs. Returns zeros on failure. |
| `_kill_ext(e)` | Clears `e.ext_proc` first, then SIGTERM to process group + SIGKILL after 2s. |
| `_run_in_external_terminal(e, path, label)` | Try terminal emulators in order. Always calls `_kill_ext()` first. |
| `_watch_existing_pid(e)` | Poll `/proc` every 5s for PIDs alive at startup. |

### ProcessEntry fields

| Field | Type | Description |
|-------|------|-------------|
| `uid` | str | 10-char hex, stable across reorders |
| `status` | str | `ON` / `OFF` / `SEP` |
| `proc` | Popen\|None | Internal subprocess |
| `ext_proc` | Popen\|None | External terminal subprocess |
| `crash` | bool | Last exit was unexpected |
| `manually_killed` | bool | Set before intentional kill |
| `generation` | int | Incremented on each launch; invalidates old reader/monitor threads |
| `unreachable` | bool | `True` when `e.path` not found on disk |
| `log_buf` | deque(1000) | Circular log buffer |
| `subs` | list[Queue] | Live SSE client queues |
| `metrics` | dict | `{cpu: float, ram: int}` updated every 2s |

### `to_dict()` fields exposed to frontend

All `ProcessEntry` fields plus: `displayName`, `hasUpdate`, `hasInfo`, `unreachable`.

---

## main.py — Key Functions

| Function | Description |
|----------|-------------|
| `_resolve_data_dir()` | 3-level priority: `--data-dir` CLI arg, `MAESTRO_DATA` env, `dataDir` in settings.json, default. |
| `_load_settings(data_dir)` | Read settings.json. Creates with defaults on first run. Backfills missing keys. |
| `_write_settings(data_dir, s)` | Atomic write via tempfile + shutil.move. |
| `_check_single()` | PID lock via `/tmp/MaestroWeb.pid`. |
| `Handler._api_state()` | Calls `daemon._read_sys_ram()` and `daemon._read_gpu()` inline. |
| `Handler._sse_stream()` | Replays log buffer then blocks on queue. Keepalive `: k\n\n` every 5s. |
| `Handler._post_settings()` | Updates `_settings`, persists, applies `logsRecorder` → `daemon._logging_enabled` immediately. |
| `Handler._toggle_logging()` | Legacy endpoint. Calls `daemon.toggle_logging()`, syncs to settings.json. |
| `main()` | Boot sequence: resolve DATA_DIR → daemon.init() → load settings → apply settings → check single instance → load config → start metrics loop → start HTTP server. |

---

## tray.py — Key Functions

| Function | Description |
|----------|-------------|
| `_resolve_data_dir()` | Same 3-level priority as main.py (minus `--data-dir` arg). |
| `_load_settings()` | Read settings.json. Safe on missing file. |
| `_write_settings(s)` | Atomic write. |
| `_notify(title, body)` | `notify-send --expire-time 4000`. Non-blocking Popen. |
| `_set_autostart(enabled)` | Write/remove `~/.config/autostart/maestro.desktop`. Updates settings.json. |
| `_make_icon_pixbuf(dot_color)` | Cairo: load PNG, overlay 7px dot (green/red/grey), return GdkPixbuf. |
| `MaestroTray.run()` | Start main.py subprocess → optional browser open → health loop → SSE loop → Gtk.main(). |
| `MaestroTray._start_main()` | Popen `python3 main.py --data-dir <data_dir>`. Starts `_watch_main` thread. |
| `MaestroTray._watch_main()` | Blocks until main.py exits. Sets icon red on unexpected exit. Does NOT auto-restart. |
| `MaestroTray._health_loop()` | GET `/api/state` every 5s → update icon dot. Daemon thread. |
| `MaestroTray._sse_loop()` | Stream `/api/logs/__maestro__`. Filter 7 keywords → `_notify()`. Reconnects on drop. |
| `MaestroTray._shutdown()` | POST `/api/shutdown` → wait 1s → `_stop_main()`. Exits GTK if `trayPersistent=false`. |
| `MaestroTray._restart()` | `_stop_main()` → sleep 0.5s → `_start_main()`. Clears `_restart_pending`. |

### SSE notification keywords (tray.py)

| Keyword | Notification |
|---------|-------------|
| `▶ Started` | `<name> started` |
| `⚠ Crashed` | `<name> crashed` |
| `↺ Auto-restart` | `<name> restarting…` |
| `[UPDATE]` | `Update: <message>` |
| `File logging` | Raw message |
| `Script not found` | Raw message |
| `Script restored` | Raw message |

---

## ui.js — Key Functions

### State variables

```javascript
let entries = [];        // EntryDict[] from last /api/state
let _settings = {};      // settings.json contents
let selIdx = -1;         // selected sidebar row index
let panels = [];         // uid[] open grid panels (sessionStorage)
let sseConns = {};       // uid → EventSource
let activeTab = null;    // uid of focused webapp tab pill
let _prevHash = '';      // structural hash for poll optimisation
let _widePanels = {};    // uid → true when spanning 2 cells (localStorage 'tpw')
let _tabOrder = [];      // webapp tab pill order (localStorage 'mto')
let _loggingEnabled = true;
```

### Key functions

| Function | Description |
|----------|-------------|
| `poll()` | Fetch `/api/state`, update state, re-render. Calls `renderPills()` every time. |
| `_mkHash(entries)` | Structural hash including `unreachable` field. |
| `_fetchSettings()` | GET `/api/settings`, update `_settings`, call `_applyExplorerSetting()`. Called at boot and on every settings change. |
| `_applyExplorerSetting(enabled)` | Show/hide `#lsb-divider` + `#lsb-tree-wrap`. Closes explorer panel if disabled. |
| `openSettings()` | Fetch settings, render modal with 5 toggles + server section + links + footer. |
| `closeSettings()` | Hide settings backdrop. |
| `_smSection(title)` | Create a labelled section div for the settings modal. |
| `_smToggle(label, key, checked, onChange)` | Toggle row widget. Calls `onChange(bool)` on click. |
| `_smInput(label, type, value)` | Input field for settings modal. Returns the `<input>` element. |
| `renderSidebar()` | Full DOM rebuild. Handles unreachable entries: grey `✕` dot, muted name, `missing` badge, no action buttons. |
| `renderGrid()` | Recalculate grid cols/rows, apply wide-panel spans, restore scroll. |
| `renderPills()` | Rebuild webapp tab pills (shown only when process running). |
| `openPanel(uid)` | Add to `panels[]`, `renderGrid()` first, then `connectSSE()`. |
| `hidePanel(uid)` | Remove from `panels[]`, close SSE, remove DOM, re-render. |
| `connectSSE(uid)` | Open EventSource AFTER panel DOM exists. |
| `doLaunch(uid)` | Launch via uid. Skips if entry `unreachable`. |
| `doLaunchAllFav()` | Launch all `status==='ON' && !unreachable` entries. |
| `makeTermPanel(uid)` | Create log panel with metrics, colour theme, wide toggle. |
| `makeColorPicker(current, onChange)` | Colour swatch widget for terminal theme. |

### Panel UID conventions

| Pattern | Type |
|---------|------|
| `<10-char-hex>` | Script log panel |
| `<uid>_w<N>` | WebApp iframe (N = index in webapps[]) |
| `__maestro__` | Maestro internal log |
| `__explorer__` | File explorer overlay |
| `fv_<uid>` | Separator file viewer (editable) |
| `info_<uid>` | Info file panel (read-only) |

### Unreachable entry state (frontend)

When `e.unreachable === true`:
- Status dot replaced by grey `✕` box
- Name rendered at 40% opacity
- `missing` badge shown in metrics area
- No action buttons (eacts overlay suppressed)
- Row click still opens the RSB edit panel (path can be fixed)
- `doLaunchAllFav()` skips silently

---

## Features Summary

### Process types
1. **Regular script** — `bash <script.sh>`, stdout/stderr captured → scrollable log panel
2. **Open Outside** — same script in OS terminal emulator. Kill from Maestro works.
3. **WebApp** — script + iframe tabs. Iframes load only after ping confirms URL reachable.

### Unreachable entry state
- Detected every 2s via `os.path.isfile()` in `daemon._check_paths()`
- Transition → `_notify()` fires once (both directions)
- Frontend: grey dot, muted name, no launch button, "missing" badge
- Launch API returns `{"error": "script not found"}` on HTTP 400
- Entry is NOT removed automatically — user decides

### Settings modal (⚙ button)
- Centred overlay, semi-transparent backdrop, click outside to close
- Sections: General (5 toggles), Server (host + port inputs + LAN warning), Links, Footer text

### System tray
- `Gtk.StatusIcon` + Cairo dot overlay
- Green = up, Red = crashed/stopped, Grey = intentionally off
- Health check every 5s via GET `/api/state`
- SSE listener for desktop notifications
- Does NOT auto-restart main.py on crash — menu only

### Terminal colour themes

| ID | Label | Background |
|----|-------|------------|
| `default` | Default | `var(--bg2)` |
| `navy` | Navy | `#0a0e1a` |
| `forest` | Forest | `#0a150a` |
| `plum` | Plum | `#160a1a` |
| `teal` | Teal | `#091518` |
| `ember` | Ember | `#160a00` |

### Wide panel
Toggle `⊞` button. Spans 2 columns (or 2 rows in single-col layout). State in `localStorage('tpw')`.

### External terminal emulators (tried in order)
`gnome-terminal`, `xfce4-terminal`, `xterm`, `konsole`, `lxterminal`, `mate-terminal`

---

## Adding new features (AI guide)

### Adding a new config field
1. Add to `ProcessEntry.__init__()` in `daemon.py` with default
2. Parse in `load_config()` — always `p[N].strip() if len(p) > N else default`
3. Save in `save_config()` — always write all 15 fields
4. Include in `to_dict()`
5. Handle in `Handler._update_entry()` in `main.py`
6. Add to `_mkHash()` in `ui.js`
7. Update config format table in this guide

### Adding a new settings key
1. Add to `_DEFAULT_SETTINGS` in both `main.py` and `tray.py`
2. Add to `settings.json` schema section in this guide
3. Handle in `Handler._post_settings()` in `main.py`
4. Apply side effect where needed (immediately or on next boot)

### Adding a new API endpoint
1. Add `"/api/my-endpoint": self._my_handler` to the POST/GET dict in `Handler`
2. Implement `def _my_handler(self, b)` as a `Handler` method
3. Call from `ui.js` via `api('POST', '/api/my-endpoint', {...})`

### Adding a new panel type
1. Define UID convention (e.g. `"xx_" + uid`)
2. Add detection utility (`isXx = uid => uid.startsWith('xx_')`)
3. Implement `makeXxPanel(uid)` in `ui.js`
4. Handle in `renderGrid()`
5. Add to `validUIDs` set in `poll()` to prevent stale cleanup

### Critical SSE + DOM ordering rule
```javascript
panels.push(uid);      // 1. add to list
renderGrid();          // 2. create DOM node (panel with data-ppre attribute)
connectSSE(uid);       // 3. ONLY NOW open EventSource
```
Reversing steps 2 and 3 silently discards all initial replay log events.

---

## Development status

| Block | Status |
|-------|--------|
| daemon.py extraction | ✅ Done |
| main.py rewrite | ✅ Done |
| settings.json system | ✅ Done |
| tray.py | ✅ Done |
| start.sh update | ✅ Done |
| Settings modal UI | ✅ Done |
| File explorer toggle | ✅ Done |
| Unreachable entry state | ✅ Done |
| install.sh | ✅ Done |
| README.md | ✅ Done |
| V2_0_MAESTRO_GUIDE.md | ✅ Done |
| File Explorer component (full) | 🔜 Future |
| CodeMirror editor | 🔜 Future |
| Bookmarks system | 🔜 Future |

---

## How to start a new chat

Share both files at the start of the new chat:
1. `V2_0_MAESTRO_GUIDE.md` — this file (architecture, API, all functions)
2. Any source file being modified

Then say:
> "Leggi il guide MD. Stiamo lavorando su Maestro v2.0. [descrizione del problema/feature]"

Rules for the AI:
- Never write code without explicit user consent per block
- Always ask clarifying questions before writing code
- Always provide complete files when modifying existing ones
- Always specify save path in the response and as a comment at top of file
- camelCase in JS, snake_case in Python, English comments
- Never hardcode absolute paths in source

---

## Changelog

### v2.0 (current) — Michele Pella
- **Architecture**: `main.py` split into `tray.py` + `main.py` + `daemon.py`
- **daemon.py**: all process state extracted from main.py; `init(base_dir, data_dir)` for path injection
- **tray.py**: GTK StatusIcon, Cairo dot overlay, health check loop, SSE notification listener, autostart management, watchdog (no auto-restart)
- **settings.json**: new config system; `GET`/`POST /api/settings`; replaces scattered toggles
- **Settings modal**: ⚙ button replaces 📄; centred overlay modal with 5 toggles + server section
- **File explorer toggle**: `_applyExplorerSetting()` shows/hides sidebar tree and explorer panel
- **Unreachable entry state**: `_check_paths()` every 2s; frontend visual (grey ✕, muted name, "missing" badge); `doLaunchAllFav()` guard; `notify-send` on transitions
- **Data directory**: configurable via `--data-dir`, `MAESTRO_DATA`, `settings.json["dataDir"]`; default `../data/` relative to `bin/`
- **install.sh**: one-shot installer with requirements check, directory creation, autostart prompt
- **README.md**: public GitHub description

### v1.4
- See `V1_4_MAESTRO_GUIDE.md` for full v1.4 changelog
