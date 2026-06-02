# Maestro

**Privacy-first process orchestrator for Linux.**

Maestro lets you launch, monitor, and manage shell scripts from a browser UI running entirely on localhost. No cloud, no telemetry, no dependencies beyond the Python standard library and GTK.

---

## What is Maestro?

Maestro is a personal tool for people who run multiple background scripts on a Linux machine and want a clean way to manage them. It provides:

- A browser UI at `http://127.0.0.1:8865` with live log streaming
- A system tray daemon that keeps Maestro alive and shows its state
- Per-process terminal panels with colour themes, crash detection, and auto-restart
- WebApp iframe tabs for scripts that expose a web interface (ComfyUI, llama.cpp, etc.)
- A file explorer and inline text editor for quick edits without leaving the UI

Everything runs locally. Nothing leaves your machine.

---

## Features

- **Process management** — launch, kill, restart scripts; auto-restart on crash; configurable launch delay
- **Live log streaming** — Server-Sent Events (SSE), no polling, no WebSocket dependency
- **System tray** — GTK StatusIcon with green/red/grey dot; right-click menu; health check every 5s
- **Desktop notifications** — `notify-send` on process start, crash, restart, update, script not found
- **WebApp tabs** — iframe tabs in the header bar, ping-gated load, suspend/resume, drag-to-reorder
- **Terminal colour themes** — Default, Navy, Forest, Plum, Teal, Ember — set per script
- **Wide panel toggle** — any terminal panel can span 2 grid columns
- **Open outside** — launch a script in an OS terminal emulator instead of the internal panel
- **Update scripts** — per-entry `update.sh` support, run in external terminal
- **Info files** — read-only `.md` panel per script
- **Separator rows** — group scripts with labelled dividers; optional attached text file
- **File tree** — sidebar tree for quick navigation; full file explorer panel
- **Unreachable detection** — scripts with missing files shown as unreachable; notified once per transition
- **Settings modal** — toggle file explorer, logs recorder, autostart, open browser at launch, host/port
- **Single-instance lock** — `/tmp/MaestroWeb.pid`
- **Atomic config writes** — `config.txt` and `settings.json` written via `tempfile + shutil.move`
- **Zero pip, zero npm, zero CDN** — pure Python stdlib backend, vanilla JS frontend

---

## Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | System Python — no venv needed |
| python3-gobject | any | GTK bindings for the tray daemon |
| bash | any | Script runner |
| Linux | any | Tested on Fedora 38+ GNOME/XFCE |
| notify-send | any | Optional — desktop notifications |

**Fedora:**
```bash
sudo dnf install python3-gobject libnotify
```

**Ubuntu / Debian:**
```bash
sudo apt install python3-gi gir1.2-gtk-3.0 libnotify-bin
```

---

## Install

### From GitHub

```bash
git clone https://github.com/AIdevelopmentPit/Maestro.git
cd Maestro
bash install.sh
```

The installer will:
1. Check requirements and print install/fix instructions
2. Ask for an install path (default: `~/.local/share/maestro`)
3. Create `bin/` (source files) and `data/` (config, logs, settings)
4. Optionally enable autostart via `~/.config/autostart/maestro.desktop`

### Manual

```bash
# Copy source files to your preferred location
mkdir -p /my/path/bin /my/path/data
cp tray.py main.py daemon.py ui.html ui.js webui.js start.sh /my/path/bin/
cp -r img/ /my/path/bin/
chmod +x /my/path/bin/start.sh /my/path/bin/tray.py
```

---

## Usage

### Start

```bash
bash /path/to/bin/start.sh
```

This launches `tray.py` (GTK tray daemon), which in turn launches `main.py` (HTTP server). The tray icon appears in your system tray.

### Open the UI

- Click the tray icon, or
- Open `http://127.0.0.1:8865` in any browser

### Tray icon

| Dot colour | Meaning |
|------------|---------|
| 🟢 Green | HTTP server is up and responding |
| 🔴 Red | Server stopped or crashed |
| ⚫ Grey | Intentionally shut down |

Right-click the tray icon for: Open in Browser, Restart Maestro, Autostart toggle, Shut Down.

---

## Config format (`config.txt`)

One entry per line, pipe-delimited. Edited via the UI — you rarely need to touch this file directly.

```
STATUS|PATH|PID|DELAY|AR|KO|NAME|_|LH|WEBAPPS_JSON|UPDATE_SCRIPT|SEP_FILE|INFO_FILE|OPEN_OUTSIDE|TERM_COLOR
```

| Field | Values | Description |
|-------|--------|-------------|
| STATUS | `ON` / `OFF` / `SEP` | Favourite flag / separator |
| PATH | string | Absolute path to `.sh` script |
| PID | int or empty | Last known PID (validated at load) |
| DELAY | int | Launch delay in seconds |
| AR | `0`/`1` | Auto-restart on crash |
| KO | `0`/`1` | Keep panel open on crash |
| NAME | string | Custom display name |
| _ | — | Reserved (legacy, ignored) |
| LH | `0`/`1` | Launch hidden |
| WEBAPPS_JSON | JSON | `[{"url":"http://127.0.0.1:8188"}]` |
| UPDATE_SCRIPT | string | Path to update script |
| SEP_FILE | string | Attached text file (separator only) |
| INFO_FILE | string | Read-only info `.md` panel |
| OPEN_OUTSIDE | `0`/`1` | Open in OS terminal emulator |
| TERM_COLOR | string | `default` / `navy` / `forest` / `plum` / `teal` / `ember` |

---

## Settings (`settings.json`)

Located in `data/settings.json`. Edited via the ⚙ Settings modal in the UI.

| Key | Default | Description |
|-----|---------|-------------|
| `fileExplorer` | `false` | Show/hide sidebar file tree and explorer panel |
| `logsRecorder` | `false` | Write all logs to `data/logs/maestro.log` |
| `openBrowserAtLaunch` | `false` | Auto-open browser 3s after start |
| `autostart` | `false` | Create/remove `~/.config/autostart/maestro.desktop` |
| `trayPersistent` | `true` | Keep tray alive when Maestro is shut down from UI |
| `host` | `127.0.0.1` | HTTP bind address — set `0.0.0.0` for LAN access (no auth!) |
| `port` | `8865` | HTTP port |
| `dataDir` | `""` | Custom data directory path (empty = default `../data/`) |

---

## Directory structure

```
<install-dir>/
├── bin/                  ← source files (never run directly from here)
│   ├── tray.py           ← GTK tray daemon (entry point)
│   ├── main.py           ← HTTP server + UI layer
│   ├── daemon.py         ← process manager, SSE broker, metrics
│   ├── ui.html           ← single-page frontend shell
│   ├── ui.js             ← all frontend JS
│   ├── webui.js          ← WebApp iframe lifecycle
│   ├── start.sh          ← launch script
│   └── img/
│       ├── MaestroLogo.svg
│       └── MaestroLogo24x24.png
└── data/                 ← user data (never committed to git)
    ├── settings.json
    ├── config.txt
    └── logs/
        └── maestro.log
```

Data directory path resolution order:
1. `MAESTRO_DATA` environment variable
2. `dataDir` field in `settings.json`
3. Default: `<install-dir>/data/`

---

## Screenshots

*Coming soon.*

---

## License

MIT License — © Michele Pella

Permission is hereby granted, free of charge, to any person obtaining a copy of this software to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software, subject to the following conditions: the above copyright notice and this permission notice shall be included in all copies or substantial portions of the software.

---

## Contributing

Issues and PRs welcome. Please read the developer reference (`V2_0_MAESTRO_GUIDE.md`) before contributing — it documents the full architecture, API, and coding rules.

---

## Support

☕ [Buy me a coffee](#) *(link coming soon)*
