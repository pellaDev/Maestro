#!/usr/bin/env python3
"""
Maestro — cross-platform Python launcher
Path: start.py

Works on Windows and Linux. Replaces start.sh on Windows; can also be used
on Linux as an alternative to the bash script.

Launches tray.py (GTK/Win32 tray daemon + main.py watchdog) with --bin-dir
pointing to this file's directory so all paths resolve correctly.

Any extra arguments (e.g. --data-dir /custom/path) are forwarded to tray.py.

Usage:
    python start.py
    python start.py --data-dir /path/to/data
"""
import os, sys, subprocess
from pathlib import Path


def main():
    bin_dir = Path(__file__).parent.resolve()
    tray    = bin_dir / "tray.py"

    if not tray.exists():
        print(f"[maestro] Error: tray.py not found at {tray}", file=sys.stderr)
        sys.exit(1)

    # Forward any extra args (e.g. --data-dir) to tray.py
    cmd = [sys.executable, str(tray), "--bin-dir", str(bin_dir)] + sys.argv[1:]

    print(f"[maestro] Starting tray daemon from {bin_dir}", flush=True)

    if sys.platform == "win32":
        # Windows: spawn subprocess and wait; start.py acts as a thin watchdog.
        # The actual process management is inside tray.py.
        try:
            proc = subprocess.Popen(cmd, cwd=str(bin_dir))
            sys.exit(proc.wait())
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        # Unix: replace the current process image with tray.py (cleaner — no
        # extra Python interpreter lingering in the process list).
        os.chdir(str(bin_dir))
        os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
