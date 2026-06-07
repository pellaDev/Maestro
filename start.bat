@echo off
:: Maestro — Windows launcher
:: Path: start.bat
::
:: Double-click this file (or add it to Task Scheduler / Startup folder)
:: to launch Maestro on Windows.
::
:: Prefers pythonw.exe (no console window) over python.exe.
:: Any arguments are forwarded to start.py → tray.py.

setlocal
cd /d "%~dp0"

where pythonw 1>nul 2>&1
if %errorlevel% equ 0 (
    :: pythonw: no console window, silent background launch
    start "" pythonw "%~dp0start.py" %*
) else (
    :: python fallback: console window stays open while Maestro runs
    python "%~dp0start.py" %*
)

endlocal
