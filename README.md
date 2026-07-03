<h1 align="center">
  <strong><span style="color:#FFD700;">MAESTRO</span></strong>
</h1>

<p align="center">
  <strong><span style="color:#FF8C00;">Privacy-first process orchestrator for Linux and Windows.</span></strong>
</p>

<p align="center">
  <img src="img/MaestroLogo.png" width="124" alt="Logo">
</p>

Maestro lets you launch, monitor, and manage shell scripts from a browser UI running entirely on localhost. No cloud, no telemetry, no external dependencies beyond the Python standard library and GTK (Linux) or Win32 (Windows).

---

> **Portable first** — clone the repo anywhere, run `bash install.sh` (Linux) or `python install.py` (Windows), and everything installs self-contained inside that same folder. No system paths touched. No scattered files. Move the folder and Maestro moves with it.

---

<p align="center">
<img width="2187" height="1336" alt="Pasted image (2)" src="https://github.com/user-attachments/assets/2e512e68-db4b-416d-9d12-f9949bbc4742" />
</p>

## What is Maestro?

Maestro is a personal tool for people who run multiple background scripts on a desktop machine and want a clean way to manage them. It provides:

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
- **System tray** — GTK StatusIcon / AppIndicator3 (Linux) or Win32 (Windows); health check every 5s
- **Desktop notifications** — `notify-send` on Linux; Win32 balloon on Windows; fired on start, crash, restart, update, script not found
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
- **Single-instance lock** — platform temp dir `/MaestroWeb.pid`
- **Atomic config writes** — `config.txt` and `settings.json` written via `tempfile + shutil.move`
- **Zero pip, zero npm, zero CDN** — pure Python stdlib backend, vanilla JS frontend

---

## Requirements

### Linux

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | System Python — no venv needed |
| python3-gobject | any | GTK bindings — **required** for the tray daemon |
| AppIndicator3 | any | Optional — falls back to GtkStatusIcon, then headless |
| bash | any | Script runner |
| Linux | any | Tested on Fedora 38+ GNOME/XFCE |
| notify-send | any | Optional — desktop notifications |

Install dependencies for your distro:

```bash
# Fedora / RHEL / Rocky / Alma
sudo dnf install python3-gobject libappindicator-gtk3 libnotify

# Ubuntu 20.04 / Debian / Mint
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 libnotify-bin

# Ubuntu 22.04+  (Ayatana replaces the old AppIndicator)
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin

# Arch / Manjaro  (AppIndicator3 is AUR-only; GtkStatusIcon works without it)
sudo pacman -S python-gobject libnotify
yay -S libappindicator-gtk3      # optional, from AUR

# openSUSE Leap / Tumbleweed
sudo zypper install python3-gobject typelib-1_0-AppIndicator3-0_1 libnotify-tools
```

> **GNOME users** — if the tray icon is invisible, enable the AppIndicator Shell Extension:
> `gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com`
> or install it from https://extensions.gnome.org/extension/615/appindicator-support/

**Tray fallback chain** (automatic, no config needed):
`AppIndicator3` → `GtkStatusIcon` → headless (prints URL to console)

---

### Windows

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) — official installer, add to PATH |
| Windows | 10 / 11 | Win10+ for balloon notifications and Windows Terminal |
| WSL **or** Git Bash | any | Required to run `.sh` scripts |

> Maestro uses only Python stdlib on Windows — **zero pip installs needed.**

Install bash if not already present:

```
WSL (recommended):   https://aka.ms/wslinstall
                     Run: wsl --install  in PowerShell (admin)

Git Bash:            https://git-scm.com/download/win
```

---

## Install

Maestro has two install channels — pick one when cloning below:

- **Latest** — tracks the `main` branch directly. This is the primary,
  most up-to-date channel; most development and testing happens here.
- **Stable** — pinned to the most recent tagged
  [GitHub Release](https://github.com/AIdevelopmentPit/Maestro/releases), a
  frozen snapshot for anyone who'd rather not track `main` day-to-day.

Both channels use the exact same installer (`install.sh` / `install.py`) —
the only difference is which commit you check out *before* running it.

### Linux — from GitHub

**Latest (main branch):**
```bash
git clone https://github.com/AIdevelopmentPit/Maestro.git
cd Maestro
bash install.sh
```

**Stable (latest tagged Release):**
```bash
git clone https://github.com/AIdevelopmentPit/Maestro.git
cd Maestro
git fetch --tags
git checkout "$(git describe --tags "$(git rev-list --tags --max-count=1)")"
bash install.sh
```

By default this installs **entirely inside the cloned folder**:
```
Maestro/
├── bin/    ← source files (created by installer)
└── data/   ← config.txt, settings.json, logs/ (created by installer)
```

The installer will:
1. Auto-detect your distro and show exact package install commands if anything is missing
2. Confirm the install path (default: the folder where `install.sh` lives)
3. Create `bin/` and `data/` inside that folder
4. Optionally enable autostart via `~/.config/autostart/maestro.desktop`
5. Optionally create an application menu entry (GNOME Activities, KDE menu, etc.)

Custom install path (optional — if you want bin/ and data/ somewhere else):
```bash
bash install.sh --install-dir /custom/path
bash install.sh --no-autostart   # skip autostart prompt
```

### Linux — manual

```bash
mkdir -p /my/path/bin/img /my/path/data
cp tray.py main.py daemon.py ui.html ui.js webui.js start.sh start.py /my/path/bin/
cp img/MaestroLogo.svg img/MaestroLogo24x24.png /my/path/bin/img/
chmod +x /my/path/bin/start.sh /my/path/bin/start.py /my/path/bin/tray.py
```
A manual copy is inherently whatever version you copied from — there's no
channel selection here, that only applies to the `git clone` paths above.

---

### Windows — from GitHub

Open **PowerShell** or **Command Prompt**:

**Latest (main branch):**
```powershell
git clone https://github.com/AIdevelopmentPit/Maestro.git
cd Maestro
python install.py
```

**Stable (latest tagged Release):**
```powershell
git clone https://github.com/AIdevelopmentPit/Maestro.git
cd Maestro
git fetch --tags
git checkout (git describe --tags (git rev-list --tags --max-count=1))
python install.py
```

By default this installs **entirely inside the cloned folder**:
```
Maestro├── bin\    ← source files (created by installer)
└── data\   ← config.txt, settings.json, logs\ (created by installer)
```

The installer will:
1. Check Python 3.11+ and bash availability (WSL / Git Bash)
2. Confirm the install path (default: the folder where `install.py` lives)
3. Create `bin\` and `data\` inside that folder
4. Create `data\settings.json` with defaults
5. Optionally enable autostart via the Windows registry (`HKCU\...\Run`)
6. Optionally create a Start Menu shortcut (silent launch, no console window)

Custom install path (optional):
```powershell
python install.py --install-dir C:\Tools\Maestro
python install.py --no-autostart
```

### Windows — manual

```powershell
# In PowerShell, from the cloned repo directory:
$dest = "$env:LOCALAPPDATA\Maestro"
New-Item -ItemType Directory -Force "$dest\bin\img", "$dest\data\logs"
Copy-Item tray.py,main.py,daemon.py,ui.html,ui.js,webui.js,start.py,start.bat,start.sh "$dest\bin\"
Copy-Item img\MaestroLogo.svg,img\MaestroLogo24x24.png "$dest\bin\img\"
```

---

## Usage

### Start

**Linux:**
```bash
bash /path/to/bin/start.sh
# or:
python3 /path/to/bin/start.py
```

**Windows:**
```
Double-click  bin\start.bat
# or in PowerShell:
python bin\start.py
```

This launches `tray.py` (tray daemon + watchdog), which in turn launches `main.py` (HTTP server). The tray icon appears in your system notification area.

### Open the UI

- Click the tray icon → **Open in Browser**, or
- Navigate to `http://127.0.0.1:8865` in any browser

### Tray icon

| Dot colour | Meaning |
|---|---|
| 🟢 Green | HTTP server is up and responding |
| 🔴 Red | Server stopped or crashed |
| ⚫ Grey | Intentionally shut down |

Right-click the tray icon for: **Open in Browser**, **Restart Maestro**, **Autostart toggle**, **Shut Down**.

---

## Config format (`config.txt`)

One entry per line, pipe-delimited. Edited via the UI — you rarely need to touch this directly.

```
STATUS|PATH|PID|DELAY|AR|KO|NAME|_|LH|WEBAPPS_JSON|UPDATE_SCRIPT|SEP_FILE|INFO_FILE|OPEN_OUTSIDE|TERM_COLOR
```

| Field | Values | Description |
|---|---|---|
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
|---|---|---|
| `fileExplorer` | `false` | Show/hide sidebar file tree and explorer panel |
| `logsRecorder` | `false` | Write all logs to `data/logs/maestro.log` |
| `openBrowserAtLaunch` | `false` | Auto-open browser 3s after start |
| `autostart` | `false` | Create/remove autostart entry (Linux `.desktop` / Windows registry) |
| `trayPersistent` | `true` | Keep tray alive when Maestro is shut down from UI |
| `systemNotifications` | `true` | Enable desktop notifications (notify-send / Win32 balloon) |
| `host` | `127.0.0.1` | HTTP bind address — set `0.0.0.0` for LAN access (no auth!) |
| `port` | `8865` | HTTP port |
| `dataDir` | `""` | Custom data directory path (empty = default `../data/`) |

---

## Directory structure

```
<install-dir>/
├── bin/                  ← source files
│   ├── tray.py           ← tray daemon entry point (GTK / Win32 / headless)
│   ├── main.py           ← HTTP server + UI layer
│   ├── daemon.py         ← process manager, SSE broker, metrics (Linux + Windows)
│   ├── ui.html            ← single-page frontend shell
│   ├── ui.js              ← all frontend JS
│   ├── webui.js           ← WebApp iframe lifecycle
│   ├── start.sh           ← Linux launch script
│   ├── start.py           ← cross-platform Python launch script
│   ├── start.bat          ← Windows launch script (calls start.py)
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

## License

MIT License — © Michele Pella

Permission is hereby granted, free of charge, to any person obtaining a copy of this software to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software, subject to the following conditions: the above copyright notice and this permission notice shall be included in all copies or substantial portions of the software.

---

## Contributing

Issues and PRs welcome. However, all contributions are governed by the terms described in CONTRIBUTING.md. By submitting a contribution, you agree to those terms.

---

## Support

☕ [Buy me a coffee](#) *(link coming soon)*
