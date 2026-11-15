<p align="center">
  <a href="https://liberapay.com/pellaDev/donate">
    <img src="https://liberapay.com/assets/widgets/donate.svg" alt="Donate using Liberapay">
  </a>
</p>

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
<img width="2313" height="1341" alt="screenshot" src="https://github.com/user-attachments/assets/c4dc7b4d-3bf8-4bbe-aff0-2870bd0f4c20" />
</p>

## What is Maestro?

Maestro is a personal tool for people who run multiple background scripts on a desktop machine and want a clean way to manage them. It provides:

- A browser UI at `http://127.0.0.1:8865` with live log streaming
- A system tray daemon that keeps Maestro alive and shows its state
- Per-process terminal panels with colour themes, crash detection, and auto-restart
- WebApp iframe tabs for scripts that expose a web interface (ComfyUI, llama.cpp, etc.)
- A file explorer and inline text editor for quick edits without leaving the UI
- A one-click **Update Maestro** section (Settings) that pulls the Latest or Pre-release channel straight from GitHub — plus auto-update, any-version install/rollback, and full state backup/restore

Everything runs locally. Nothing leaves your machine (aside from the update check/download itself talking to GitHub, when you use that feature).

---

## Features

- **Process management** — launch, kill, restart scripts; auto-restart on crash; configurable launch delay
- **Live log streaming** — one multiplexed Server-Sent Events (SSE) stream for all panels, no polling, no WebSocket dependency; `\r`-rewritten progress lines (install/download bars) render live
- **System tray** — GTK StatusIcon / AppIndicator3 (Linux) or Win32 (Windows); health check every 5s
- **Desktop notifications** — `notify-send` on Linux; Win32 balloon on Windows; fired on start, crash, restart, update, script not found
- **WebApp tabs** — iframe tabs in the header bar, suspend/resume, drag-to-reorder; lazy loading — inactive webapps auto-suspend after `webAppAutoSuspendMin` minutes and wake on click (optional background tab keep-alive keeps them loaded)
- **Terminal colour themes** — Default, Navy, Forest, Plum, Teal, Ember — set per script
- **Wide panel toggle** — any terminal panel can span 2 grid columns
- **Open outside** — launch a script in an OS terminal emulator instead of the internal panel
- **Update scripts** — per-entry `update.sh` support, run in external terminal
- **Info files** — read-only `.md` panel per script
- **Separator rows** — group scripts with labelled dividers; optional attached text file
- **Native file picker** — adding scripts or attaching files opens the OS dialog (zenity or kdialog on Linux); last-used directory remembered
- **Unreachable detection** — scripts with missing files shown as unreachable; notified once per transition
- **Settings modal** — toggle background tab keep-alive, screen wake lock, webapp auto-suspend, logs recorder, autostart, open browser at launch, host/port, GitHub token
- **Update Maestro** — check for and install updates from either the Latest or Pre-release channel, directly from the Settings modal — no manual reinstall needed. Also: **CHECK ALL VERSIONS** lists every installable release so you can install any specific tag (rollback included), an optional **Auto-update** toggle that installs a newer Latest at startup, and **Backup / Restore** to export/import your full state (settings + scripts list)
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

> **GNOME users** — stock GNOME Shell has no legacy tray, so **the tray icon is
> usually invisible** without an AppIndicator shell extension. The installer says
> so plainly when `AppIndicator3` is missing. Maestro keeps running without it
> (web UI only) — that is normal, not a failed install. Enable it:
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

Maestro has two install channels — pick one when cloning below. Naming
matches GitHub's own release vocabulary directly (GitHub flags the newest
tagged, non-prerelease Release as "Latest"; anything else is a "Pre-release"):

- **Latest** — the most recent tagged [GitHub Release](https://github.com/pellaDev/Maestro/releases)
  that GitHub flags as "Latest" (tag shape `vX.Y`). A pinned, finalized snapshot.
  Recommended for anyone who'd rather not track pre-releases day-to-day.
- **Pre-release** — the newest release flagged as "Pre-release" on GitHub
  (tag shape `vX.Y.Z`). This is where day-to-day development ships; expect it
  to be ahead of Latest most of the time, and occasionally less tested.

Both channels use the exact same installer (`install.sh` / `install.py`) —
the only difference is which tag you check out *before* running it. Both are
also installable/switchable later from inside the app itself, via the Settings
modal's **Update Maestro** section (CHECK LATEST / CHECK PRE-RELEASE buttons)
— no need to re-clone or reinstall by hand once Maestro is already running.

A plain `git clone` with no checkout lands on `main`, which is the Pre-release
channel. Say which channel you actually installed before reporting a version.

### Linux — from GitHub

**Pre-release (newest pre-release tag):**
```bash
git clone https://github.com/pellaDev/Maestro.git
cd Maestro
git fetch --tags
git checkout "$(git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
bash install.sh
```

**Latest (most recent tagged Release):**
```bash
git clone https://github.com/pellaDev/Maestro.git
cd Maestro
git fetch --tags
git checkout "$(git describe --tags --abbrev=0)"
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

### Non-interactive install (agents and scripts)

Both installers ask for confirmation before creating anything. When nobody is
there to answer, pass the flags that suppress every prompt:

```bash
bash install.sh --yes --no-menu-entry --no-autostart
python install.py --yes --no-menu-entry --no-autostart
```

| Flag | Effect |
|---|---|
| `--yes` | Accept each prompt's default answer |
| `--no-menu-entry` | Skip the application menu entry (Windows: Start Menu shortcut) |
| `--no-autostart` | Skip the autostart prompt |

Leave the flags out when a human installs interactively — the answers are the
same, only asked instead of assumed.

The rules for an agent running the install are in [AGENTS.md](AGENTS.md) and
[docs/agent-install.md](docs/agent-install.md).

### Verify the install

An installer exiting `0` is not proof that Maestro is installed and running.
"Installed" means every one of these is true:

1. the installer exited `0`;
2. `bin/` and `data/settings.json` exist inside the install directory;
3. `data/settings.json` shows `host` `127.0.0.1` and `port` `8865` — the
documented default, changed only when explicitly asked;
4. after the first launch a listener is visible on that port:
`ss -ltnp | grep 8865` (Windows: `netstat -ano | findstr 8865`);
5. the UI loads at `http://127.0.0.1:8865`.

If a check fails, stop there and report it. Do not change `host` or `port`, do
not enable autostart or create a menu entry, and do not start anything that was
not asked for.

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

**Pre-release (main branch):**
```powershell
git clone https://github.com/pellaDev/Maestro.git
cd Maestro
python install.py
```

**Latest (most recent tagged Release):**
```powershell
git clone https://github.com/pellaDev/Maestro.git
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

`bash bin/start.sh` stays attached to the terminal that runs it: closing that
terminal sends `SIGHUP` to `tray.py` and to its `main.py` child, so Maestro
exits. For a detached start use the menu entry or autostart, or
`setsid bash bin/start.sh &`.

### Open the UI

- Click the tray icon → **Open in Browser**, or
- Navigate to `http://127.0.0.1:8865` in any modern browser

> **Works with any modern browser.** The UI is plain HTML/JS with SSE log
> streaming — no plugins, extensions, or anything to install. Any evergreen
> desktop or mobile browser (Chrome, Edge, Firefox, Safari, Brave, …) works,
> as long as it runs on the same machine so `127.0.0.1` is reachable.

### Tray icon

| Dot colour | Meaning |
|---|---|
| 🟢 Green | HTTP server is up and responding |
| 🔴 Red | Server stopped or crashed |
| ⚫ Grey | Intentionally shut down |

Right-click the tray icon for: **Open in Browser**, **Restart Maestro**, **Autostart toggle**, **Shut Down**.

Only one tray process runs — `tray.py` refuses a second instance through a
guard file in the system temp directory. Two tray PIDs means it was launched
twice, usually autostart plus a manual start. On stock GNOME the `GtkStatusIcon`
fallback prints `Gtk-CRITICAL ... gtk_widget_get_scale_factor` warnings and shows
no icon; that is the missing-extension case above, not a crash.

### Terminal emulator (Open outside)

`open_outside` scripts launch in an OS terminal emulator. Auto-detection order:
`gnome-terminal`, `xfce4-terminal`, `xterm`, `konsole`, `lxterminal`,
`mate-terminal`, `ptyxis`, `xdg-terminal-exec` — the first one found wins.

**ptyxis** (default terminal on Fedora Workstation, GTK4/VTE) is supported, with
two behaviours that need special handling:

1. it does not propagate the command's exit code to its own process, so crash
detection reads the real exit status from `data/terminal_state/<uid>.rc`, written
by the wrapper the terminal actually runs;
2. without `-s` it forwards the command to an already-running instance and the
launcher exits immediately, so Maestro launches it with `-s` and trusts the state
file instead of the launcher's return code.

When a failure is detected the window stays open until a key is pressed, so the
error is still readable.

Override auto-detection with the `terminal` key in `settings.json`
(⚙ Settings → Terminal emulator) or the `MAESTRO_TERMINAL` environment variable;
`$TERMINAL` is honoured as a fallback. Any command line works — a name that is
not one of the known emulators is used as-is (`<terminal> -- bash -c "<cmd>"`).
If nothing is found, the error names the terminals actually tried and how to
configure one.

On Windows the internal panel is used; no external emulator is spawned.

### Updating Maestro

From the running app: ⚙ **Settings → Update Maestro**. Two independent
buttons, **CHECK LATEST** and **CHECK PRE-RELEASE** — clicking one checks
that channel against what you have installed; if a newer version is found,
the button turns into **⬆ UPDATE NOW**. Updating stops all running scripts
and restarts Maestro (no automatic backup is kept).

More in this section:

- **Auto-update** (toggle) — when on, Maestro checks the Latest channel at
  startup and installs automatically if a newer release exists. It never
  downgrades, and it does nothing if your current version can't be read.
- **CHECK ALL VERSIONS** — lists *every* installable GitHub release (newest
  first, your current one marked). Pick one and the button becomes
  **INSTALL THIS VERSION**: a second click installs exactly that tag — which
  also means you can roll back to an older release on purpose.

If the repo is private at the time you're reading this, you'll need a GitHub
token in the same Settings section for any of this to work — see the repo's
own visibility for whether that still applies.

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
| `tabKeepalive` | `true` | Keep inactive webapp tabs loaded; no auto-suspend |
| `screenWakeLock` | `true` | Screen wake lock while the UI is open; re-acquired when the tab becomes visible |
| `webAppAutoSuspendMin` | `10` | Minutes of inactivity after which an inactive webapp tab suspends itself (page blanked, no requests until click/▶) |
| `logsRecorder` | `false` | Write all logs to `data/logs/maestro.log` |
| `openBrowserAtLaunch` | `false` | Auto-open browser 3s after start |
| `autostart` | `false` | Create/remove autostart entry (Linux `.desktop` / Windows registry) |
| `trayPersistent` | `true` | Keep tray alive when Maestro is shut down from UI |
| `systemNotifications` | `true` | Enable desktop notifications (notify-send / Win32 balloon) |
| `autoUpdate` | `false` | When on, check the Latest channel at startup and auto-install if a newer release exists (never downgrades; no-op when the local version is unreadable). Takes effect on next start. |
| `host` | `127.0.0.1` | HTTP bind address — set `0.0.0.0` for LAN access (no auth!) |
| `port` | `8865` | HTTP port |
| `dataDir` | `""` | Custom data directory path (empty = default `../data/`) |
| `githubToken` | `""` | GitHub Personal Access Token — only needed for **Update Maestro** while the repo is private. Server-side only, never sent to the browser. |
| `terminal` | `""` | Preferred terminal emulator for `open_outside` launches (blank = auto-detect). Takes effect from the next launch. |

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

## Uninstall

Deleting the install folder alone is not a full removal — the installer may have
created entries outside it.

**Linux:**
```bash
rm -rf /path/to/Maestro
rm -f ~/.local/share/applications/maestro.desktop   # application menu entry
rm -f ~/.config/autostart/maestro.desktop           # autostart entry
```

**Windows:**

- delete the install directory;
- delete the Start Menu shortcut
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Maestro.lnk`;
- remove the autostart value `Maestro` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  (`reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /f`).

Quit the tray process first — one process only. `data/` holds the scripts list,
settings and logs, so removing the folder removes your scripts too.

---

## License

MIT License — © Michele Pella

Permission is hereby granted, free of charge, to any person obtaining a copy of this software to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software, subject to the following conditions: the above copyright notice and this permission notice shall be included in all copies or substantial portions of the software.

---

## Contributing

Issues and PRs welcome. However, all contributions are governed by the terms described in CONTRIBUTING.md. By submitting a contribution, you agree to those terms.

---

## Support

💛 [Donate on Liberapay](https://liberapay.com/pellaDev/donate)
