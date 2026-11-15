# Instructions for an agent working on Maestro

If you are a model reading this because you were asked to **install** Maestro on a machine, the full procedure is
[docs/agent-install.md](docs/agent-install.md): the order of the steps, the stop conditions, the definition of
"installed", and the report format. Read it before running anything. These are the rules that do not change in any
case:

- Read the README's Requirements and Install sections for this machine before running anything, then follow them in
  order. Do not improvise a step.
- **Stop at the first failure.** Do not continue "to see what happens". A half-installed Maestro - bin/ without
  data/, or a written settings.json with no listener - is worse than nothing installed. Report and wait.
- Never change host (keep 127.0.0.1) or port (keep the documented default 8865) unless the user asked for that
  specific machine. 0.0.0.0 means no authentication: never set it on your own.
- Do not create an autostart entry, an application menu entry, a Start Menu shortcut, or a registry Run value unless
  asked. For an unattended run use --yes --no-menu-entry --no-autostart: the flags answer every prompt with its
  default instead of asking, so there is nothing to answer and nothing to invent.
- Do not install packages, do not open a browser, do not start Maestro, do not enable a shell extension, unless
  asked. If you are asked to run it, run it as the normal user.
- Do not write data/settings.json, a GitHub token, or any secret on the user's behalf unless asked. Maestro's
  defaults are part of how it works.
- An exit code 0 proves the command ran, not that Maestro is installed. To claim "installed", run the five checks in
  the Verify the install section - files, settings, listener on the documented port, UI reachable.
- Keep verified facts apart from inferences in every report. If you cannot read something, say so instead of
  assuming.

If you are working on the code instead of installing it: daemon.py is the process engine, main.py the HTTP server,
tray.py the tray daemon, ui.html + ui.js (+ webui.js for the webapp tabs) the frontend. Both installers must stay in
sync with the README defaults: the port, the flags, the uninstall list. The version badge in ui.html is the single
source for the version string.

---

💛 [Donate on Liberapay](https://liberapay.com/pellaDev/donate)
