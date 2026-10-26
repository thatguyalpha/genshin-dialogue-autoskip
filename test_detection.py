import os
import sys
import ctypes
from collections import deque
from datetime import datetime
from threading import Thread, Event
from time import sleep, perf_counter
from typing import Dict, Any
from ctypes import wintypes

import win32gui
import win32con
import win32api

from autoskip_dialogue import (
    BackgroundFrameGrabber,
    get_coords,
    is_dialogue_box_visible,
    is_advance_marker_visible,
    is_dialogue_choice_visible,
    is_autoplay_enabled,
    is_interact_prompt_visible,
    is_paimon_visible,
    is_map_open,
    is_wish_open,
    is_settings_open,
    is_mailbox_open,
    is_events_open,
    is_party_setup_open,
    is_escape_menu_open,
    is_handbook_open,
    is_katheryne_menu_open,
    is_overworld_active,
)

# ── Console setup ──────────────────────────────────────────────

class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

class CONSOLE_FONT_INFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("nFont", wintypes.DWORD),
        ("dwFontSize", COORD),
        ("FontFamily", wintypes.UINT),
        ("FontWeight", wintypes.UINT),
        ("FaceName", wintypes.WCHAR * 32),
    ]

kernel32 = ctypes.windll.kernel32

TUI_COLS = 54
TUI_LINES = 22


def setup_console():
    h = kernel32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
        kernel32.SetConsoleMode(h, mode.value | 0x0004)

    try:
        hOut = kernel32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        if hOut != -1:
            fi = CONSOLE_FONT_INFOEX()
            fi.cbSize = ctypes.sizeof(fi)
            fi.dwFontSize.Y = 16
            fi.FontFamily = 54
            fi.FontWeight = 400
            fi.FaceName = "Consolas"
            kernel32.SetCurrentConsoleFontEx(hOut, False, ctypes.byref(fi))
            kernel32.CloseHandle(hOut)
    except Exception:
        pass

    os.system(f"mode con: cols={TUI_COLS} lines={TUI_LINES}")

    hwnd = kernel32.GetConsoleWindow()
    if hwnd:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style &= ~(win32con.WS_THICKFRAME | win32con.WS_MAXIMIZEBOX)
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        m = win32gui.GetSystemMenu(hwnd, False)
        if m:
            win32gui.DeleteMenu(m, win32con.SC_SIZE, win32con.MF_BYCOMMAND)
            win32gui.DeleteMenu(m, win32con.SC_MAXIMIZE, win32con.MF_BYCOMMAND)
        win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)


# ── Colors ─────────────────────────────────────────────────────

R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
GRN = "\033[32m"
YEL = "\033[33m"
CYN = "\033[36m"
GRY = "\033[90m"
WHT = "\033[97m"
BG_GRN = "\033[42;97m"
BG_CYN = "\033[46;97m"
BG_GRY = "\033[100;97m"
BG_YEL = "\033[43;30m"

# ── State ──────────────────────────────────────────────────────

exit_event = Event()
on_top = False
events: deque = deque(maxlen=3)
prev_verdict = ""


def set_console_on_top(enable: bool) -> None:
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            top_flag = win32con.HWND_TOPMOST if enable else win32con.HWND_NOTOPMOST
            win32gui.SetWindowPos(
                hwnd,
                top_flag,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            )
    except Exception:
        pass


def log(msg):
    events.append(f"{GRY}{datetime.now().strftime('%H:%M:%S')}{R} {msg}")


def poll_hotkeys():
    global on_top
    last_states = {
        win32con.VK_F7: False,
        win32con.VK_F12: False,
        win32con.VK_ESCAPE: False,
        0x51: False,  # 'Q'
    }
    while not exit_event.is_set():
        for vk in list(last_states.keys()):
            is_down = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
            if is_down and not last_states[vk]:
                if vk == win32con.VK_F7:
                    on_top = not on_top
                    set_console_on_top(on_top)
                    log(f"on top {'on' if on_top else 'off'}")
                elif vk in (win32con.VK_F12, win32con.VK_ESCAPE, 0x51):
                    exit_event.set()
            last_states[vk] = is_down
        sleep(0.02)


# ── TUI Renderer ───────────────────────────────────────────────

def render(arr, coords, w, h, hwnd):
    global prev_verdict
    now = datetime.now().strftime("%H:%M:%S")
    K = "\033[K"
    ot = f"{GRN}on{R}" if on_top else f"{GRY}off{R}"

    d_map = is_map_open(arr, coords)
    d_wish = is_wish_open(arr, coords) if not d_map else False
    d_set = is_settings_open(arr, coords) if (not d_map and not d_wish) else False
    d_mail = is_mailbox_open(arr, coords) if (not d_map and not d_wish and not d_set) else False
    d_evnt = is_events_open(arr, coords) if (not d_map and not d_wish and not d_set and not d_mail) else False
    d_party = is_party_setup_open(arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt) else False
    d_esc = is_escape_menu_open(arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt and not d_party) else False
    d_book = is_handbook_open(arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt and not d_party and not d_esc) else False
    d_kath = is_katheryne_menu_open(arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt and not d_party and not d_esc and not d_book) else False
    d_menu = d_map or d_wish or d_set or d_mail or d_evnt or d_party or d_esc or d_book or d_kath
    # The dialogue panel gates everything. A bare [F] keycap is an interaction
    # prompt (forge/chest/NPC), never proof of dialogue. is_paimon_visible is
    # informational only - it false-positives on bright scene content.
    d_ow = is_overworld_active(arr, coords)
    d_box = is_dialogue_box_visible(arr, coords) if (not d_menu and not d_ow) else False
    d_marker = is_advance_marker_visible(arr, coords) if not d_menu else False
    d_choice = is_dialogue_choice_visible(arr, coords) if not d_menu else False
    active = (d_box or d_marker or d_choice) and not d_ow
    d_auto = is_autoplay_enabled(arr, coords) if active else False
    d_interact = is_interact_prompt_visible(arr, coords, active) if (not d_menu and not active) else False
    d_paim = is_paimon_visible(arr, coords) if not d_menu else False

    # Verdict
    if active:
        triggers = []
        if d_auto: triggers.append("autoplay")
        if d_choice: triggers.append("reply option")
        vstr = "DIALOGUE"
        verdict = f" {BG_GRN}{B} SKIPPING {R}  {GRN}pressing F{R} {GRY}({', '.join(triggers)}){R}"
    elif d_map:
        vstr = "MAP"
        verdict = f" {BG_CYN}{B} MAP {R}  {CYN}map open{R}"
    elif d_wish:
        vstr = "WISH"
        verdict = f" {BG_CYN}{B} WISH {R}  {CYN}wish menu open{R}"
    elif d_set:
        vstr = "SETTINGS"
        verdict = f" {BG_CYN}{B} SETTINGS {R}  {CYN}settings menu open{R}"
    elif d_mail:
        vstr = "MAILBOX"
        verdict = f" {BG_CYN}{B} MAILBOX {R}  {CYN}mailbox open{R}"
    elif d_evnt:
        vstr = "EVENTS"
        verdict = f" {BG_CYN}{B} EVENTS {R}  {CYN}events overview open{R}"
    elif d_party:
        vstr = "PARTY"
        verdict = f" {BG_CYN}{B} PARTY {R}  {CYN}party setup open{R}"
    elif d_esc:
        vstr = "ESCAPE"
        verdict = f" {BG_CYN}{B} ESCAPE {R}  {CYN}escape menu open{R}"
    elif d_book:
        vstr = "HANDBOOK"
        verdict = f" {BG_CYN}{B} HANDBOOK {R}  {CYN}handbook open{R}"
    elif d_kath:
        vstr = "KATHERYNE"
        verdict = f" {BG_CYN}{B} KATHERYNE {R}  {CYN}katheryne menu open{R}"
    elif d_paim:
        vstr = "OVERWORLD"
        verdict = f" {BG_CYN}{B} OVERWORLD {R}  {CYN}dialogue finished{R}"
    elif d_interact:
        vstr = "INTERACT"
        verdict = f" {BG_YEL}{B} INTERACT {R}  {YEL}[F] prompt - not dialogue, ignoring{R}"
    else:
        vstr = "IDLE"
        verdict = f" {BG_GRY} IDLE {R}  {GRY}waiting for dialogue{R}"

    if vstr != prev_verdict and prev_verdict:
        log(f"state -> {B}{vstr.lower()}{R}")
    prev_verdict = vstr

    def tag(v):
        return f"{GRN}{B}ON{R}" if v else f"{GRY}--{R}"

    L = []
    L.append(f" {B}{WHT}genshin detection test{R}        {GRY}{now}{R}{K}")
    L.append(f" {w}x{h}  {GRY}hwnd {WHT}{hwnd}{R}{K}")
    L.append(f" {GRY}f7{R} on top {ot}  {GRY}q/esc{R} quit{K}")
    L.append(f"{GRY}{'-' * TUI_COLS}{R}{K}")
    L.append(f"{K}")
    L.append(f"{verdict}{K}")
    L.append(f"{K}")
    L.append(f" dialogue  {tag(d_box)}{K}")
    L.append(f" diamond   {tag(d_marker)}{K}")
    L.append(f" choice    {tag(d_choice)}{K}")
    L.append(f" autoplay  {tag(d_auto)}{K}")
    L.append(f" interact  {YEL}{B}--{R} shown" if d_interact else f" interact  {GRY}--{R}{K}")
    L.append(f"{K}")
    L.append(f"{GRY}{'-' * TUI_COLS}{R}{K}")

    if events:
        L.append(f"{K}")
        for e in events:
            L.append(f" {e}{K}")

    L.append("\033[J")
    return "\n".join(L)


def main():
    setup_console()
    sys.stdout.write("\033[?25l\033[2J\033[H")
    sys.stdout.flush()

    print(f"\n {B}genshin detection test{R}")
    print(f" {GRY}looking for window...{R}")

    hwnd = 0
    while not exit_event.is_set():
        hwnd = win32gui.FindWindow("UnityWndClass", "Genshin Impact")
        if hwnd:
            break
        sleep(0.5)

    if exit_event.is_set():
        sys.stdout.write("\033[?25h\033[0m\n")
        return

    log(f"attached hwnd {hwnd}")

    grabber = BackgroundFrameGrabber(hwnd)
    grabber.start()

    lw, lh = 0, 0
    coords: Dict[str, Any] = {}

    try:
        while not exit_event.is_set():
            frame = grabber.latest_frame
            if frame is None:
                sleep(0.05)
                continue

            cw, ch = grabber.width, grabber.height
            if cw != lw or ch != lh:
                lw, lh = cw, ch
                coords = get_coords(cw, ch)
                log(f"calibrated {cw}x{ch}")

            sys.stdout.write("\033[H" + render(frame, coords, cw, ch, hwnd))
            sys.stdout.flush()
            sleep(0.1)
    finally:
        grabber.stop()
        sys.stdout.write("\033[?25h\033[0m\033[2J\033[H")
        sys.stdout.flush()


if __name__ == "__main__":
    t_hotkeys = Thread(target=poll_hotkeys, daemon=True)
    t_hotkeys.start()
    try:
        main()
    except KeyboardInterrupt:
        exit_event.set()
        sys.stdout.write("\033[?25h\033[0m\n")
