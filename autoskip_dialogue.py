import os
import sys
import ctypes
import base64
import zlib
from collections import deque
from datetime import datetime
from random import randint, uniform
from threading import Thread
from time import perf_counter, sleep
from typing import Union, Dict, Any, Optional
from ctypes import wintypes

import win32gui
import win32con
import win32api
from windows_capture import WindowsCapture, Frame, InternalCaptureControl
import numpy as np
import cv2
from pynput.keyboard import Key, KeyCode, Listener  # type: ignore[import-untyped]
from dotenv import load_dotenv  # type: ignore[import-not-found]

load_dotenv()

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


# ── ANSI colors ────────────────────────────────────────────────

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


# ── Frame Grabber ──────────────────────────────────────────────

class BackgroundFrameGrabber:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.latest_frame = None
        self.width = 0
        self.height = 0
        self.capture = None
        self.capture_control = None

    def start(self) -> None:
        self.capture = WindowsCapture(
            window_hwnd=self.hwnd,
            cursor_capture=False,
            draw_border=False
        )

        @self.capture.event
        def on_frame_arrived(frame: Frame, ctrl: InternalCaptureControl) -> None:
            raw_buf = frame.frame_buffer
            fw, fh = frame.width, frame.height
            try:
                client_rect = win32gui.GetClientRect(self.hwnd)
                cw = client_rect[2] - client_rect[0]
                ch = client_rect[3] - client_rect[1]
                if cw > 0 and ch > 0 and (fw > cw or fh > ch):
                    cw = min(cw, fw)
                    ch = min(ch, fh)
                    diff_w = fw - cw
                    diff_h = fh - ch
                    border_x = max(0, diff_w // 2)
                    title_h = max(0, diff_h - border_x)
                    end_y = min(fh, title_h + ch)
                    end_x = min(fw, border_x + cw)
                    self.latest_frame = raw_buf[title_h:end_y, border_x:end_x].copy()
                    self.width = end_x - border_x
                    self.height = end_y - title_h
                else:
                    self.latest_frame = raw_buf.copy()
                    self.width = fw
                    self.height = fh
            except Exception:
                self.latest_frame = raw_buf.copy()
                self.width = fw
                self.height = fh

        @self.capture.event
        def on_closed() -> None:
            self.latest_frame = None

        self.capture_control = self.capture.start_free_threaded()

    def stop(self) -> None:
        if self.capture_control:
            try:
                self.capture_control.stop()
            except Exception:
                pass
            self.capture_control = None


# ── Coordinate calculation ─────────────────────────────────────

def get_coords(w: int, h: int) -> Dict[str, Any]:
    aspect = w / h if h > 0 else 16.0 / 9.0
    if aspect <= 16.0 / 9.0:
        ui_scale = w / 1920.0
        x_offset = 0
    else:
        ui_scale = h / 1080.0
        x_offset = int((w - 1920.0 * ui_scale) / 2.0)

    # ── Autoplay pill (top-left "Auto" toggle) ───────────────────────────
    # Lit pill means autoplay is ON. Unlit means autoplay is off or has been
    # suspended by the game because a reply choice is pending.
    autoplay_x0 = int(52 * ui_scale)
    autoplay_x1 = int(96 * ui_scale)
    autoplay_y0 = int(36 * ui_scale)
    autoplay_y1 = int(60 * ui_scale)

    # ── Dialogue body text band ─────────────────────────────────────────
    # White serif body copy of the dialogue panel. This is the only signal
    # that actually means "there is dialogue on screen to skip".
    # y >= 890 excludes the gold speaker name (845..890) and the reply-option
    # pills (700..830). x <= 1745 excludes the UID watermark (1740..1900).
    dialogue_text_x0 = x_offset + int(180 * ui_scale)
    dialogue_text_x1 = x_offset + int(1745 * ui_scale)
    dialogue_text_y0 = int(890 * ui_scale)
    dialogue_text_y1 = int(1022 * ui_scale)

    paimon_white = [
        (int(47 * ui_scale), int(30 * ui_scale)),
        (int(55 * ui_scale), int(30 * ui_scale)),
        (int(60 * ui_scale), int(50 * ui_scale)),
        (int(55 * ui_scale), int(55 * ui_scale)),
    ]
    paimon_gold = [
        (int(66 * ui_scale), int(22 * ui_scale)),
        (int(68 * ui_scale), int(18 * ui_scale)),
        (int(73 * ui_scale), int(25 * ui_scale)),
    ]
    paimon_blue = [
        (int(40 * ui_scale), int(76 * ui_scale)),
        (int(50 * ui_scale), int(78 * ui_scale)),
        (int(60 * ui_scale), int(78 * ui_scale)),
    ]

    return {
        "ui_scale": ui_scale,
        "x_offset": x_offset,
        "autoplay_x0": autoplay_x0,
        "autoplay_x1": autoplay_x1,
        "autoplay_y0": autoplay_y0,
        "autoplay_y1": autoplay_y1,
        "dialogue_text_x0": dialogue_text_x0,
        "dialogue_text_x1": dialogue_text_x1,
        "dialogue_text_y0": dialogue_text_y0,
        "dialogue_text_y1": dialogue_text_y1,
        "paimon_white": paimon_white,
        "paimon_gold": paimon_gold,
        "paimon_blue": paimon_blue,
        "map_gear_x": int(54 * ui_scale),
        "map_gear_y": h - int(60 * ui_scale),
        "map_close_x": w - int(77 * ui_scale),
        "map_close_y": int(48 * ui_scale),
        "wish_star_x": int(56 * ui_scale),
        "wish_star_y": int(48 * ui_scale),
        "wish_btn_x": w - int(240 * ui_scale),
        "wish_btn_y": h - int(60 * ui_scale),
        "exit_btn_x": int(42 * ui_scale),
        "exit_btn_y": h - int(54 * ui_scale),
    }


# ── Timing helpers ─────────────────────────────────────────────

def random_f_key_interval() -> float:
    return uniform(0.18, 0.2) if randint(1, 6) == 6 else uniform(0.12, 0.18)

def should_take_break() -> bool:
    return randint(1, 25) == 1

def take_random_break() -> float:
    return uniform(3.0, 8.0)


# ── Notification sound ─────────────────────────────────────────

NOTIFY_SOUND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.mp3")
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# The "dialogue finished" chime means the bot has stopped working and it is
# your turn again, so it must not land while presses are still going out.
NOTIFY_QUIET_SECONDS = 10.0

def play_notification_sound() -> None:
    try:
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW("close notify_snd", None, 0, 0)
        winmm.mciSendStringW(f'open "{NOTIFY_SOUND_PATH}" type mpegvideo alias notify_snd', None, 0, 0)
        winmm.mciSendStringW("play notify_snd from 0", None, 0, 0)
    except Exception:
        pass


# ── Detection functions ────────────────────────────────────────

def check_pixel(arr: Any, x: int, y: int, target_rgb: tuple, tolerance: int = 8) -> bool:
    try:
        pixel = arr[y, x]
        return (abs(int(pixel[0]) - target_rgb[2]) <= tolerance and
                abs(int(pixel[1]) - target_rgb[1]) <= tolerance and
                abs(int(pixel[2]) - target_rgb[0]) <= tolerance)
    except Exception:
        return False


# ── Advance diamond template ─────────────────────────────────────
# A 56x56 colour-filtered context window centred on the advance diamond at
# (960,1045) in a 1920x1080 frame. It is a soft gold mask, not raw pixels:
# matching against a binary colour filter makes the score invariant to
# brightness and contrast, which is what lets it survive different scenes.
_ADVANCE_TPL_B64 = (
    "eNrt0jERwDAMQ1EzMBRBKIRACwRDCIRCMoT2ck0zR5719j9JZiIiIocc1wY/79DvrYPoIp8lg+i8jRXmaG58SGZ/SGdf"
    "WMhmGFHI5ohwXVlE6l7A0jWh"
)
_ADVANCE_TPL = np.frombuffer(
    zlib.decompress(base64.b64decode(_ADVANCE_TPL_B64)), dtype=np.uint8
).reshape((56, 56)).astype(np.float32) / 255.0

_ADVANCE_CX = 960.0
_ADVANCE_CY = 1045.0
_ADVANCE_SCALES = (0.7, 0.8, 0.9, 1.0, 1.15, 1.3)
_ADVANCE_MIN_SCORE = 0.65


def _gold_mask(im):
    b = im[:, :, 0].astype(np.int16)
    g = im[:, :, 1].astype(np.int16)
    r = im[:, :, 2].astype(np.int16)
    return ((r >= 185) & (g >= 125) & (b <= 115) & ((r - b) >= 85)
            & ((r - g) >= 40) & (r >= g) & (g >= b)).astype(np.float32)


def is_advance_marker_visible(arr: Any, coords: dict) -> bool:
    """The gold advance diamond below the dialogue text is showing.

    Restored after being removed, and rebuilt. The original implementation
    probed a fixed pixel and demanded a dark surround; it never fired once,
    because the probe point was 19px above the glyph and because the "dark
    surround" assumption is false outdoors.

    This is the "filter then rate the shape" approach: filter to UI gold,
    correlate a context window against it over a small scale pyramid. Binary
    colour masks make the score contrast-invariant, the pyramid handles
    resolution, and the context window is what stops a similarly-coloured blob
    (the health bar, red foliage, lanterns) from matching.

    Measured over the labelled set: dialogue frames carrying the marker score
    0.857-0.962, everything else tops out at 0.451. Threshold 0.65.

    Note the marker is only on screen for part of a dialogue, so this
    corroborates the text-band gate rather than replacing it.
    """
    try:
        height, width = arr.shape[:2]
        aspect = width / height if height > 0 else 16.0 / 9.0
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))
        hw = int(110 * s)
        # The diamond sits dead centre on 16:9, so _ADVANCE_CX is half of 1920.
        # On ultrawide the game anchors UI to height and centres it on width,
        # so this x must be width/2 - scaling the 1920-space constant by the
        # height-based scale instead put the probe at x=1280 on a 3440-wide
        # window while the diamond sat at x=1720, scoring a hard 0.000 and
        # silently disabling the marker gate. Same convention as the choice
        # column in is_dialogue_choice_visible.
        cx = (int(_ADVANCE_CX * s) if aspect <= 16.0 / 9.0
              else int(round(width / 2 + (_ADVANCE_CX - 960.0) * s)))
        cy = int(_ADVANCE_CY * s)
        rx0, rx1 = max(0, cx - hw), min(width, cx + hw)
        ry0, ry1 = max(0, cy - int(50 * s)), min(height, cy + int(50 * s))
        if rx1 - rx0 < 24 or ry1 - ry0 < 24:
            return False
        region = _gold_mask(arr[ry0:ry1, rx0:rx1])
        if min(region.shape) < 56:
            return False

        best = 0.0
        for sc in _ADVANCE_SCALES:
            tw = int(round(56 * sc * s))
            if tw < 16 or tw >= region.shape[1] or tw >= region.shape[0]:
                continue
            tpl = cv2.resize(_ADVANCE_TPL, (tw, tw), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
            best = max(best, float(cv2.minMaxLoc(res)[1]))
            if best >= _ADVANCE_MIN_SCORE:
                return True
        return best >= _ADVANCE_MIN_SCORE
    except Exception:
        return False


def is_dialogue_box_visible(arr: Any, coords: dict) -> bool:
    """Primary gate: is the dialogue panel on screen?

    This is the only signal that means "there is dialogue to skip". Everything
    else is a refinement.

    Three conditions, all required. Each was measured over 19 dialogue frames and
    44 non-dialogue ones; see .wip/02-detection-reference.md.

    1. Enough near-white pixels in the body-text band. Sunlit stone and the
       health bar both produce white pixels here, so the threshold is 1500:
       a real one-line dialogue measures 1779 and the worst overworld false
       positive measures 1047. That band is narrow, which is why the marker
       below is OR-ed in as a second, independent route.
    2. No long unbroken white run. Dialogue text is broken up by letter and word
       spacing (longest run 16-31px at 1920x1080); menu panels produce runs of
       182-289.
    3. The gold nameplate rule above the band. Every dialogue panel draws one;
       the open world does not. This is what rejects bright scenery.
    """
    try:
        height, width = arr.shape[:2]
        s = coords.get("ui_scale", width / 1920.0)

        x0 = max(0, coords["dialogue_text_x0"])
        x1 = min(width, coords["dialogue_text_x1"])
        y0 = max(0, coords["dialogue_text_y0"])
        y1 = min(height, coords["dialogue_text_y1"])
        if x1 - x0 < 16 or y1 - y0 < 8:
            return False
        # Genshin never renders below 720p, where this band is 88px tall, so a
        # thinner band means a cropped region capture rather than a game frame.
        if (y1 - y0) < 60:
            return False

        # -- 3. gold nameplate rule ---------------------------------------
        ry0 = max(0, int(820 * s))
        ry1 = min(height, int(895 * s))
        x_offset = coords.get("x_offset", 0)
        rx0 = x_offset + int(200 * s)
        rx1 = min(width, x_offset + int(1650 * s))
        if ry1 - ry0 < 8 or rx1 - rx0 < 16:
            return False
        rule = arr[ry0:ry1, rx0:rx1]
        rb = rule[:, :, 0].astype(np.int16)
        rg = rule[:, :, 1].astype(np.int16)
        rr = rule[:, :, 2].astype(np.int16)
        gold = (rr >= 170) & (rg >= 100) & (rb <= 130) & ((rr - rb) >= 70) & (rr >= rg) & (rg >= rb)
        max_gold = int(gold.sum(axis=1).max()) if rule.size else 0

        # -- 1 + 2. dialogue body text ------------------------------------
        strip = arr[y0:y1, x0:x1]
        if not np.any(strip > 232):
            return False

        b = strip[:, :, 0]
        g = strip[:, :, 1]
        r = strip[:, :, 2]
        white = (r >= 232) & (g >= 232) & (b >= 232)
        white_cnt = int(np.count_nonzero(white))

        ys, xs = np.where(white)
        if len(xs):
            mean_x = xs.mean() + x0
            center_x = width / 2.0
            if abs(mean_x - center_x) > 95.0 * s:
                if not is_dialogue_choice_visible(arr, coords):
                    return False

        max_run = 0
        limit = 150 * s
        for row in white:
            idx = np.flatnonzero(row)
            if idx.size == 0:
                continue
            brk = np.flatnonzero(np.diff(idx) != 1)
            if brk.size == 0:
                max_run = max(max_run, int(idx.size))
                continue
            starts = np.concatenate(([0], brk + 1))
            ends = np.concatenate((brk, [idx.size - 1]))
            max_run = max(max_run, int((ends - starts + 1).max()))
            if max_run >= limit:
                return False

        # When a prominent gold nameplate rule is present (named speaker like Paimon),
        # short dialogue lines (e.g. "Hehehe...") measure ~960px at 1080p, so accept >= 600 * s * s.
        # When no prominent nameplate rule is present (monologue / unnamed speaker),
        # require >= 1400 * s * s and the base gold rule (>= 20 * s).
        if white_cnt >= int(1400 * s * s):
            return max_gold >= int(20 * s)
        elif white_cnt >= int(600 * s * s):
            return max_gold >= int(60 * s)
        return False
    except Exception:
        return False


def is_autoplay_enabled(arr: Any, coords: dict) -> bool:
    """Is the top-left Auto pill lit (autoplay ON)?

    Note this is NOT the same as "dialogue is playing" - the game also dims the
    pill whenever a reply choice is pending. Measured counts are ~680 when lit
    and exactly 0 when unlit, so the threshold is not delicate.
    """
    try:
        height, width = arr.shape[:2]
        s = coords.get("ui_scale", width / 1920.0)
        x0 = max(0, coords["autoplay_x0"])
        x1 = min(width, coords["autoplay_x1"])
        y0 = max(0, coords["autoplay_y0"])
        y1 = min(height, coords["autoplay_y1"])
        if x1 - x0 < 4 or y1 - y0 < 4:
            return False

        box = arr[y0:y1, x0:x1]
        b = box[:, :, 0].astype(np.int16)
        g = box[:, :, 1].astype(np.int16)
        r = box[:, :, 2].astype(np.int16)
        cream = np.count_nonzero(
            (np.abs(r - 236) <= 12) & (np.abs(g - 229) <= 12) & (np.abs(b - 216) <= 12)
        )
        return cream >= 120 * s * s
    except Exception:
        return False


_FKEY_TPL_B64 = (
    "eJxrmn3387//eMG/Lzc6bLd9xq8IAn5MeE2Msv//H3wnTt1X4pT9+4vC/fvhLRx8QPLdv3+oXn3QVlkBA1V3/yKpQzXv9QQLIzjY/AtJGaq6/08cjYxMJsw"
    "AgikFM77jVvfXDaju0UcgeHftxG+EL/7+QfOYu7GRyXsI8+c/hLo/eNQheZd4db+xqNu4GwTu/yWgzigkHARW/ySgzjg6DgTWo6j7hanOZP9pEHj6l5A6bP"
    "4gVh02/xKpzuoDceqCPmFRhxkfpnN+YKrDSAeR8dOf/SVC3cEz7zFzM2b6ww6IVYeRP3Cr+0egbIEp/PuWKHX/Px8lzsDrxXeIUfa+2j9+5wsCnvn39lCmIw"
    "Cfksfc"
)
_FKEY_TPL = np.frombuffer(zlib.decompress(base64.b64decode(_FKEY_TPL_B64)), dtype=np.uint8).reshape((33, 40))


def find_f_keycap(arr: Any, coords: dict) -> Optional[tuple]:
    """Locate the [F] keycap prompt. Returns (centre_x, centre_y, score) or None.

    The search window is a *fraction of the frame*, not scaled pixels. On
    ultrawide the game anchors UI to height and centres it, so an absolute
    `ui_scale` x lands in the wrong place.

    IMPORTANT: this keycap is a *generic* interaction prompt. Genshin shows it
    whenever the player stands near anything interactable (chest, forge,
    device, NPC) as well as for pending dialogue replies. A world prompt and a
    dialogue reply score 0.96+ on the same template and are pixel-identical in
    shape, so this function cannot tell them apart - callers must not treat its
    presence as proof of dialogue.
    """
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))

        cur_tw = max(16, int(round(40 * s)))
        cur_th = max(14, int(round(33 * s)))
        cur_tpl = cv2.resize(_FKEY_TPL, (cur_tw, cur_th), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR)

        fx0, fx1, fy0, fy1 = 0.40, 0.98, 0.40, 0.90

        x1 = max(0, int(fx0 * width))
        x2 = min(width, int(fx1 * width))
        y1 = max(0, int(fy0 * height))
        y2 = min(height, int(fy1 * height))
        if x2 - x1 < cur_tw or y2 - y1 < cur_th:
            return None

        strip = arr[y1:y2, x1:x2]
        if not np.any(strip > 230):
            return None

        gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(gray, cur_tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, loc = cv2.minMaxLoc(res)
        if max_val < 0.80:
            return None
        return (x1 + loc[0] + cur_tw // 2, y1 + loc[1] + cur_th // 2, float(max_val))
    except Exception:
        return None


def is_dialogue_choice_visible(arr: Any, coords: dict) -> bool:
    """Is a dialogue reply choice prompt [F] visible in the choice column?

    Dialogue reply options appear strictly in the choice column on the right
    (cx ≈ 1228 at 1080p, or centered offset on ultrawide), accompanied by
    a speech bubble icon or option pill badge.
    """
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))
        res = find_f_keycap(arr, coords)
        if not res:
            return False
        cx, cy, sc = res
        if sc < 0.80:
            return False
        expected_choice_x = int(round(1228 * s)) if aspect <= 16.0 / 9.0 else int(round(width / 2 + 268 * (height / 1080.0)))
        if abs(cx - expected_choice_x) > max(12, int(15 * s)):
            return False
        # Check speech bubble / choice icon to the right of the [F] keycap
        r_y0 = max(0, int(cy - 16 * s))
        r_y1 = min(height, int(cy + 16 * s))
        r_x0 = max(0, int(cx + 18 * s))
        r_x1 = min(width, int(cx + 55 * s))
        bubble_crop = arr[r_y0:r_y1, r_x0:r_x1]
        if bubble_crop.size == 0:
            return False
        white_px = np.sum(np.all(bubble_crop > 215, axis=2))
        return white_px >= int(25 * s * s)
    except Exception:
        return False


def is_interact_prompt_visible(arr: Any, coords: dict, box_visible: bool = True) -> bool:
    """An [F] keycap is showing for a *world* interaction, not for dialogue.

    Never contributes to pressing F. It exists only so the UI can say the bot is
    deliberately ignoring an interaction prompt, and as the groundwork for the
    targeted interactions (Katheryne services and so on) that are planned but
    not implemented yet.

    World interaction prompts appear near the screen centre (cx ≈ 1119 at 1080p),
    unlike dialogue reply choices which sit in the fixed choice column (cx ≈ 1228)
    with speech bubble icons.
    """
    if box_visible:
        return False
    try:
        if is_dialogue_choice_visible(arr, coords):
            return False
        return find_f_keycap(arr, coords) is not None
    except Exception:
        return False


# Anything a player would plausibly press while driving. If any of these is
# held, or was pressed within USER_YIELD_SECONDS, the bot stands down - you
# should never have to fight it for control of the character.
_USER_INPUT_VKS = (
    0x57, 0x41, 0x53, 0x44,        # W A S D
    0x51, 0x45,                    # Q E
    0x10, 0xA0, 0xA1,              # shift
    0x20,                          # space
    0x09,                          # tab
    0x1B,                          # esc
    0x12,                          # ctrl
    0x31, 0x32, 0x33, 0x34, 0x35, 0x36,   # 1-6
    0x60, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66,  # numpad
    0x01, 0x02, 0x04, 0x08,        # mouse left/right/middle/x
)
USER_YIELD_SECONDS = 1.5
_last_user_input = 0.0


def is_user_providing_input() -> bool:
    for vk in _USER_INPUT_VKS:
        if win32api.GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


def note_user_input(now: float, game_focused: bool = True) -> None:
    """Remember when the player last touched the keyboard or mouse.

    Only meaningful while the game actually has focus. GetAsyncKeyState is a
    *global* poll - it reports keys held in any application, with no regard for
    which window is foreground. Sampling it unconditionally meant that typing
    in chat, clicking on the desktop, or working in the very terminal running
    this bot all registered as "the player is driving", and the bot stood down
    for 1.5s after every stray keystroke even with the game in the background.
    That presented as random stalls: the verdict still read "pressing F" while
    the counter stood still, and it recovered on its own once the player
    stopped typing.

    Outside the game there is nothing to yield to - the character cannot be
    moved by input aimed at another window - so the poll is skipped entirely.
    """
    global _last_user_input
    if not game_focused:
        return
    if is_user_providing_input():
        _last_user_input = now


def user_is_driving(now: float) -> bool:
    """True while the player is actively driving - the bot must not press F."""
    return (now - _last_user_input) < USER_YIELD_SECONDS


_input_error_reported = False


def _report_input_error(exc: Exception) -> None:
    """Surface injection failures once - a silent denial looks like success."""
    global _input_error_reported
    if _input_error_reported:
        return
    _input_error_reported = True
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    msg = f"input failed: {exc}"
    if "denied" in str(exc).lower() or getattr(exc, "winerror", None) == 5:
        msg += " - this usually means the script is not running as administrator. "
        msg += "run.bat requests elevation; UIPI blocks all input to the game otherwise."
    elif not is_admin:
        msg += " - script is not elevated; run.bat requests administrator rights."
    try:
        app.log(msg)
    except Exception:
        print(msg)


def game_has_focus(hwnd: int) -> bool:
    """True when the OS foreground belongs to the game, popups included.

    Comparing GetForegroundWindow() == hwnd is not enough. The foreground
    window is whatever window owns input right now, and that is frequently a
    *child* or *owned popup* of the game rather than the game window itself:
    right-clicking inside Genshin pops a cursor-confine/tooltip/shell window,
    and a foreground-flash from the taskbar does the same.

    Missing those is what locks the player out. A strict compare answers
    "background" while the player is literally tabbed into the game, so the
    bot primes the window as active, presses F, then hands the window back
    inactive - WM_KILLFOCUS to a window Windows still believes is foreground.
    Unity then drops every keystroke and mouse click until the user alt-tabs
    out and back in. Right-click was the reliable way to trigger it.

    GA_ROOTOWNER also covers owned popups (menus, dialogs); GA_ROOT covers
    plain children. Either match means the game owns the foreground.
    """
    if not hwnd:
        return False
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        return False
    if not fg or fg == hwnd:
        return bool(fg)
    for flag in (win32con.GA_ROOTOWNER, win32con.GA_ROOT):
        try:
            if win32gui.GetAncestor(fg, flag) == hwnd:
                return True
        except Exception:
            continue
    return False


def _activation_messages(hwnd: int, active: bool) -> None:
    """Walk the game's WndProc through an activate/deactivate cycle.

    This is the priming that makes injected keys land when the game is in the
    background. It is NOT SetForegroundWindow - no OS focus is taken, so you
    keep whatever window you were using. Measured live: PostMessage on its own
    does nothing in the background, PostMessage after this cycle works (band
    delta 14-46 vs 0.3).

    Never call the inactive half on a window that currently has focus - see
    game_has_focus() for why that locks the player out.
    """
    if active:
        win32gui.PostMessage(hwnd, win32con.WM_ACTIVATE,
                             win32con.WA_ACTIVE, 0)
        win32gui.PostMessage(hwnd, win32con.WM_SETFOCUS, 0, 0)
    else:
        # WM_KILLFOCUS before WM_ACTIVATE(WA_INACTIVE), not the other way
        # round. Unity tears down its input pump on kill-focus and only
        # rebuilds it on the activate that follows, so this ordering is what
        # leaves the window cleanly inert while the user works elsewhere.
        win32gui.PostMessage(hwnd, win32con.WM_KILLFOCUS, 0, 0)
        win32gui.PostMessage(hwnd, win32con.WM_ACTIVATE,
                             win32con.WA_INACTIVE, 0)


def _reactivate(hwnd: int) -> None:
    """Re-sync a focused game window that a previous press left inert.

    Each background press ends with WM_KILLFOCUS + WM_ACTIVATE(WA_INACTIVE),
    so between presses the game's WndProc believes it has no focus. Alt-tabbing
    back in makes Windows send its own WM_ACTIVATE(WA_ACTIVE) + WM_SETFOCUS,
    which normally repairs that - but not reliably. When the repair misses,
    the game is genuinely foreground while its input pump stays torn down, and
    the symptom is the worst kind: your keystrokes and clicks go nowhere and
    only tabbing out and back in brings them back.

    So on the foreground transition, send the activate pair again ourselves.
    It is cheap, it is what Windows just sent anyway, and it is idempotent:
    the window really is focused, so we are not lying to the WndProc the way
    the deactivate half does. This is the difference between "alt-tab in and
    it works" and "alt-tab in and you are a ghost until you alt-tab twice".
    """
    win32gui.PostMessage(hwnd, win32con.WM_ACTIVATE,
                         win32con.WA_ACTIVE, 0)
    win32gui.PostMessage(hwnd, win32con.WM_SETFOCUS, 0, 0)


def press_f_background(hwnd: int, is_active: Optional[bool] = None) -> None:
    """Send F to the game window by PostMessage only.

    Background operation: the key goes to the Genshin hwnd directly and the OS
    focus is never taken, so you can keep using the machine while the bot runs.
    keybd_event()/SendInput() are deliberately NOT used - they inject into
    whatever the OS considers focused, which is how presses land on other windows.

    Focus safety (see game_has_focus for the why). The two halves of the
    priming cycle are NOT equally dangerous, and treating them as one thing is
    what broke both "skip in background" and "play in foreground":

    - The activate half (WM_ACTIVATE(WA_ACTIVE) + WM_SETFOCUS) is what makes
      the key actually land. Measured live, bare PostMessage does nothing even
      when the window is foreground; the primed press works. It is also
      idempotent here: when the window really is foreground we are repeating
      what Windows already did, not inventing a new state. So it is sent
      unconditionally, on every press.
    - The deactivate half (WM_KILLFOCUS + WM_ACTIVATE(WA_INACTIVE)) is the
      only genuinely destructive part. It tears down Unity's input pump. Sent
      to a window the OS still considers foreground, it leaves the game
      looking active while being functionally dead - WASD and mouse stop
      responding until the user alt-tabs out and back in. So it is sent only
      when the game is genuinely in the background, and it is re-checked
      immediately before sending, because focus can change during the press
      (a right-click or an alt-tab landing mid-press is enough).
    """
    # The caller's hint is only a hint. Focus can change between the check in
    # the main loop and here, and the deactivate half is the part that does
    # real damage, so re-derive the truth here rather than trusting it.
    is_active = game_has_focus(hwnd)

    scan_code = 0x21
    lparam_down = 1 | (scan_code << 16)
    lparam_up = lparam_down | (1 << 30) | (1 << 31)
    try:
        # Prime unconditionally - this is what makes the key land.
        _activation_messages(hwnd, True)
        sleep(0.03)
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, 0x46, lparam_down)
        sleep(0.04)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, 0x46, lparam_up)
        sleep(0.03)
        # Hand the window back inert only if it is still in the background.
        # Never tear down the input pump of a window the player is using.
        if not is_active and not game_has_focus(hwnd):
            _activation_messages(hwnd, False)
    except Exception as exc:  # noqa: BLE001
        _report_input_error(exc)


def is_paimon_visible(arr: Any, coords: dict) -> bool:
    try:
        wh = sum(1 for sx, sy in coords["paimon_white"]
                 if int(arr[sy, sx][2]) > 220 and int(arr[sy, sx][1]) > 220 and int(arr[sy, sx][0]) > 220)
        gh = sum(1 for sx, sy in coords["paimon_gold"]
                 if 200 <= int(arr[sy, sx][2]) <= 255 and 160 <= int(arr[sy, sx][1]) <= 220
                 and 110 <= int(arr[sy, sx][0]) <= 170 and int(arr[sy, sx][2]) - int(arr[sy, sx][0]) >= 50)
        bh = sum(1 for sx, sy in coords["paimon_blue"]
                 if 35 <= int(arr[sy, sx][2]) <= 95 and 75 <= int(arr[sy, sx][1]) <= 135
                 and 120 <= int(arr[sy, sx][0]) <= 185 and int(arr[sy, sx][0]) > int(arr[sy, sx][2]))
        return (int(wh >= 3) + int(gh >= 2) + int(bh >= 2)) >= 2
    except Exception:
        return False


def is_overworld_active(arr: Any, coords: dict) -> bool:
    """Check if open-world HUD elements (player HP bar) are visible.

    In Genshin Impact, the overworld HUD (HP bar, elemental skill icons) is
    strictly hidden during dialogue cutscenes and overlay menus. Its presence
    indicates the player is walking around or in combat in the overworld.
    """
    try:
        height, width = arr.shape[:2]
        s = height / 1080.0
        y0 = int(1000 * s)
        y1 = min(height, int(1025 * s))
        x0 = int(width / 2.0 - 210 * s)
        x1 = min(width, int(width / 2.0 + 210 * s))
        crop = arr[y0:y1, x0:x1]
        if crop.size > 0:
            b = crop[:, :, 0].astype(int)
            g = crop[:, :, 1].astype(int)
            r = crop[:, :, 2].astype(int)
            # Lime green player HP bar
            lime = (g >= 175) & (r <= 180) & (b <= 110) & (g - r >= 35) & (g - b >= 60)
            if np.sum(lime) >= int(100 * s * s):
                return True
        return False
    except Exception:
        return False


def is_map_open(arr: Any, coords: dict) -> bool:
    try:
        height, width = arr.shape[:2]
        s = coords.get("ui_scale", width / 1920.0)
        gx, gy = coords["map_gear_x"], coords["map_gear_y"]
        cx, cy = coords["map_close_x"], coords["map_close_y"]

        # 1. Gear icon must be present *and* shaped like a glyph, not just be
        # "a warm-white pixel somewhere near here".
        #
        # The old test was a bare warm-white pixel check, which any bright sky
        # satisfies. A Fontaine cutscene with the camera tilted up scores 441
        # warm-white pixels in this patch, reads as an open map, sets d_menu,
        # and silently stops dialogue skipping. The real gear is a glyph: a
        # compact set of bright strokes on a dark surround, so it is a minority
        # of its patch and has strong local contrast. Measured on the false
        # positive vs a real map: 441 warm px at local stddev 0.7 (flat sky)
        # against 105 warm px at stddev 70 (actual icon). Density and contrast
        # each reject flat scenery, and requiring both also rejects bright
        # textured rock.
        if not check_pixel(arr, gx, gy, (236, 229, 216), tolerance=12):
            return False
        v = max(6, int(10 * s))
        x0, x1 = max(0, gx - v), min(width, gx + v + 1)
        y0, y1 = max(0, gy - v), min(height, gy + v + 1)
        patch = arr[y0:y1, x0:x1]
        if patch.size == 0:
            return False
        warm = (patch[:, :, 2] > 224) & (patch[:, :, 1] > 217) & (patch[:, :, 0] < 232)
        warm_count = int(warm.sum())
        if warm_count == 0:
            return False
        # An icon is a minority of its patch (strokes plus dark gaps); flat
        # bright scenery fills the whole thing.
        if warm_count > int(0.55 * patch.shape[0] * patch.shape[1]):
            return False
        # ...and it has internal structure rather than being a smooth gradient.
        if cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32).std() < 18.0:
            return False

        # 2. Close button rim in top-right (circular button with warm white
        # rim). All four cardinal points are required, not three of four: the
        # sky false positive scored exactly 3/4.
        r = max(8, int(18 * s))
        close_hits = 0
        for ox, oy in [(0, -r), (0, r), (-r, 0), (r, 0)]:
            hit = False
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if check_pixel(arr, cx + ox + dx, cy + oy + dy, (236, 229, 216), tolerance=15):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                close_hits += 1

        return close_hits == 4
    except Exception:
        return False


def is_wish_open(arr: Any, coords: dict) -> bool:
    try:
        height, width = arr.shape[:2]
        s = coords.get("ui_scale", width / 1920.0)
        cx, cy = coords["map_close_x"], coords["map_close_y"]
        sx, sy = coords["wish_star_x"], coords["wish_star_y"]
        bx, by = coords.get("wish_btn_x", width - int(240 * s)), coords.get("wish_btn_y", height - int(60 * s))

        # 1. Check close button in top right
        r = max(8, int(18 * s))
        close_hits = 0
        for ox, oy in [(0, -r), (0, r), (-r, 0), (r, 0)]:
            hit = False
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if check_pixel(arr, cx + ox + dx, cy + oy + dy, (236, 229, 216), tolerance=15):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                close_hits += 1
        if close_hits < 3:
            return False

        # 2. Check top-left gold wish star
        star_hit = False
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                px = sx + dx
                py = sy + dy
                if 0 <= px < width and 0 <= py < height:
                    b, g, r_pix = arr[py, px][:3]
                    if 180 <= r_pix <= 235 and 160 <= g <= 210 and 120 <= b <= 165 and int(r_pix) - int(b) >= 40:
                        star_hit = True
                        break
            if star_hit:
                break
        if not star_hit:
            return False

        # 3. Check bottom-right Wish x10 button (warm white)
        btn_hit = False
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                px = bx + dx
                py = by + dy
                if 0 <= px < width and 0 <= py < height:
                    if np.all(arr[py, px] > 240):
                        btn_hit = True
                        break
            if btn_hit:
                break

        return btn_hit
    except Exception:
        return False


def is_settings_open(arr: Any, coords: dict) -> bool:
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))
        cx, cy = coords.get("map_close_x", width - int(77 * s)), coords.get("map_close_y", int(48 * s))

        # 1. Close button in top right
        r = max(8, int(18 * s))
        close_hits = 0
        for ox, oy in [(0, -r), (0, r), (-r, 0), (r, 0)]:
            hit = False
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if check_pixel(arr, cx + ox + dx, cy + oy + dy, (236, 229, 216), tolerance=15):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                close_hits += 1
        if close_hits < 3:
            return False

        # 2. Top-left Settings gear icon center at (66*s, 47*s)
        gx = int(round(66 * s))
        gy = int(round(47 * s))

        # Gear outer ring must be gold
        gold_ring = 0
        total_outer = 0
        ro_in = (9.5 * s) ** 2
        ro_out = (13.5 * s) ** 2
        for dy in range(int(-14 * s), int(15 * s)):
            for dx in range(int(-14 * s), int(15 * s)):
                d_sq = dx * dx + dy * dy
                if ro_in <= d_sq <= ro_out:
                    total_outer += 1
                    px = gx + dx
                    py = gy + dy
                    if 0 <= px < width and 0 <= py < height:
                        b, g, r_pix = arr[py, px][:3]
                        if r_pix > 165 and g > 140 and b < 165 and int(r_pix) > int(b) + 30:
                            gold_ring += 1

        # Gear inner ring at radius 6*s must be dark hole (purple background)
        dark_ring = 0
        total_ring = 0
        r_in = (4.5 * s) ** 2
        r_out = (7.5 * s) ** 2
        for dy in range(int(-8 * s), int(9 * s)):
            for dx in range(int(-8 * s), int(9 * s)):
                d_sq = dx * dx + dy * dy
                if r_in <= d_sq <= r_out:
                    total_ring += 1
                    px = gx + dx
                    py = gy + dy
                    if 0 <= px < width and 0 <= py < height:
                        b, g, r_pix = arr[py, px][:3]
                        if r_pix < 120 and g < 120:
                            dark_ring += 1

        is_gold = (total_outer > 0 and gold_ring / total_outer >= 0.35)
        has_hole = (total_ring > 0 and dark_ring / total_ring >= 0.25)
        return is_gold and has_hole
    except Exception:
        return False


def is_mailbox_open(arr: Any, coords: dict) -> bool:
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))
        cx = width - int(77 * s)
        cy = int(48 * s)

        # 1. Close button in top right
        r = max(8, int(18 * s))
        close_hits = 0
        for ox, oy in [(0, -r), (0, r), (-r, 0), (r, 0)]:
            hit = False
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if check_pixel(arr, cx + ox + dx, cy + oy + dy, (236, 229, 216), tolerance=15):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                close_hits += 1
        if close_hits < 3:
            return False

        # 2. Envelope gold body at (66*s, 47*s)
        gx = int(round(66 * s))
        gy = int(round(47 * s))

        gold_outer = 0
        total_outer = 0
        ro_in = (9.5 * s) ** 2
        ro_out = (13.5 * s) ** 2
        for dy in range(int(-14 * s), int(15 * s)):
            for dx in range(int(-14 * s), int(15 * s)):
                d_sq = dx * dx + dy * dy
                if ro_in <= d_sq <= ro_out:
                    total_outer += 1
                    px = gx + dx
                    py = gy + dy
                    if 0 <= px < width and 0 <= py < height:
                        b, g, r_pix = arr[py, px][:3]
                        if r_pix > 165 and g > 140 and b < 165 and int(r_pix) > int(b) + 30:
                            gold_outer += 1

        dark_ring = 0
        total_ring = 0
        r_in = (4.5 * s) ** 2
        r_out = (7.5 * s) ** 2
        for dy in range(int(-8 * s), int(9 * s)):
            for dx in range(int(-8 * s), int(9 * s)):
                d_sq = dx * dx + dy * dy
                if r_in <= d_sq <= r_out:
                    total_ring += 1
                    px = gx + dx
                    py = gy + dy
                    if 0 <= px < width and 0 <= py < height:
                        b, g, r_pix = arr[py, px][:3]
                        if r_pix < 120 and g < 120:
                            dark_ring += 1

        is_solid_gold = (total_outer > 0 and gold_outer / total_outer >= 0.70)
        has_no_hole = (total_ring > 0 and dark_ring / total_ring <= 0.10)

        # 3. Bottom left: Claim All button warm white pill at x=124*s, y=h-58*s
        bx = int(124 * s)
        by = height - int(58 * s)
        btn_hit = False
        if 0 <= bx < width and 0 <= by < height:
            btn_hit = bool(np.all(arr[by, bx] > 215))

        return is_solid_gold and has_no_hole and btn_hit
    except Exception:
        return False


def is_events_open(arr: Any, coords: dict) -> bool:
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))
        cx = width - int(77 * s)
        cy = int(156 * s)

        # 1. Close button at (w - 77*s, 156*s)
        r = max(8, int(18 * s))
        close_hits = 0
        for ox, oy in [(0, -r), (0, r), (-r, 0), (r, 0)]:
            hit = False
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if check_pixel(arr, cx + ox + dx, cy + oy + dy, (236, 229, 216), tolerance=15):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                close_hits += 1
        if close_hits < 3:
            return False

        # 2. Compass icon at (78*s, 153*s)
        gx = int(round(78 * s))
        gy = int(round(153 * s))
        gold_ring = 0
        total_ring = 0
        r_in = (12 * s) ** 2
        r_out = (18 * s) ** 2
        for dy in range(int(-18 * s), int(19 * s)):
            for dx in range(int(-18 * s), int(19 * s)):
                d_sq = dx * dx + dy * dy
                if r_in <= d_sq <= r_out:
                    total_ring += 1
                    px = gx + dx
                    py = gy + dy
                    if 0 <= px < width and 0 <= py < height:
                        b, g, r_pix = arr[py, px][:3]
                        if r_pix > 165 and g > 140 and b < 165 and int(r_pix) > int(b) + 30:
                            gold_ring += 1

        return total_ring > 0 and (gold_ring / total_ring >= 0.25)
    except Exception:
        return False


def _glyph_plate(arr: Any, width: int, height: int, px: int, py: int,
                 half: int, min_warm_frac: float, min_stddev: float) -> bool:
    """Does a top-bar icon actually *look like an icon* here?

    Genshin's top-bar buttons are always a glyph on a plate, never a flat
    wash. Sampling four isolated warm-white pixels therefore cannot tell an
    icon from any bright background behind it - a character splash load
    screen is near-white end to end and scored 4/4 on every probe.

    This asks two questions of the surrounding patch instead:
      * is any of it warm white at all (an icon is drawn in the UI's cream
        ink, so a patch with none of it is not this UI), and
      * does it have internal structure (an icon has dark strokes and gaps;
        a flat splash does not).
    """
    x0, x1 = max(0, px - half), min(width, px + half + 1)
    y0, y1 = max(0, py - half), min(height, py + half + 1)
    pad = arr[y0:y1, x0:x1]
    if pad.size == 0:
        return False
    warm = (pad[:, :, 2] > 224) & (pad[:, :, 1] > 217) & (pad[:, :, 0] < 232)
    if float(warm.sum()) / float(warm.size) < min_warm_frac:
        return False
    return float(cv2.cvtColor(pad, cv2.COLOR_BGR2GRAY).astype(np.float32).std()) >= min_stddev


def is_party_setup_open(arr: Any, coords: dict) -> bool:
    """Is the party setup / co-op screen open?

    Two independent top-bar signals must both hold:

    1. The back chevron at (w - 77, 48) must hit *all four* cardinal
       samples. Three of four is matchable by any bright top-right corner -
       the splash screen's magnifier icon scored exactly 3/4 against a real
       back arrow's 4/4. The two glyphs are otherwise near-identical in
       colour and local contrast, so count is the only thing separating them.
    2. The filter glyph at (35, 48) must additionally look like a glyph.
       On the white splash screen that patch is flat: local stddev 0.5 with
       zero warm pixels, versus stddev 83 and 57% warm on the real filter
       icon. A red party-invite badge bottom-left is accepted as an
       alternative second signal, since that one is already colour-specific.
    """
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))

        # 1. Top-right circular back button at (w - 77*s, 48*s)
        cx = width - int(77 * s)
        cy = int(48 * s)
        r = max(8, int(18 * s))
        close_hits = 0
        for ox, oy in [(0, -r), (0, r), (-r, 0), (r, 0)]:
            hit = False
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if check_pixel(arr, cx + ox + dx, cy + oy + dy, (236, 229, 216), tolerance=15):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                close_hits += 1
        if close_hits != 4:
            return False

        # 2. Check either:
        # a) Top-left filter button at (35*s, 48*s)
        fx = int(35 * s)
        fy = int(48 * s)
        fr = max(6, int(12 * s))
        filter_hits = 0
        for ox, oy in [(0, -fr), (0, fr), (-fr, 0), (fr, 0)]:
            if check_pixel(arr, fx + ox, fy + oy, (236, 229, 216), tolerance=20):
                filter_hits += 1
        if filter_hits >= 3 and _glyph_plate(arr, width, height, fx, fy,
                                            fr + max(2, int(4 * s)), 0.15, 25.0):
            return True

        # b) Bottom-left red badge at (69*s, h - 61*s)
        bx = int(69 * s)
        by = height - int(61 * s)
        box = arr[max(0, by - int(12 * s)):min(height, by + int(12 * s)), max(0, bx - int(12 * s)):min(width, bx + int(12 * s))]
        if box.size:
            red_count = int(np.sum((box[:, :, 2] > 170) & (box[:, :, 1] < 110) & (box[:, :, 0] < 110)))
            if red_count >= int(40 * s * s):
                return True

        return False
    except Exception:
        return False


def is_escape_menu_open(arr: Any, coords: dict) -> bool:
    try:
        height, width = arr.shape[:2]
        s = coords.get("ui_scale", width / 1920.0)
        ex = coords.get("exit_btn_x", int(42 * s))
        ey = coords.get("exit_btn_y", height - int(54 * s))

        # 1. Center of exit arrow must be beige/gold
        b, g, r = int(arr[ey, ex][0]), int(arr[ey, ex][1]), int(arr[ey, ex][2])
        if not (r > 175 and g > 155 and b < 165 and (r - b) > 30):
            return False

        # 2. Door right vertical bar must be beige/gold
        rf_x = int(ex + 17 * s)
        rb, rg, rr = int(arr[ey, rf_x][0]), int(arr[ey, rf_x][1]), int(arr[ey, rf_x][2])
        if not (rr > 165 and rg > 145 and rb < 165 and (rr - rb) > 25):
            return False

        # 3. Gold pixel cluster in bounding box
        bw = max(8, int(20 * s))
        bh = max(8, int(20 * s))
        crop = arr[max(0, ey - bh):min(height, ey + bh + 1), max(0, ex - bw):min(width, ex + bw + 1)]
        mask = (crop[:, :, 2] > 175) & (crop[:, :, 1] > 155) & (crop[:, :, 0] < 165) & (crop[:, :, 2].astype(int) - crop[:, :, 0].astype(int) > 30)
        gold_count = np.sum(mask)
        if gold_count < int(200 * s * s):
            return False

        # 4. Outside left and above must be dark sidebar
        check_points = [
            (max(0, ex - int(24 * s)), ey),
            (ex, max(0, ey - int(24 * s))),
        ]
        dark_hits = 0
        for px, py in check_points:
            pb, pg, pr = int(arr[py, px][0]), int(arr[py, px][1]), int(arr[py, px][2])
            if max(pr, pg, pb) <= 155 and pb >= pr:
                dark_hits += 1

        return dark_hits == 2
    except Exception:
        return False


def is_handbook_open(arr: Any, coords: dict) -> bool:
    """Detect the open Handbook/Archive book from its left-hand tab column.

    The tabs are six *saturated* coloured chips stacked in a fixed column:
    green Experience, purple Commissions, cyan Domains, peach Enemies,
    gold Guide, teal Embattle. Exactly one of them - the selected page - is
    swapped for a cream highlight.

    Both facts are load-bearing. Accepting the cream highlight as evidence on
    its own is what used to fire on open-world scenery: pale stone and paving
    are bright and near-neutral (measured saturation 0.04-0.07), so every tab
    probe "hit" at once and d_menu went True on bright Fontaine ground, which
    silently killed dialogue detection until you walked somewhere darker. A
    real tab column measures 0.37-0.67 saturation, and at most one tab is ever
    cream, so the two-sided test (many saturated chips, at most one cream)
    separates the book from scenery without depending on scenery being dark.
    """
    try:
        height, width = arr.shape[:2]
        s = coords.get("ui_scale", width / 1920.0)
        cx = width // 2
        cy = height // 2

        tx = cx - int(732 * s)
        if tx < 0:
            return False

        tab_y_offsets = [-307, -200, -93, 13, 120, 213]

        def is_cream(r: int, g: int, b: int) -> bool:
            # The selected-tab highlight. Bright and near-neutral.
            return r > 190 and g > 180 and b > 165 and abs(r - g) <= 25

        def is_chip(idx: int, r: int, g: int, b: int) -> bool:
            # The unselected tab fill for this row. Hue-specific, because a
            # generic "saturated" test also fires on coloured scenery.
            if idx == 0:    # Experience (Green)
                return g > r + 15 and g > b + 20 and g > 60
            elif idx == 1:  # Commissions (Purple)
                return b > r + 10 and b > g + 15 and b > 60
            elif idx == 2:  # Domains (Blue/Cyan)
                return b > r + 30 and g > r + 20 and b > 60
            elif idx == 3:  # Enemies (Peach/Salmon)
                return r > g + 10 and r > b + 15 and r > 80
            elif idx == 4:  # Guide (Gold/Brown)
                return r > g + 15 and r > b + 35 and r > 80
            elif idx == 5:  # Embattle (Teal)
                return g > r + 25 and b > r + 25 and g > 60
            return False

        chip_hits = 0
        cream_hits = 0
        for idx, y_off in enumerate(tab_y_offsets):
            ty = cy + int(y_off * s)
            if not (0 <= ty < height):
                continue
            for dx in range(-2, 3):
                px = tx + dx
                if not (0 <= px < width):
                    continue
                b_pix, g_pix, r_pix = int(arr[ty, px][0]), int(arr[ty, px][1]), int(arr[ty, px][2])
                if is_chip(idx, r_pix, g_pix, b_pix):
                    chip_hits += 1
                    break
            else:
                # Not a coloured chip - only the one selected tab may be cream.
                for dx in range(-2, 3):
                    px = tx + dx
                    if 0 <= px < width:
                        b_pix, g_pix, r_pix = int(arr[ty, px][0]), int(arr[ty, px][1]), int(arr[ty, px][2])
                        if is_cream(r_pix, g_pix, b_pix):
                            cream_hits += 1
                            break

        # Five saturated chips (the selected one is cream, so five of six
        # saturated plus at most one cream) is the whole test. Measured across
        # every screenshot on hand: all 6 real handbooks score chip=6/cream=1,
        # and nothing else comes close - open-world scenes score chip<=2.
        #
        # An earlier version also required a "bright page" probe at
        # (cx-200, cy). It was pure downside: bright stone passed it, and two
        # genuine handbook pages with map/tinted content failed it, so it
        # produced false positives and false negatives while discriminating
        # nothing. The chip column alone is specific.
        return chip_hits >= 5 and cream_hits <= 1
    except Exception:
        return False


# ── Katheryne menu templates ──────────────────────────────────
_KATHERYNE_ICON_B64 = (
    "eJx1VvlTE1cc/1sqR0KS3X2bc3NngQRCTkJICCagUavilSl4cIhWBOp41KutUBRtVawHCh3LdCqedDzqOTqVotYqHhxeaL2F1+9bEjyqnx82"
    "8/I+u+97fx6rtbqc8ze3djRVTJ9UHC4sCAb8eV5fqDjf7RXgcXvehZ53Rqo27tm2unzaxEgo6M/zeT2urEDV1hKTI0eAfRSji5xsZ7R+Z3vz"
    "kumRUEEgz+t2OezZNt2kXx6uMmS+jwwB3nBt657GBUXhwiCwnUDOzEhXV9zFP+ebeJ638JY4zKPwLdrVumZuNBzKz3U5CJk3m/QZGzC+UKbU"
    "G95CH0fN1rZvSicUBv1ep91GyEadVlX7D8avfuQ0Gq2WExD/Aeza2/AF0H3uHFsmbzYa9Doll38CA65W62mNRq1WvYd9TQsi4aDPlWMlhmg5"
    "rQZlftePh4fhhQJGoVIqFQq5XK5IoLU2Gi7Ic9mtZgOw4XOsovgcxkMDL/Djhuw0wpWzFCVP4Ks54wv8HrvV4nXKJWk0qxgXPPwUP+pYegmP"
    "9MZSaEaSlCQNRU1yxAqYFg35vQ4rrw9VVk/22XQyczPYfnaqouENxp1FtNKaP7G8ZRnHMkhANBLwOrPSTVxOU//xnZUOdf1tjPu+losjnfDe"
    "rx7j3PbTfRfKRXE6KiokxlsMHKq8dWtgm7fkJNDWaRmGqn6E8eBmdVV3/8PtHmmCHw56ITRGrUZUfPRuR9ncP15jfCD/MwaJLMv7ML61YvLa"
    "awP1CjpORyG/KzvDrOfUEkf9xpI5XRgPn/hcIUGISdV/fx/j/jWTlu6fOfZ5FMh1ZGXA55XImJc7+yDxdYZKSnYo1toOywdr0mNegc6y8PB7"
    "7ZnEHKVcppsHgcfdFbDDUBSDGCawF9J2s9HHUqPBBL7PncWbdJyKSbatPgO7l6s5MaJ12QY5gyhZqHUE4zutYbGIEdgIeZw2i0ErZ7jg5nsY"
    "vz5TziUjNsX/3ZJsMYvSpPnt8C8+GOPpuAtuh9WsV2vsMzshME87SxDQaLZu8GoZcULGZDXehiOurfBrGOEFd06GUSMN7Oh5hvHI7qAaMSyT"
    "NBscacmREhdQ+qoBOLfv8GxWTILq5DlzXl0X5AZfb8yjpAxLs/YOWN2oSiL7lIQvP/oC45fdG4t1qamUOxRbuRtKAPd21FjEJO5p5g0PSf0f"
    "LmBhiZhk2ZTmS3D680Pr5ri53EXHHsPmyMsWr4wmFlLa2HVCx886HHEfpdyX18A7/Kp7RXbu4i6B/2K7R+AzabMuvxT4+N5yi4gd5S/qIfyX"
    "3SvtYM+qtruwuCnYQ42LHIbFwO9P4HklliZFTIp0wqZL/wK7i9hD/C1YFvfXL6Fd7ZA0vH9KYy/8nJiFpGK+/CD4+6pn82R9CvgL8VRTwe09"
    "z0k8A/6dQ1BxZ2Mi+ZZX8MK5sNZC4vmm71ipUkS/k69ZB6Gfhk4feETcmienZUXEriedpU034vlCzDv1gLjgFqje4WdgzWCNQcTSTOwiCVJ3"
    "PzwPva2HsXpLsq0l9fb6Tc9qUxpUV4qq7NQLIU532ovEqYl6E+rZoNUo5JQudgZ2h3doGWIoK2LL/8Jgy80NTlqGEvUM/WJLF/pFbzeWdJF+"
    "makieUUy1nUElkPf6mwclWgvVED60QT9KHXUrAxMOQ55Px7vR936W5CJNZGytSHpGH889LtwQOqErr83hWedekP6fRyYwy+HNN5ZHag+/2ep"
    "ZKx/YZ64c8g8ocvvDDzYklVyiswTHUL0IsjE4A+6GSfv9y6Wj82HaFHQ64B5pcluGHp070CpsRaM6FvFSsK/kXnl5ZdfvP1kk1WWOGBqtNBP"
    "JpB27pHLLQujHpbfArzTE8TrhXmION/MxS17YmkJfn183prLGmr8dDKtTC2EefuwreI8mbcQdnGywjN/MhUft2xr7cTxZJ5nOTNVKo1GpRqd"
    "508GyTy3S8g8RxSFFIl5PqYXtnSTUa/VcKN6MRLXCyXohfxdvRjTI7sV9AhkDfQoIOjRlY/qUe3Wff/Xuzqid1sFvUsoXeI3sPgTenppgYLo"
    "qfEDQfUU1e0heh1J6LU1Y1Sv2+J6zfPv6TXcB+p+amuuKYkUjt0HsnSTOh6sNHzsOpBhSHdGKps+vG8EFybuG3b7+/cNpLW5XfOaP3mf8XyA"
    "/wCBGN6r"
)

_KATHERYNE_ICON_TPL = np.frombuffer(zlib.decompress(base64.b64decode(_KATHERYNE_ICON_B64)), dtype=np.uint8).reshape((48, 48))


def is_katheryne_menu_open(arr: Any, coords: dict) -> bool:
    """Detect whether Katheryne's service menu is open by matching the Daily Commission star icon."""
    try:
        height, width = arr.shape[:2]
        aspect = width / height
        s = coords.get("ui_scale", (width / 1920.0) if aspect <= 16.0 / 9.0 else (height / 1080.0))

        # Check Daily Commission Star Icon in choice column
        icon_size = max(16, int(round(48 * s)))
        tpl_icon = cv2.resize(_KATHERYNE_ICON_TPL, (icon_size, icon_size), interpolation=cv2.INTER_AREA)

        x1 = max(0, int(width * 0.58))
        x2 = min(width - 1, int(width * 0.75))
        y1 = max(0, int(300 * s))
        y2 = min(height - 1, int(900 * s))

        crop_choices = arr[y1:y2, x1:x2]
        if not np.any(crop_choices > 215):
            return False

        crop_gray = cv2.cvtColor(crop_choices, cv2.COLOR_BGR2GRAY)
        if crop_gray.shape[0] < icon_size or crop_gray.shape[1] < icon_size:
            return False

        res_icon = cv2.matchTemplate(crop_gray, tpl_icon, cv2.TM_CCOEFF_NORMED)
        _, max_icon, _, _ = cv2.minMaxLoc(res_icon)
        return max_icon >= 0.80
    except Exception:
        return False



# ── TUI state ──────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.status = "pause"       # "pause", "run", "exit"
        self.events: deque = deque(maxlen=3)
        self.f_presses = 0
        self.last_state = ""
        self.resolution = ""
        self.hwnd = 0
        self.notify_enabled = True
        self.on_top = False
        # Why pressing is currently suppressed; "" when it is not. The verdict
        # line used to claim "pressing F" unconditionally, so a deliberate
        # pause and a stand-down for real player input were both invisible:
        # the counter simply stopped moving and nothing on screen said why.
        # This carries the reason so the TUI can state it out loud.
        self.hold_reason = ""

    def log(self, msg):
        now = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"{GRY}{now}{R} {msg}")

app = AppState()


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


def poll_hotkeys() -> None:
    """Poll hotkeys globally using GetAsyncKeyState — immune to anti-cheat hook blocks."""
    last_states = {
        win32con.VK_F7: False,
        win32con.VK_F8: False,
        win32con.VK_F9: False,
        win32con.VK_F12: False,
    }
    while app.status != "exit":
        for vk in list(last_states.keys()):
            is_down = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
            if is_down and not last_states[vk]:
                if vk == win32con.VK_F7:
                    app.on_top = not app.on_top
                    set_console_on_top(app.on_top)
                    app.log(f"on top {'on' if app.on_top else 'off'}")
                elif vk == win32con.VK_F8:
                    if app.status == "run":
                        app.status = "pause"
                        app.log("paused")
                    else:
                        app.status = "run"
                        app.log("started")
                elif vk == win32con.VK_F9:
                    app.notify_enabled = not app.notify_enabled
                    app.log(f"notif {'on' if app.notify_enabled else 'off'}")
                elif vk == win32con.VK_F12:
                    app.status = "exit"
            last_states[vk] = is_down
        sleep(0.02)


# ── TUI renderer ──────────────────────────────────────────────

def render_tui(d_box, d_marker, d_auto, d_interact, d_paim, d_map, d_wish, d_set, d_mail, d_evnt, d_party, d_esc, d_book, d_kath, dialogue_active, d_choice=False):
    now = datetime.now().strftime("%H:%M:%S")
    K = "\033[K"  # erase to end of line — prevents ghost text

    # status badge
    if app.status == "pause":
        st = f"{YEL}paused{R}"
    else:
        st = f"{GRN}running{R}"

    # toggle badges
    ot = f"{GRN}on{R}" if app.on_top else f"{GRY}off{R}"
    nf = f"{GRN}on{R}" if app.notify_enabled else f"{GRY}off{R}"

    # detection state
    if dialogue_active:
        # No option selection: for story dialogue the highlighted reply does not
        # matter, so F is pressed the same way whether or not a choice is up.
        triggers = []
        if d_auto: triggers.append("autoplay")
        if d_choice: triggers.append("reply option")
        tail = f" {GRY}({', '.join(triggers)}){R}" if triggers else ""
        if app.hold_reason:
            # Do not claim "pressing F" while we are knowingly not pressing.
            verdict = (f" {BG_YEL}{B} SKIPPING {R}  {YEL}{app.hold_reason}{R}{tail}")
        else:
            verdict = f" {BG_GRN}{B} SKIPPING {R}  {GRN}pressing F{R}{tail}"
    elif d_map:
        verdict = f" {BG_CYN}{B} MAP {R}  {CYN}map open{R}"
    elif d_wish:
        verdict = f" {BG_CYN}{B} WISH {R}  {CYN}wish menu open{R}"
    elif d_set:
        verdict = f" {BG_CYN}{B} SETTINGS {R}  {CYN}settings menu open{R}"
    elif d_mail:
        verdict = f" {BG_CYN}{B} MAILBOX {R}  {CYN}mailbox open{R}"
    elif d_evnt:
        verdict = f" {BG_CYN}{B} EVENTS {R}  {CYN}events overview open{R}"
    elif d_party:
        verdict = f" {BG_CYN}{B} PARTY {R}  {CYN}party setup open{R}"
    elif d_esc:
        verdict = f" {BG_CYN}{B} ESCAPE {R}  {CYN}escape menu open{R}"
    elif d_book:
        verdict = f" {BG_CYN}{B} HANDBOOK {R}  {CYN}handbook open{R}"
    elif d_kath:
        verdict = f" {BG_CYN}{B} KATHERYNE {R}  {CYN}katheryne menu open{R}"
    elif d_interact:
        # checked before d_paim on purpose - is_paimon_visible is a decorative
        # 9-pixel probe that false-positives, and "ignoring an [F] prompt" is
        # the more useful thing to show when both are true
        verdict = f" {BG_YEL}{B} INTERACT {R}  {YEL}[F] prompt — not dialogue, ignoring{R}"
    elif d_paim:
        verdict = f" {BG_CYN}{B} OVERWORLD {R}  {CYN}dialogue finished{R}"
    else:
        verdict = f" {BG_GRY} IDLE {R}  {GRY}waiting for dialogue{R}"

    L = []
    L.append(f" {B}{WHT}genshin dialogue autoskip{R}      {GRY}{now}{R}{K}")
    L.append(f" {app.resolution}  {st}  {GRY}hwnd {WHT}{app.hwnd}{R}{K}")
    L.append(f" {GRY}f7{R} on top {ot}  {GRY}f8{R} toggle  {GRY}f9{R} notif {nf}  {GRY}f12{R} quit{K}")
    L.append(f"{GRY}{'-' * TUI_COLS}{R}{K}")
    L.append(f"{K}")
    L.append(f"{verdict}{K}")
    L.append(f" f pressed {WHT}{app.f_presses}{R}{K}")
    L.append(f"{K}")
    L.append(f" dialogue  {'%s%sON%s' % (GRN, B, R) if d_box else '%s--%s' % (GRY, R)}{K}")
    L.append(f" diamond   {'%s%sON%s' % (GRN, B, R) if d_marker else '%s--%s' % (GRY, R)}{K}")
    L.append(f" choice    {'%s%sON%s' % (GRN, B, R) if d_choice else '%s--%s' % (GRY, R)}{K}")
    L.append(f" autoplay  {'%s%sON%s' % (GRN, B, R) if d_auto else '%s--%s' % (GRY, R)}{K}")
    L.append(f" interact  {'%s%s--%s' % (YEL, GRY, R) if d_interact else '%s--%s' % (GRY, R)}{K}")
    L.append(f"{K}")
    L.append(f"{GRY}{'-' * TUI_COLS}{R}{K}")

    if app.events:
        L.append(f"{K}")
        for e in app.events:
            L.append(f" {e}{K}")

    L.append("\033[J")
    return "\n".join(L)


def render_tui_idle() -> str:
    """TUI frame with nothing detected - used while paused or before a frame lands.

    Keeps the call sites independent of render_tui's arity, so adding a detector
    can never break the paused path again.
    """
    return render_tui(False, False, False, False, False, False, False,
                      False, False, False, False, False, False, False, False, False)


# ── Main loop ──────────────────────────────────────────────────

def main() -> None:
    setup_console()
    sys.stdout.write("\033[?25l\033[2J\033[H")
    sys.stdout.flush()

    app.status = "pause"
    last_f_press = 0.0
    # When a press was actually injected, as opposed to last_f_press which also
    # advances when the bot stands down for the player. The chime is gated on
    # this so holding WASD does not silence it forever.
    last_f_sent = 0.0
    next_f_interval = random_f_key_interval()
    last_break_check = perf_counter()
    break_check_interval = 30.0

    grabber: Optional[BackgroundFrameGrabber] = None
    last_coords_w = 0
    last_coords_h = 0
    coords: Dict[str, int] = {}
    was_active = False
    dialogue_start_time = 0.0
    dialogue_was_active = False
    notification_played = True

    # Detection state for TUI
    d_box = d_marker = d_auto = d_interact = d_paim = False
    dialogue_active = False
    last_tui_render = 0.0

    session_start_time = 0.0
    session_frame = None
    session_triggers: Dict[str, bool] = {}
    session_f_pressed = False

    try:
        while True:
            current_time = perf_counter()

            # Handle pause
            if app.status == "pause":
                hwnd = grabber.hwnd if grabber else win32gui.FindWindow("UnityWndClass", "Genshin Impact")
                while app.status == "pause":
                    # Render TUI even while paused
                    now_t = perf_counter()
                    if now_t - last_tui_render >= 0.5:
                        last_tui_render = now_t
                        sys.stdout.write("\033[H" + render_tui_idle())
                        sys.stdout.flush()

                    sleep(0.1)
                    current_time = perf_counter()
                    last_f_press = current_time

            if app.status == "exit":
                break

            # Window closed?
            if grabber is not None and not win32gui.IsWindow(grabber.hwnd):
                app.log("window closed, waiting...")
                grabber.stop()
                grabber = None
                last_coords_w = 0
                last_coords_h = 0
                sleep(1.0)
                continue

            # Start grabber
            if grabber is None:
                hwnd = win32gui.FindWindow("UnityWndClass", "Genshin Impact")
                if not hwnd:
                    app.log("game not found, waiting...")
                    now_t = perf_counter()
                    if now_t - last_tui_render >= 0.5:
                        last_tui_render = now_t
                        sys.stdout.write("\033[H" + render_tui_idle())
                        sys.stdout.flush()
                    sleep(2.0)
                    continue

                app.hwnd = hwnd
                app.log(f"attached hwnd {hwnd}")
                grabber = BackgroundFrameGrabber(hwnd)
                try:
                    grabber.start()
                    for _ in range(20):
                        if grabber.latest_frame is not None:
                            break
                        sleep(0.1)
                    if grabber.latest_frame is None:
                        app.log("capture failed")
                        grabber.stop()
                        grabber = None
                        sleep(2.0)
                        continue
                except Exception:
                    grabber = None
                    sleep(2.0)
                    continue

            # Focus management
            is_active = game_has_focus(grabber.hwnd)
            # Background presses leave the game's WndProc believing it is
            # inactive. Windows usually repairs that when the user tabs back
            # in, but not always - and when it misses, the game is foreground
            # with a dead input pump: the player tabs in and nothing responds.
            # Re-send the activate pair on the transition, while we know the
            # window really is focused. See _reactivate().
            if is_active and not was_active:
                _reactivate(grabber.hwnd)
            was_active = is_active

            if win32gui.IsIconic(grabber.hwnd):
                sleep(0.5)
                continue

            frame_arr = grabber.latest_frame
            if frame_arr is None:
                sleep(0.05)
                continue

            # Resolution change
            current_w, current_h = grabber.width, grabber.height
            if current_w != last_coords_w or current_h != last_coords_h:
                last_coords_w, last_coords_h = current_w, current_h
                coords = get_coords(current_w, current_h)
                app.resolution = f"{current_w}x{current_h}"
                app.log(f"calibrated {current_w}x{current_h}")

            # Detection
            # d_ow first: the overworld HUD (HP bar) is hidden by every full
            # screen menu, so a visible HP bar rules all of them out. Gating the
            # menu probes on it is the cheap half of keeping open-world scenery
            # out of the menu detectors; each probe still has to be correct on
            # its own, which is why the handbook probe was fixed too.
            d_ow = is_overworld_active(frame_arr, coords)
            d_map = is_map_open(frame_arr, coords)
            d_wish = is_wish_open(frame_arr, coords) if not d_map else False
            d_set = is_settings_open(frame_arr, coords) if (not d_map and not d_wish) else False
            d_mail = is_mailbox_open(frame_arr, coords) if (not d_map and not d_wish and not d_set) else False
            d_evnt = is_events_open(frame_arr, coords) if (not d_map and not d_wish and not d_set and not d_mail) else False
            d_party = is_party_setup_open(frame_arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt) else False
            d_esc = is_escape_menu_open(frame_arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt and not d_party) else False
            d_book = is_handbook_open(frame_arr, coords) if (not d_ow and not d_map and not d_wish and not d_set and not d_mail and not d_evnt and not d_party and not d_esc) else False
            d_kath = is_katheryne_menu_open(frame_arr, coords) if (not d_map and not d_wish and not d_set and not d_mail and not d_evnt and not d_party and not d_esc and not d_book) else False
            d_menu = d_map or d_wish or d_set or d_mail or d_evnt or d_party or d_esc or d_book or d_kath
            # The dialogue panel itself is the gate. An [F] keycap on its own is
            # an interaction prompt (forge, chest, NPC) and must never be enough.
            #
            # Note is_paimon_visible() is deliberately NOT a gate here: it is a
            # brittle 9-pixel colour probe in the extreme top-left corner and
            # false-positives on bright scene content. Letting it suppress the
            # dialogue gate silently disables skipping.
            d_box = (is_dialogue_box_visible(frame_arr, coords) if (not d_menu and not d_ow) else False)
            d_marker = (is_advance_marker_visible(frame_arr, coords) if not d_menu else False)
            d_choice = (is_dialogue_choice_visible(frame_arr, coords) if not d_menu else False)
            # Three independent routes to "there is dialogue here": the text band,
            # the advance diamond, or a dialogue reply choice [F] prompt.
            # Active open-world HUD (HP bar visible) suppresses false dialogue triggers.
            dialogue_active = (d_box or d_marker or d_choice) and not d_ow
            if not dialogue_active:
                # Nothing to press, so no reason to be holding.
                app.hold_reason = ""
            d_auto = is_autoplay_enabled(frame_arr, coords) if dialogue_active else False
            d_interact = is_interact_prompt_visible(frame_arr, coords, dialogue_active) if (not d_menu and not dialogue_active) else False
            d_paim = is_paimon_visible(frame_arr, coords) if not d_menu else False

            # Render TUI
            now_t = perf_counter()
            if now_t - last_tui_render >= 0.15:
                last_tui_render = now_t
                sys.stdout.write("\033[H" + render_tui(d_box, d_marker, d_auto, d_interact, d_paim, d_map, d_wish, d_set, d_mail, d_evnt, d_party, d_esc, d_book, d_kath, dialogue_active, d_choice))
                sys.stdout.flush()

            # Dialogue state tracking
            if dialogue_active:
                if dialogue_start_time == 0.0:
                    dialogue_start_time = current_time
                    session_start_time = current_time
                    session_frame = frame_arr.copy()
                    session_triggers = {
                        "box": d_box,
                        "marker": d_marker,
                        "choice": d_choice,
                        "auto": d_auto,
                    }
                    session_f_pressed = False
                if (current_time - dialogue_start_time) >= 0.4:
                    dialogue_was_active = True
                    notification_played = False
            else:
                if session_start_time > 0.0:
                    session_duration = current_time - session_start_time
                    if session_duration < 0.100 and (session_f_pressed or dialogue_start_time > 0.0):
                        try:
                            os.makedirs(LOGS_DIR, exist_ok=True)
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
                            dur_ms = int(round(session_duration * 1000))
                            fname = f"transient_{ts}_{dur_ms}ms.png"
                            shot_path = os.path.join(LOGS_DIR, fname)
                            if session_frame is not None:
                                cv2.imwrite(shot_path, session_frame)
                            trigger_str = ", ".join(k for k, v in session_triggers.items() if v) or "none"
                            log_msg = f"transient press (<100ms): {dur_ms}ms, pressed={session_f_pressed}, triggers={trigger_str} -> saved {fname}"
                            app.log(log_msg)
                            with open(os.path.join(LOGS_DIR, "autoskip.log"), "a", encoding="utf-8") as lf:
                                lf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {log_msg}\n")
                        except Exception as log_exc:
                            app.log(f"log error: {log_exc}")
                    session_start_time = 0.0
                    session_frame = None

                dialogue_start_time = 0.0

                if dialogue_was_active and not notification_played:
                    # Chime when dialogue has finished AND no menu is open.
                    # Previously gated on is_paimon_visible(), which is a
                    # brittle corner-colour probe and does not reliably fire.
                    #
                    # The chime also waits out NOTIFY_QUIET_SECONDS after the
                    # last injected press. A dialogue burst is dozens of F
                    # presses, and the notification means "done, your turn" -
                    # firing it mid-burst, or once per flicker of the dialogue
                    # panel, is just noise. Waiting means the ding lands when
                    # the bot has actually stopped, and the pending flag is
                    # deliberately left armed so it can still fire on a later
                    # tick rather than being dropped.
                    if not d_menu:
                        if (current_time - last_f_sent) < NOTIFY_QUIET_SECONDS:
                            pass  # still pressing - stay pending, re-check next tick
                        else:
                            if app.notify_enabled:
                                play_notification_sound()
                                app.log("dialogue finished, chime played")
                            notification_played = True
                            dialogue_was_active = False

            if not dialogue_active:
                sleep(0.1)
                continue

            # Break logic. These exist to look human, so they are deliberate - but a
            # blind sleep(dur) here froze the whole loop while the TUI kept
            # claiming "pressing F" with a stopped counter, which read as a
            # hang. Sleep in slices and redraw, so the countdown is visible.
            if current_time - last_break_check > break_check_interval:
                last_break_check = current_time
                if should_take_break():
                    dur = take_random_break()
                    app.log(f"break {dur:.1f}s")
                    try:
                        os.makedirs(LOGS_DIR, exist_ok=True)
                        with open(os.path.join(LOGS_DIR, "autoskip.log"), "a", encoding="utf-8") as lf:
                            lf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                                     f"humanising break {dur:.1f}s (not pressing)\n")
                    except Exception:
                        pass
                    break_until = perf_counter() + dur
                    while perf_counter() < break_until:
                        remaining = break_until - perf_counter()
                        app.hold_reason = f"paused {remaining:.1f}s (humanising break)"
                        sys.stdout.write("\033[H" + render_tui(
                            d_box, d_marker, d_auto, d_interact, d_paim, d_map,
                            d_wish, d_set, d_mail, d_evnt, d_party, d_esc,
                            d_book, d_kath, dialogue_active, d_choice))
                        sleep(0.25)
                    app.hold_reason = ""
                    last_f_press = perf_counter()
                    next_f_interval = random_f_key_interval()
                    continue

            # Stand down while the player is driving *in the game*. Input aimed
            # at other windows is not the player driving, so pass focus in -
            # see note_user_input.
            note_user_input(current_time, is_active)

            # Press F
            if current_time - last_f_press >= next_f_interval:
                if user_is_driving(current_time):
                    # Standing down for real player input. This was also
                    # invisible - the verdict still said "pressing F" while the
                    # counter stood still - so say so explicitly. A held mouse
                    # button is the usual cause: the game can swallow the
                    # button-up, leaving it reading as held indefinitely.
                    app.hold_reason = "your input held - not pressing"
                    last_f_press = current_time
                    next_f_interval = 0.2
                else:
                    app.hold_reason = ""
                    press_f_background(grabber.hwnd, is_active)
                    app.f_presses += 1
                    session_f_pressed = True
                    last_f_press = current_time
                    last_f_sent = current_time
                    next_f_interval = random_f_key_interval()

            sleep(0.05)

    finally:
        if grabber is not None:
            grabber.stop()
        sys.stdout.write("\033[?25h\033[0m\033[2J\033[H")
        sys.stdout.flush()


if __name__ == "__main__":
    t_hotkeys = Thread(target=poll_hotkeys, daemon=True)
    t_hotkeys.start()
    try:
        main()
    except KeyboardInterrupt:
        app.status = "exit"
        sys.stdout.write("\033[?25h\033[0m\n")
