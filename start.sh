#!/usr/bin/env bash
# Maestro — main start script (v2.0)
# Path: start.sh
#
# Launches tray.py (GTK daemon + watchdog).
# tray.py spawns main.py (HTTP server) as a subprocess.
#
# Data directory resolution order (handled inside tray.py / main.py):
#   1. MAESTRO_DATA env var  (export MAESTRO_DATA=/custom/path before calling this script)
#   2. dataDir field in settings.json at the default location
#   3. Default: <BIN_DIR>/../data/
set -euo pipefail

BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BIN_DIR"

echo "[maestro] Starting tray daemon from $BIN_DIR"
exec /usr/bin/python3 tray.py --bin-dir "$BIN_DIR"
