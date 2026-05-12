#!/usr/bin/env python3
# Maestro - curses-based script launcher
# Config format: STATUS|PATH|PID|DELAY
#   STATUS = ON | OFF | SEP (separator)
#   PATH   = script path  (or separator label when STATUS=SEP)
#   PID    = running process pid (empty if none)
#   DELAY  = launch delay in seconds (0 = no delay / no tag)
#
# Requires wmctrl for single-instance window-raise feature:
#   sudo dnf install wmctrl

import curses
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONFIG_FILE = "/config.txt"
PID_FILE    = "/tmp/Maestro.pid"
WIN_TITLE   = "Maestro"

# Global state
entries       = []   # list of dicts: {status, path, pid, delay}
curr_sel      = -1   # -1 == "LAUNCH ALL" button row
scroll_offset = 0

CLOCK_ICON = "⏱ "   # shown after path when delay > 0


# ─── Single-instance guard ────────────────────────────────────────────────────

def check_single_instance():
    pid_path = Path(PID_FILE)
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text().strip())
            os.kill(existing_pid, 0)
            subprocess.run(["wmctrl", "-a", WIN_TITLE], capture_output=True)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    pid_path.write_text(str(os.getpid()))
    return True


def cleanup_pid():
    try:
        Path(PID_FILE).unlink(missing_ok=True)
    except Exception:
        pass


# ─── Config I/O ──────────────────────────────────────────────────────────────

def pid_is_alive(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("State:"):
                return "Z" not in line
        return True
    except (FileNotFoundError, PermissionError):
        return False


def refresh_pids():
    changed = False
    for e in entries:
        if e["pid"] is not None and not pid_is_alive(e["pid"]):
            e["pid"] = None
            changed = True
    if changed:
        save_config()


def _parse_delay(s):
    """Parse delay string to int, default 0."""
    try:
        return max(0, int(s.strip()))
    except (ValueError, AttributeError):
        return 0


def load_config():
    global entries
    entries = []
    p = Path(CONFIG_FILE)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        parts  = line.split("|", 3)
        status = parts[0].strip()
        path   = parts[1].strip() if len(parts) > 1 else ""
        pid_s  = parts[2].strip() if len(parts) > 2 else ""
        delay  = _parse_delay(parts[3]) if len(parts) > 3 else 0

        # Separator entry
        if status == "SEP":
            entries.append({"status": "SEP", "path": path,
                            "pid": None, "delay": 0})
            continue

        if not path:
            continue

        pid = None
        if pid_s:
            try:
                candidate = int(pid_s)
                if pid_is_alive(candidate):
                    pid = candidate
            except ValueError:
                pass

        entries.append({"status": status, "path": path,
                        "pid": pid, "delay": delay})


def save_config():
    Path(CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                if e["status"] == "SEP":
                    f.write(f"SEP|{e['path']}||\n")
                else:
                    pid_s = str(e["pid"]) if e["pid"] is not None else ""
                    f.write(f"{e['status']}|{e['path']}|{pid_s}|{e['delay']}\n")
        shutil.move(tmp, CONFIG_FILE)
    except Exception:
        pass


# ─── Actions ──────────────────────────────────────────────────────────────────

def _kill_pid(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def launch(idx):
    e = entries[idx]
    if e["status"] == "SEP":
        return

    if e["pid"] is not None:
        _kill_pid(e["pid"])
        e["pid"] = None

    title = e["path"]
    try:
        proc = subprocess.Popen(
            ["ptyxis", "--title", title, "--", "bash", "-c",
             f'printf "\\033]0;{title}\\007"; bash "{e["path"]}"'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        e["pid"] = proc.pid
        save_config()
    except FileNotFoundError:
        pass


def kill_entry(idx):
    e = entries[idx]
    if e["pid"] is None:
        return
    _kill_pid(e["pid"])
    e["pid"] = None
    save_config()


def kill_all():
    for e in entries:
        if e["pid"] is not None:
            _kill_pid(e["pid"])
            e["pid"] = None
    save_config()


def add_entry():
    try:
        result = subprocess.run(
            ["zenity", "--file-selection", "--file-filter=*.sh"],
            capture_output=True, text=True,
        )
        path = result.stdout.strip()
    except FileNotFoundError:
        return
    if path and os.path.isfile(path) and "config.txt" not in path:
        entries.append({"status": "ON", "path": path, "pid": None, "delay": 0})
        save_config()


# ─── Drawing ─────────────────────────────────────────────────────────────────

BANNER = [
    "  ██████╗ ██████╗  ██████╗██╗  ██╗███████╗███████╗████████╗██████╗ ",
    " ██╔═══██╗██╔══██╗██╔════╝██║  ██║██╔════╝██╔════╝╚══██╔══╝██╔══██╗",
    " ██║   ██║██████╔╝██║     ███████║█████╗  ███████╗   ██║   ██████╔╝",
    " ██║   ██║██╔══██╗██║     ██╔══██║██╔══╝  ╚════██║   ██║   ██╔══██╗",
    " ╚██████╔╝██║  ██║╚██████╗██║  ██║███████╗███████║   ██║   ██║  ██║",
    "  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝",
]

HINTS = [
    (" [↵]Launch", 1),
    ("  [X]Toggle",  3),
    ("  [A]Add",     5),
    ("  [S]Sep",     5),
    ("  [T]Timer",   8),
    ("  [U/D]Order", 6),
    ("  [R]Remove",  2),
    ("  [K]Kill",    8),
    ("  [W]Update",  13),
    ("  [Q]Quit",    9),
]


def safe_addstr(scr, row, col, text, attr=0):
    h, w = scr.getmaxyx()
    if row >= h or col >= w:
        return
    try:
        scr.addstr(row, col, text[: w - col - 1], attr)
    except curses.error:
        pass


def inline_input_delay(scr, current_delay):
    """
    Show a delay-input bar at the bottom of the screen.
    Returns new delay int, or current_delay if cancelled (ESC).
    Setting 0 removes the timer tag.
    """
    h, w = scr.getmaxyx()
    curses.curs_set(1)
    buf = list(str(current_delay) if current_delay > 0 else "")
    C_CYAN = curses.color_pair(1)
    C_TEXT = curses.color_pair(11)

    while True:
        bar = h - 2
        scr.move(bar, 0)
        scr.clrtoeol()
        label = f"  ⏱  Delay in seconds (0 or empty = remove timer, ESC = cancel): "
        safe_addstr(scr, bar, 0, label[:w - 2], C_CYAN)
        ll = min(len(label), w - 2)
        val = "".join(buf)
        safe_addstr(scr, bar, ll, (val + " ")[:w - ll - 1], C_TEXT)
        try:
            scr.move(bar, ll + len(buf))
        except curses.error:
            pass
        scr.refresh()

        ch = scr.getch()
        if ch in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            break
        elif ch == 27:
            curses.curs_set(0)
            return current_delay
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif ord("0") <= ch <= ord("9"):
            buf.append(chr(ch))

    curses.curs_set(0)
    raw = "".join(buf).strip()
    return _parse_delay(raw)


def draw(scr):
    global scroll_offset
    scr.erase()
    h, w = scr.getmaxyx()

    C_CYAN    = curses.color_pair(1)
    C_RED     = curses.color_pair(2)
    C_GREEN   = curses.color_pair(3)
    C_DIM     = curses.color_pair(4)
    C_MAGENTA = curses.color_pair(5)
    C_BLUE    = curses.color_pair(6)
    C_DRED    = curses.color_pair(7)
    C_YELLOW  = curses.color_pair(8)
    C_GRAY    = curses.color_pair(9)
    C_SEP     = curses.color_pair(10)
    C_TEXT    = curses.color_pair(11)
    C_BTN     = curses.color_pair(12)
    C_TEAL    = curses.color_pair(13)

    SEP = "─" * w
    row = 0

    for line in BANNER:
        col = max(0, (w - len(line)) // 2)
        safe_addstr(scr, row, col, line, C_CYAN | curses.A_BOLD)
        row += 1

    safe_addstr(scr, row, 0, SEP, C_SEP); row += 1

    # Hints — centered
    hints_total = sum(len(lbl) for lbl, _ in HINTS)
    x = max(0, (w - hints_total) // 2)
    for label, pair in HINTS:
        attr = curses.color_pair(pair)
        if pair == 7: attr |= curses.A_DIM
        if pair == 9: attr |= curses.A_DIM
        safe_addstr(scr, row, x, label, attr)
        x += len(label)
    row += 1

    safe_addstr(scr, row, 0, SEP, C_SEP); row += 1

    # LAUNCH ALL button
    btn_label = "  ▶  LAUNCH ALL SELECTED  "
    btn_col   = max(0, (w - len(btn_label)) // 2)
    if curr_sel == -1:
        safe_addstr(scr, row, btn_col, btn_label, C_BTN | curses.A_BOLD)
    else:
        safe_addstr(scr, row, btn_col, btn_label, C_DIM)
    row += 1

    safe_addstr(scr, row, 0, SEP, C_SEP); row += 1

    # Entry list with viewport + scrollbar
    list_rows = max(1, h - row - 3)
    total     = len(entries)

    if curr_sel < 0:
        view_top = 0
    elif curr_sel < scroll_offset:
        scroll_offset = curr_sel
    elif curr_sel >= scroll_offset + list_rows:
        scroll_offset = curr_sel - list_rows + 1
    view_top = scroll_offset

    show_scrollbar = total > list_rows
    text_width     = w - 2 if not show_scrollbar else w - 3

    for slot in range(list_rows):
        i = view_top + slot
        if i >= total:
            break
        e        = entries[i]
        list_row = row + slot
        selected = (curr_sel == i)

        # ── Separator row ─────────────────────────────────────────────────────
        if e["status"] == "SEP":
            label = e["path"] or "─"
            line  = f"  ───  {label}  ───"
            safe_addstr(scr, list_row, 0, line[:text_width],
                        C_MAGENTA | curses.A_BOLD if selected else C_MAGENTA)
            continue

        # ── Script row ────────────────────────────────────────────────────────
        base = C_CYAN | curses.A_BOLD if selected else C_TEXT

        if e["pid"] is not None:
            dot, dot_attr = "●", C_YELLOW
        else:
            dot, dot_attr = "●", C_DIM

        if e["status"] == "ON":
            marker, m_attr = "[X]", C_GREEN
        else:
            marker, m_attr = "[ ]", C_DIM

        arrow = "> " if selected else "  "
        num   = f"{i + 1}."

        x = 2
        safe_addstr(scr, list_row, x, arrow,  base);     x += len(arrow)
        safe_addstr(scr, list_row, x, dot,    dot_attr); x += len(dot) + 1
        safe_addstr(scr, list_row, x, num,    base);     x += len(num) + 1
        safe_addstr(scr, list_row, x, marker, m_attr);   x += len(marker) + 1

        path_str = e["path"]
        if e["delay"] > 0:
            path_str += f"  {CLOCK_ICON}{e['delay']}s"

        space = max(0, text_width - x)
        # Draw path in base color
        safe_addstr(scr, list_row, x, e["path"][:space], base)
        # If delay set, draw clock tag in yellow after path
        if e["delay"] > 0:
            clock_x = x + len(e["path"])
            clock_str = f"  {CLOCK_ICON}{e['delay']}s"
            safe_addstr(scr, list_row, clock_x,
                        clock_str[:max(0, text_width - clock_x)], C_YELLOW)

    # Scrollbar
    if show_scrollbar and list_rows > 1:
        bar_col    = w - 2
        thumb_size = max(1, list_rows * list_rows // total)
        thumb_top  = (view_top * (list_rows - thumb_size)) // max(1, total - list_rows)
        for sy in range(list_rows):
            ch = "█" if thumb_top <= sy < thumb_top + thumb_size else "░"
            safe_addstr(scr, row + sy, bar_col, ch, C_SEP)

    row += list_rows
    safe_addstr(scr, row, 0, SEP, C_SEP); row += 1

    # Footer
    footer = "..no more docker, thanks."
    fcol = max(0, (w - len(footer)) // 2)
    safe_addstr(scr, min(row, h - 1), fcol, footer, C_SEP | curses.A_ITALIC)
    scr.refresh()


# ─── Main loop ───────────────────────────────────────────────────────────────

def main(scr):
    global curr_sel, entries, scroll_offset

    curses.start_color()
    curses.use_default_colors()

    if curses.can_change_color() and curses.COLORS >= 256:
        curses.init_color(16, 550, 550, 550)
        curses.init_color(17,  80,  80,  80)
        curses.init_color(18, 870, 860, 840)
        FG_GRAY  = 16
        BG_DARK  = 17
        FG_WHITE = 18
    else:
        FG_GRAY  = curses.COLOR_WHITE
        BG_DARK  = curses.COLOR_BLACK
        FG_WHITE = curses.COLOR_WHITE

    curses.init_pair(1,  curses.COLOR_CYAN,    BG_DARK)
    curses.init_pair(2,  curses.COLOR_RED,     BG_DARK)
    curses.init_pair(3,  curses.COLOR_GREEN,   BG_DARK)
    curses.init_pair(4,  FG_GRAY,              BG_DARK)
    curses.init_pair(5,  curses.COLOR_MAGENTA, BG_DARK)
    curses.init_pair(6,  curses.COLOR_BLUE,    BG_DARK)
    curses.init_pair(7,  curses.COLOR_RED,     BG_DARK)
    curses.init_pair(8,  curses.COLOR_YELLOW,  BG_DARK)
    curses.init_pair(9,  curses.COLOR_WHITE,   BG_DARK)
    curses.init_pair(10, FG_GRAY,              BG_DARK)
    curses.init_pair(11, FG_WHITE,             BG_DARK)
    curses.init_pair(12, BG_DARK,              FG_WHITE)

    if curses.can_change_color() and curses.COLORS >= 256:
        curses.init_color(20, 300, 850, 800)   # light teal
        TEAL = 20
    else:
        TEAL = curses.COLOR_CYAN
    curses.init_pair(13, TEAL, BG_DARK)        # update

    scr.bkgd(' ', curses.color_pair(4))
    curses.curs_set(0)
    scr.keypad(True)
    scr.timeout(1000)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(0)

    load_config()
    curr_sel      = -1
    scroll_offset = 0

    while True:
        refresh_pids()
        draw(scr)
        key = scr.getch()
        if key == -1:
            continue

        # ── Navigation ────────────────────────────────────────────────────────
        if key == curses.KEY_UP:
            if curr_sel > -1:
                curr_sel -= 1

        elif key == curses.KEY_DOWN:
            if curr_sel < len(entries) - 1:
                curr_sel += 1

        elif key == curses.KEY_MOUSE:
            try:
                _, _mx, _my, _mz, bstate = curses.getmouse()
                if bstate & curses.BUTTON4_PRESSED:
                    if curr_sel > -1: curr_sel -= 1
                elif bstate & curses.BUTTON5_PRESSED:
                    if curr_sel < len(entries) - 1: curr_sel += 1
            except curses.error:
                pass

        # ── Launch ────────────────────────────────────────────────────────────
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            if curr_sel == -1:
                # Launch all ON entries in order; respect per-entry delay
                first = True
                for i in range(len(entries)):
                    e = entries[i]
                    if e["status"] != "ON" or e["status"] == "SEP":
                        continue
                    # 500 ms gap between consecutive launches (existing behavior)
                    if not first:
                        time.sleep(0.5)
                    # Per-entry delay on top of that
                    if e["delay"] > 0:
                        time.sleep(e["delay"])
                    launch(i)
                    first = False
            else:
                e = entries[curr_sel]
                if e["status"] == "SEP":
                    # Edit separator name on Enter
                    curses.curs_set(1)
                    h, w = scr.getmaxyx()
                    buf = list(e["path"])
                    C_CYAN = curses.color_pair(1)
                    C_TEXT = curses.color_pair(11)
                    while True:
                        bar = h - 2
                        scr.move(bar, 0); scr.clrtoeol()
                        label = "  Separator name (clear+↵ = delete, ESC = cancel): "
                        safe_addstr(scr, bar, 0, label[:w-2], C_CYAN)
                        ll = min(len(label), w-2)
                        val = "".join(buf)
                        safe_addstr(scr, bar, ll, (val+" ")[:w-ll-1], C_TEXT)
                        try: scr.move(bar, ll+len(buf))
                        except curses.error: pass
                        scr.refresh()
                        ch = scr.getch()
                        if ch in (curses.KEY_ENTER, ord("\n"), ord("\r")):
                            break
                        elif ch == 27:
                            buf = list(e["path"]); break
                        elif ch in (curses.KEY_BACKSPACE, 127, 8):
                            if buf: buf.pop()
                        elif 32 <= ch <= 126:
                            buf.append(chr(ch))
                    curses.curs_set(0)
                    new_name = "".join(buf).strip()
                    if new_name == "":
                        entries.pop(curr_sel)
                        if curr_sel >= len(entries):
                            curr_sel = max(-1, len(entries)-1)
                    else:
                        e["path"] = new_name
                    save_config()
                else:
                    if e["delay"] > 0:
                        time.sleep(e["delay"])
                    launch(curr_sel)

        # ── Toggle ON/OFF ─────────────────────────────────────────────────────
        elif key in (ord("x"), ord("X")):
            if curr_sel >= 0:
                e = entries[curr_sel]
                if e["status"] != "SEP":
                    e["status"] = "OFF" if e["status"] == "ON" else "ON"
                    save_config()

        # ── Add script ────────────────────────────────────────────────────────
        elif key in (ord("a"), ord("A")):
            add_entry()

        # ── Separator: add new (on script) or edit (on separator) ─────────────
        elif key in (ord("s"), ord("S")):
            if curr_sel >= 0 and entries[curr_sel]["status"] == "SEP":
                # Edit — handled via Enter (same logic, trigger it)
                key = ord("\n")   # re-route to Enter handler next tick
                # Inline here directly instead:
                e = entries[curr_sel]
                curses.curs_set(1)
                h, w = scr.getmaxyx()
                buf = list(e["path"])
                C_CYAN = curses.color_pair(1); C_TEXT = curses.color_pair(11)
                while True:
                    bar = h-2; scr.move(bar,0); scr.clrtoeol()
                    label = "  Separator name (clear+↵ = delete, ESC = cancel): "
                    safe_addstr(scr, bar, 0, label[:w-2], C_CYAN)
                    ll = min(len(label), w-2)
                    val = "".join(buf)
                    safe_addstr(scr, bar, ll, (val+" ")[:w-ll-1], C_TEXT)
                    try: scr.move(bar, ll+len(buf))
                    except curses.error: pass
                    scr.refresh()
                    ch = scr.getch()
                    if ch in (curses.KEY_ENTER, ord("\n"), ord("\r")): break
                    elif ch == 27: buf = list(e["path"]); break
                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        if buf: buf.pop()
                    elif 32 <= ch <= 126: buf.append(chr(ch))
                curses.curs_set(0)
                new_name = "".join(buf).strip()
                if new_name == "":
                    entries.pop(curr_sel)
                    if curr_sel >= len(entries): curr_sel = max(-1, len(entries)-1)
                else:
                    e["path"] = new_name
                save_config()
            else:
                # Add new separator below current selection
                curses.curs_set(1)
                h, w = scr.getmaxyx()
                buf = []
                C_CYAN = curses.color_pair(1); C_TEXT = curses.color_pair(11)
                while True:
                    bar = h-2; scr.move(bar,0); scr.clrtoeol()
                    label = "  New separator name (ESC = cancel): "
                    safe_addstr(scr, bar, 0, label[:w-2], C_CYAN)
                    ll = min(len(label), w-2)
                    val = "".join(buf)
                    safe_addstr(scr, bar, ll, (val+" ")[:w-ll-1], C_TEXT)
                    try: scr.move(bar, ll+len(buf))
                    except curses.error: pass
                    scr.refresh()
                    ch = scr.getch()
                    if ch in (curses.KEY_ENTER, ord("\n"), ord("\r")): break
                    elif ch == 27: buf = None; break
                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        if buf: buf.pop()
                    elif 32 <= ch <= 126: buf.append(chr(ch))
                curses.curs_set(0)
                if buf is not None:
                    name = "".join(buf).strip() or "Separator"
                    ins  = curr_sel + 1 if curr_sel >= 0 else len(entries)
                    entries.insert(ins, {"status":"SEP","path":name,
                                         "pid":None,"delay":0})
                    curr_sel = ins
                    save_config()

        # ── Timer delay ───────────────────────────────────────────────────────
        elif key in (ord("t"), ord("T")):
            if curr_sel >= 0:
                e = entries[curr_sel]
                if e["status"] != "SEP":
                    new_delay = inline_input_delay(scr, e["delay"])
                    e["delay"] = new_delay
                    save_config()

        # ── Kill ──────────────────────────────────────────────────────────────
        elif key in (ord("k"), ord("K")):
            if curr_sel >= 0:
                kill_entry(curr_sel)

        # ── Remove ────────────────────────────────────────────────────────────
        elif key in (ord("r"), ord("R")):
            if curr_sel >= 0:
                entries.pop(curr_sel)
                if not entries:
                    curr_sel = -1
                elif curr_sel >= len(entries):
                    curr_sel = len(entries) - 1
                save_config()

        # ── Reorder ───────────────────────────────────────────────────────────
        elif key in (ord("u"), ord("U")):
            if curr_sel > 0:
                entries[curr_sel], entries[curr_sel-1] = \
                    entries[curr_sel-1], entries[curr_sel]
                curr_sel -= 1
                save_config()

        elif key in (ord("d"), ord("D")):
            if 0 <= curr_sel < len(entries) - 1:
                entries[curr_sel], entries[curr_sel+1] = \
                    entries[curr_sel+1], entries[curr_sel]
                curr_sel += 1
                save_config()

        # ── Update: run updateX.sh in same folder as selected script ─────────
        elif key in (ord("w"), ord("W")):
            if curr_sel >= 0:
                e = entries[curr_sel]
                if e["status"] != "SEP":
                    update_path = Path(e["path"]).parent / "updateX.sh"
                    if not update_path.exists():
                        draw(scr)
                        h2, w2 = scr.getmaxyx()
                        msg = f"  ✕  {update_path} not found — aborting."
                        try:
                            scr.addstr(h2-2, 0, msg[:w2-1],
                                       curses.color_pair(2) | curses.A_BOLD)
                        except curses.error:
                            pass
                        scr.refresh()
                        scr.timeout(-1); scr.getch(); scr.timeout(1000)
                    else:
                        if e["pid"] is not None:
                            kill_entry(curr_sel)
                        title = str(update_path)
                        try:
                            subprocess.Popen(
                                ["ptyxis","--title",title,"--","bash","-c",
                                 f'printf "\\033]0;{title}\\007"; bash "{update_path}"'],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True,
                            )
                        except FileNotFoundError:
                            pass

        # ── Quit ──────────────────────────────────────────────────────────────
        elif key in (ord("q"), ord("Q")):
            kill_all()
            break


if __name__ == "__main__":
    print(f"\033]0;{WIN_TITLE}\007", end="", flush=True)

    if not check_single_instance():
        sys.exit(0)

    try:
        curses.wrapper(main)
    finally:
        kill_all()
        cleanup_pid()
