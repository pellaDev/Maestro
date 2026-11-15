# Install guide for an agent

This page is written for a model that runs the installation on someone's machine. It assumes you have a shell on that
machine and the Maestro files. The README's Requirements and Install sections hold the normal instructions; this page
is the order, the stop conditions, the definition of "installed", and the difference between what you checked and
what you assumed. The root [AGENTS.md](../AGENTS.md) is the short version of the same rules.

## Rules that hold on every machine

1. Read the README's Requirements and Install sections for this machine first, then follow them in order. Do not
   improvise a step.
2. **Stop at the first failure.** Do not continue "to see what happens". Report the failure and wait.
3. Never change host or port. The default is 127.0.0.1 and 8865, and it is the documented port - the installer
   prints it at the end. Change it only when the user asked for that specific machine.
4. Do not create autostart, a menu entry, a Start Menu shortcut, or a registry Run value unless asked. Use
   --yes --no-menu-entry --no-autostart for an unattended run instead of answering prompts by hand, and do not ask
   the user questions the flags already answer.
5. Do not install packages, do not open a browser, do not start Maestro, do not enable a shell extension, unless
   asked.
6. Do not write settings.json, a GitHub token, or any secret on the user's behalf unless asked.
7. Say what you verified and what you inferred, separately. An installer returning 0 proves the installer ran, not
   that Maestro is running.
8. Never report "installed successfully" unless every item in the definition below was checked on this machine.

## What "installed" means

Maestro is installed when all of these are true on this machine, checked after the installer finished:

1. the installer exited 0;
2. bin/ and data/settings.json exist inside the install directory;
3. data/settings.json shows host 127.0.0.1 and port 8865 - unless the user asked for a different port;
4. after the first launch a listener is visible on that port: ss -ltnp | grep 8865 (Windows:
   netstat -ano | findstr 8865);
5. the UI loads at http://127.0.0.1:8865.

Anything less is a partial install, and must be reported as such.

## Linux, in order

1. Check before installing: python3 3.11+, python3-gobject, bash, and which terminal emulator exists. Report the
   exact versions, do not assume them.
2. Say which channel you are installing. A plain git clone lands on main, which is the Pre-release channel;
   git checkout <tag> pins it (vX.Y = Latest, vX.Y.Z = Pre-release).
3. Install without prompts: bash install.sh --yes --no-menu-entry --no-autostart. Add --install-dir only when the
   user named a path.
4. Run the five checks in the definition above. Report each as a command plus its output line.
5. Terminal emulators. open_outside needs one. Auto-detection order: gnome-terminal, xfce4-terminal, xterm,
   konsole, lxterminal, mate-terminal, ptyxis, xdg-terminal-exec. If none is found, do not install one: report the
   list the error printed. If the user asked for a specific emulator, set the terminal key in settings.json
   (Settings -> Terminal emulator) or MAESTRO_TERMINAL, then launch again.
   - ptyxis does not return the command's exit code, so crash detection reads data/terminal_state/<uid>.rc, written
     by the wrapper the terminal actually runs. Verify that file after a failing script before claiming the state is
     correct.
   - cbreak() returned ERR from curses is not a detection failure: it means the process has no controlling TTY. Run
     the script through "open in OS terminal", not through a piped or redirected stdout.
6. Tray icon. On stock GNOME the icon is usually invisible without an AppIndicator shell extension, and the
   GtkStatusIcon fallback prints Gtk-CRITICAL ... gtk_widget_get_scale_factor warnings. That is a normal
   observation, not a failure. Installing an extension is not part of the install and needs an explicit request.
7. One tray process. If you find two tray PIDs, it was launched twice (autostart plus a manual start). Report the
   PIDs you actually saw.
8. Detached start. bash bin/start.sh stays attached to its terminal: closing that terminal sends SIGHUP to tray.py
   and to its main.py child, so Maestro exits. If the user wants it to survive the window, use the menu entry or
   autostart, or setsid bash bin/start.sh &.

## Windows, in order

1. python 3.11+, and WSL or Git Bash for .sh scripts. Report which one is present.
2. python install.py --yes --no-menu-entry --no-autostart.
3. The five checks, with netstat -ano | findstr 8865 for the listener.
4. Windows has no external terminal emulator path - open_outside uses the internal panel.

## Uninstall, complete

rm -rf <folder> alone is not a full removal, because the installer may have created entries outside the folder.

Linux:

    rm -rf /path/to/Maestro
    rm -f ~/.local/share/applications/maestro.desktop   # application menu entry
    rm -f ~/.config/autostart/maestro.desktop           # autostart entry

Windows: the install directory, the Start Menu shortcut
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Maestro.lnk, and the Maestro value under
HKCU\Software\Microsoft\Windows\CurrentVersion\Run.

Quit the tray process first - one process only. data/ holds the scripts list, settings and logs, so removing the
folder removes the user's scripts too; confirm before deleting it.

## Report format

- **Machine**: distro, Python version, terminal emulator found.
- **Channel**: which one you installed, with the exact tag or commit.
- **What was created**: the files and entries the installer created, marking which prompts were answered by --yes
  rather than asked.
- **Verified**: each check, with the command and its output.
- **Inferred**: what you did not check, kept separate from the verified block.
- **Failures**: in the order they happened, with the exact error text.

---

💛 [Donate on Liberapay](https://liberapay.com/pellaDev/donate)
