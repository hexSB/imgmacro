"""imgmacro: a small, auditable image-triggered macro for Windows.

Watches the screen for template images, colours or text and clicks, moves the
mouse, presses keys or plays a recorded macro when one appears. Third-party code
is limited to numpy, opencv-python and mss (screen capture); text detection
optionally adds Windows' own OCR engine (see imgocr.py). Input goes straight to
the Win32 SendInput API through ctypes. Nothing in this file touches the
network; the only files it reads or writes are your configs and template images.

    python imgmacro.py snip templates/button.png   # capture a template
    python imgmacro.py test config.json            # show match scores once
    python imgmacro.py run config.json             # watch and act (F8 stops)
    python imgmacro_gui.py                         # the same, in a window
"""

import argparse
import ctypes
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import cv2
import mss
import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Work in physical pixels so screenshots and clicks agree under display scaling.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    user32.SetProcessDPIAware()


# --- Win32 input -------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.SetWindowDisplayAffinity.argtypes = (wintypes.HWND, wintypes.DWORD)
user32.GetForegroundWindow.restype = wintypes.HWND

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSE_BUTTONS = {  # button -> (down flag, up flag)
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

VK_NAMES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "printscreen": 0x2C, "numlock": 0x90, "scrolllock": 0x91,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    # Punctuation, named by the key on a US layout (other layouts print other symbols).
    "grave": 0xC0, "minus": 0xBD, "equals": 0xBB, "lbracket": 0xDB, "rbracket": 0xDD,
    "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF,
    # The numeric keypad.
    **{f"num{n}": 0x60 + n for n in range(10)},
    "nummultiply": 0x6A, "numplus": 0x6B, "numminus": 0x6D, "numperiod": 0x6E, "numdivide": 0x6F,
}
# The symbols themselves work too, e.g. "`" or "ctrl+/". ("+" can't: it joins combos.)
KEY_ALIASES = {"`": "grave", "-": "minus", "=": "equals", "[": "lbracket", "]": "rbracket",
               "\\": "backslash", ";": "semicolon", "'": "quote", ",": "comma", ".": "period",
               "/": "slash", "tilde": "grave", "backtick": "grave", "escape": "esc",
               "return": "enter", "control": "ctrl"}
# Virtual-key code -> key name, for recording keys. Modifiers are recorded as part of
# a combo ("ctrl+s"), or on their own if pressed alone.
VK_TO_NAME = {vk: name for name, vk in VK_NAMES.items()}
VK_TO_NAME.update({vk: chr(vk).lower() for vk in range(0x30, 0x3A)})
VK_TO_NAME.update({vk: chr(vk).lower() for vk in range(0x41, 0x5B)})
MODIFIER_VKS = {0x10: "shift", 0x11: "ctrl", 0x12: "alt"}
# Keys whose scan codes need the extended flag, or games read them as numpad keys.
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x2C, 0x90, 0x6F}


def parse_key(name):
    """'a', '5', 'space', 'F8', 'grave' or '`' -> virtual-key code."""
    key = name.strip().lower()
    key = KEY_ALIASES.get(key, key)
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    if key in VK_NAMES:
        return VK_NAMES[key]
    raise ValueError(f"unknown key {name!r} (known: a-z, 0-9, f1-f24, num0-num9, "
                     f"{', '.join(k for k in VK_NAMES if not k[1:].isdigit() and not k.startswith('num'))}, "
                     "numplus, numminus, …)")


def parse_combo(combo):
    """'ctrl+shift+s' -> [vk, vk, vk]."""
    return [parse_key(part) for part in combo.split("+")]


def _send(*inputs):
    array = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)) != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


def _mouse(flags, dx=0, dy=0, data=0):
    # mouseData is unsigned; a negative wheel delta goes in as its two's complement.
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                                                 dwFlags=flags))


def virtual_screen():
    """(left, top, width, height) of the whole desktop, across every monitor."""
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


def move_to(x, y):
    # Absolute coordinates are normalised to 0..65535 across the whole virtual desktop.
    vx, vy, vw, vh = virtual_screen()
    nx = round((x - vx) * 65535 / max(vw - 1, 1))
    ny = round((y - vy) * 65535 / max(vh - 1, 1))
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))


# Humanize: vary where each click lands and how long things take by a random amount,
# so a rule doesn't click the exact same pixel at the exact same rhythm every time.
# Set from the config by set_humanize() when a run starts; recorded macros and drags
# are always replayed exactly.
HUMANIZE = [0, 0.0]  # [pixels, seconds]


def set_humanize(px=0, ms=0):
    HUMANIZE[:] = [max(int(px), 0), max(float(ms), 0.0) / 1000]


def _human_point(x, y):
    px = HUMANIZE[0]
    return (x + random.randint(-px, px), y + random.randint(-px, px)) if px else (x, y)


def _human_delay():
    """A random extra delay of up to the humanize time, or 0."""
    return random.uniform(0, HUMANIZE[1]) if HUMANIZE[1] else 0.0


def button_event(button, up):
    """Press or release a mouse button where the mouse is (for macro playback)."""
    _send(_mouse(MOUSE_BUTTONS[button][1 if up else 0]))


def wheel(delta, horizontal=False):
    """Turn the wheel by `delta` (120 = one notch; positive is up, or right)."""
    _send(_mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, data=int(delta)))


def scroll(notches, horizontal=False):
    wheel(round(notches * 120), horizontal)


def click(x, y, button="left", hold=0.03):
    down, up = MOUSE_BUTTONS[button]
    move_to(*_human_point(x, y))
    time.sleep(0.01)
    _send(_mouse(down))
    time.sleep(hold + _human_delay())  # many games ignore a down/up pair sent in the same instant
    _send(_mouse(up))


def _wait_until(deadline, stop):
    """Sleep until perf_counter() reaches `deadline`. -> False if `stop()` said stop first."""
    while time.perf_counter() < deadline:
        if stop and stop():
            return False
        time.sleep(min(0.005, max(deadline - time.perf_counter(), 0)))
    return True


def hold_click(x, y, button="left", hold=0.5, path=(), cancel=None, until=None):
    """Press at (x, y), follow `path`, and release after `hold` seconds.

    `path` is [(seconds after the press, dx, dy), ...] relative to (x, y), as
    recorded from a drag; the button stays down until the path ends, even if that's
    later than `hold`. The button is released early, at once, when `until()`
    returns True (e.g. a release image appeared) or `cancel()` does (e.g. the stop
    key). It is always released, even on an error.

    -> "until", "cancel" or "time": why the button was released.
    """
    down, up = MOUSE_BUTTONS[button]
    stop = _Stopper(cancel, until)
    x, y = _human_point(x, y)  # a drag moves as a whole, so its shape is kept
    move_to(x, y)
    time.sleep(0.01)
    _send(_mouse(down))
    start = time.perf_counter()
    try:
        for t, dx, dy in path:
            if not _wait_until(start + t, stop):
                return stop.reason
            move_to(x + dx, y + dy)
        _wait_until(start + max(hold, 0.03), stop)
        return stop.reason
    finally:
        _send(_mouse(up))


class _Stopper:
    """`stop()` for _wait_until: True once `cancel()` or `until()` is; `.reason` says which."""

    def __init__(self, cancel, until):
        self.cancel, self.until, self.reason = cancel, until, "time"

    def __call__(self):
        if self.cancel and self.cancel():
            self.reason = "cancel"
        elif self.until and self.until():
            self.reason = "until"
        return self.reason != "time"


def _key(vk, up):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    # Scan codes rather than virtual keys: DirectInput/raw-input games only see these.
    scan = user32.MapVirtualKeyW(vk, 0)
    if 0x60 <= vk <= 0x6E:
        # Numpad digits share scan codes with the arrows/Home/End/etc. and turn into
        # those when NumLock is off, so send the virtual key too, which always means
        # the digit (the scan code still goes along for games that read it).
        flags &= ~KEYEVENTF_SCANCODE
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags))
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wScan=scan, dwFlags=flags))


def key_event(vk, up):
    """Press or release one key (for macro playback)."""
    _send(_key(vk, up))


def press(vks, hold=0.05):
    for vk in vks:
        _send(_key(vk, up=False))
    time.sleep(hold + _human_delay())
    for vk in reversed(vks):
        _send(_key(vk, up=True))


def type_text(text, gap=0.03, cancel=None):
    """Type `text` character by character, whatever the keyboard layout, as Unicode
    input (some games only read keys: use key steps for those). -> a note if
    `cancel()` cut it short, else None."""
    for i, ch in enumerate(text):
        if cancel and cancel():
            return f"stopped after {i} of {len(text)} characters"
        if ch in "\n\t":
            press([VK_NAMES["enter" if ch == "\n" else "tab"]])
        else:
            data = ch.encode("utf-16-le")  # characters outside the BMP are two code units
            for unit in (int.from_bytes(data[j:j + 2], "little") for j in range(0, len(data), 2)):
                _send(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wScan=unit, dwFlags=KEYEVENTF_UNICODE)),
                      INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(
                          wScan=unit, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)))
        time.sleep(gap + _human_delay())
    return None


def hold_keys(vks, hold=0.5, cancel=None, until=None):
    """Hold keys down for `hold` seconds, or until `until()` / `cancel()` returns True.
    They are always released, even on an error. -> "until", "cancel" or "time"."""
    stop = _Stopper(cancel, until)
    pressed = []
    try:
        for vk in vks:
            _send(_key(vk, up=False))
            pressed.append(vk)
        _wait_until(time.perf_counter() + max(hold, 0.05), stop)
        return stop.reason
    finally:
        for vk in reversed(pressed):
            _send(_key(vk, up=True))


def cursor_pos():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def foreground_pid():
    """The process id of the window that has keyboard focus, or None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_center():
    """The centre of the window that has keyboard focus (e.g. the game), or None."""
    hwnd = user32.GetForegroundWindow()
    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


def window_at(x, y):
    """The top-level window under a screen point -> (process id, window class), or None."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return None
    hwnd = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, name, 256)
    return pid.value, name.value


# --- Screen and matching -----------------------------------------------------

def read_image(path):
    # np.fromfile + imdecode handles non-ASCII Windows paths, unlike cv2.imread.
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise ValueError(f"could not read image {path}")
    return image


def write_image(path, image, fast=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Fast mode trades file size for speed, for full-screen recording screenshots.
    params = [cv2.IMWRITE_PNG_COMPRESSION, 1] if fast else []
    cv2.imencode(".png", image, params)[1].tofile(str(path))


def ppm_data(image):
    """BGR array -> binary PPM, which tk.PhotoImage loads far faster than PNG."""
    h, w = image.shape[:2]
    return b"P6\n%d %d\n255\n" % (w, h) + cv2.cvtColor(image, cv2.COLOR_BGR2RGB).tobytes()


def grab(sct, bounds):
    """Capture `bounds` -> (BGR frame, (left, top) of the frame in screen coords)."""
    shot = sct.grab(bounds)
    return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR), (bounds["left"], bounds["top"])


def monitor_at(sct, x, y):
    """The monitor containing screen point (x, y), as mss bounds; the whole desktop if none."""
    for m in sct.monitors[1:]:
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return m
    return sct.monitors[0]


def region_bounds(region):
    left, top, width, height = region
    return {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}


class Hit(NamedTuple):
    """A rule's best match on screen."""
    score: float        # 0..1; the rule's `needed` score or more means found
    x: int              # the centre, which is what gets clicked (plus the rule's offset)
    y: int
    box: tuple = ()     # (left, top, width, height) of what matched, in screen pixels
    how: str = ""       # for the log: "at 110% size", "by outline", ...


def clip_region(a, b):
    """The overlap of two [left, top, width, height] boxes (None means everywhere)."""
    if a is None or b is None:
        return b if a is None else a
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (left, top, max(right - left, 0), max(bottom - top, 0))


def crop_area(frame, origin, region):
    """The part of a frame inside `region` (screen coords) -> (pixels, (left, top) on screen)."""
    if not region:
        return frame, origin
    ox, oy = origin
    left, top, width, height = (int(v) for v in region)
    x0, y0 = max(left - ox, 0), max(top - oy, 0)
    x1 = max(min(left + width - ox, frame.shape[1]), x0)
    y1 = max(min(top + height - oy, frame.shape[0]), y0)
    return np.ascontiguousarray(frame[y0:y1, x0:x1]), (ox + x0, oy + y0)


def match_template(template, frame, origin, region=None):
    """Best match of `template` within `region` of a frame -> Hit, or None if the
    region is smaller than the template."""
    area, (ax, ay) = crop_area(frame, origin, region)
    th, tw = template.shape[:2]
    if area.shape[0] < th or area.shape[1] < tw:
        return None
    result = cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (mx, my) = cv2.minMaxLoc(result)
    score = float(score) if np.isfinite(score) else 0.0
    return Hit(score, ax + mx + tw // 2, ay + my + th // 2, (ax + mx, ay + my, tw, th))


def resized(image, scale):
    h, w = image.shape[:2]
    size = (max(round(w * scale), 1), max(round(h * scale), 1))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)


SCALES = (0.7, 0.8, 0.9, 1.1, 1.2, 1.3)  # 100% is the plain match, tried first


def match_scaled(template, frame, origin, region=None):
    """Best match of `template` at 70–130% of its size -> (Hit, scale), or (None, 1.0).

    For when the game's window or UI scale isn't what it was when the template
    was snipped. Sizes 10% apart are tried on a half-size copy of the screen, which
    is quick, then the best one is refined at full size, 5% either side, close to
    where it was found.
    """
    area, (ax, ay) = crop_area(frame, origin, region)
    if area.size == 0:
        return None, 1.0
    # Small templates lose too much at half size: search those at full size.
    f = 0.5 if min(template.shape[:2]) >= 24 and min(area.shape[:2]) >= 48 else 1.0
    small = resized(area, f) if f != 1 else area
    coarse = None  # (score, scale, centre x, centre y) in area pixels
    for s in SCALES:
        t = resized(template, s * f)
        if not (4 <= t.shape[0] <= small.shape[0] and 4 <= t.shape[1] <= small.shape[1]):
            continue
        _, score, _, (mx, my) = cv2.minMaxLoc(cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED))
        if np.isfinite(score) and (coarse is None or score > coarse[0]):
            coarse = (score, s, (mx + t.shape[1] / 2) / f, (my + t.shape[0] / 2) / f)
    if coarse is None:
        return None, 1.0
    _, s0, cx, cy = coarse
    best, best_scale = None, s0
    for s in (s0 - 0.05, s0, s0 + 0.05):
        t = resized(template, s)
        th, tw = t.shape[:2]
        pad = max(tw, th) // 4 + 8
        near = (ax + cx - tw / 2 - pad, ay + cy - th / 2 - pad, tw + 2 * pad, th + 2 * pad)
        hit = match_template(t, frame, origin, clip_region(near, region))
        if hit is not None and (best is None or hit.score > best.score):
            best, best_scale = hit, s
    return best, best_scale


def outline(image):
    """The edges in an image, which stay put when a game's colours or theme change."""
    return cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 60, 160)


def match_outline(template_edges, frame, origin, region=None):
    """Like match_template, but on edges: finds a button whose colours have changed.
    Edge scores separate real matches from look-alikes about as well as plain ones,
    so the rule's usual threshold applies. -> Hit, or None."""
    area, (ax, ay) = crop_area(frame, origin, region)
    th, tw = template_edges.shape[:2]
    if area.shape[0] < th or area.shape[1] < tw:
        return None
    _, score, _, (mx, my) = cv2.minMaxLoc(
        cv2.matchTemplate(outline(area), template_edges, cv2.TM_CCOEFF_NORMED))
    score = float(score) if np.isfinite(score) else 0.0
    return Hit(score, ax + mx + tw // 2, ay + my + th // 2, (ax + mx, ay + my, tw, th), "by outline")


def find_color(rgb, tolerance, min_area, frame, origin, region=None):
    """The biggest patch of a colour -> Hit, or None if the region is empty.

    A pixel counts when each of its red, green and blue is within `tolerance` of
    `rgb`. The score is the patch's size over `min_area`, capped at 1, so 1.0
    means found; the centre is the middle of the patch.
    """
    area, (ax, ay) = crop_area(frame, origin, region)
    if area.size == 0:
        return None
    bgr = np.array(rgb[::-1], dtype=int)
    mask = cv2.inRange(area, np.clip(bgr - tolerance, 0, 255).astype(np.uint8),
                       np.clip(bgr + tolerance, 0, 255).astype(np.uint8))
    n, _, stats, centres = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        h, w = area.shape[:2]
        return Hit(0.0, ax + w // 2, ay + h // 2, (ax, ay, w, h))
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    left, top, w, h, size = (int(v) for v in stats[i])
    cx, cy = centres[i]
    return Hit(min(size / max(min_area, 1), 1.0), ax + int(round(cx)), ay + int(round(cy)),
               (ax + left, ay + top, w, h), f"{size} px")


def parse_color(value):
    """'#ff8800' or [255, 136, 0] -> (255, 136, 0)."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
            raise ValueError(f"colour must look like #ff8800, not {value!r}")
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    if len(value) != 3:
        raise ValueError("colour must be #rrggbb or [red, green, blue]")
    return tuple(int(v) for v in value)


def color_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


DETECTS = ("image", "color", "text")
ACTIONS = ("click", "move", "key", "macro", "none")


# eq=False keeps identity hashing, so the GUI can key live scores by rule.
@dataclass(eq=False)
class Rule:
    name: str
    image: Path
    template: np.ndarray = field(repr=False)
    enabled: bool = True
    threshold: float = 0.85
    region: tuple | None = None  # (left, top, width, height) in screen pixels
    action: str = "click"        # click | move | key | none
    button: str = "left"
    offset: tuple = (0, 0)       # added to the match centre before acting
    key: str = ""                # for action "key": "space", "e", "ctrl+s", ...
    cooldown: float = 1.0
    pause_after: float = 0.0     # watching: after acting, check no rule for this many seconds
    restore_cursor: bool = False
    hold: float = 0.0            # for action "click": seconds to hold the button down (0 = a click)
    path: list = field(default_factory=list, repr=False)  # drag: [(t, dx, dy), ...] from the press point
    # Spam: a quick click or key press is repeated this many times, this many seconds apart.
    repeat: int = 1
    repeat_gap: float = 0.1
    # For a hold or drag: let go as soon as this image appears (e.g. a charge bar
    # lined up with its mark). `hold` is then the longest to wait for it.
    release_image: Path | None = None
    release_template: np.ndarray | None = field(default=None, repr=False)
    release_threshold: float = 0.9
    release_delay: float = 0.0  # seconds to keep holding after it appears (your reaction time)
    release_region: tuple | None = None  # where to look for it; usually just around the snip
    # What to look for: "image" (the template), "color" (a patch of one colour, or a
    # single pixel with a 1×1 region) or "text" (words, read by Windows' OCR).
    detect: str = "image"
    color: tuple = (255, 255, 255)  # colour: red, green, blue
    tolerance: int = 24             # colour: how far each channel may be off
    min_area: int = 20              # colour: the patch must be at least this many pixels
    text: str = ""                  # text: the words to find (any case)
    multiscale: bool = False        # image: also try it at 70–130% of its size
    edges: bool = False             # image: also try matching its outline
    macro: str = ""                 # for action "macro": the macro to play
    last_fired: float = field(default=float("-inf"), repr=False)
    fired: int = field(default=0, repr=False)
    # Where (and at what size) the image was last found: looked at first next time,
    # which is far quicker than searching the whole screen. (x, y, scale) or None.
    seen: tuple | None = field(default=None, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def needed(self):
        """The score that counts as found: a colour patch must reach its full size."""
        return 1.0 if self.detect == "color" else self.threshold

    @property
    def preview(self):
        """A picture of what the rule looks for, for the window: the image itself, a
        swatch of the colour, or the text."""
        if self.detect == "image":
            return self.template
        key = (self.detect, self.color, self.text)
        if self._cache.get("preview_key") != key:
            if self.detect == "color":
                image = np.full((40, 64, 3), self.color[::-1], np.uint8)
                cv2.rectangle(image, (0, 0), (63, 39), (60, 52, 45), 1)
            else:
                text = self.text.encode("ascii", "replace").decode() or "?"
                (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                image = np.full((h + 22, w + 20, 3), (30, 34, 38), np.uint8)
                cv2.putText(image, text, (10, h + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            (220, 230, 236), 2, cv2.LINE_AA)
            self._cache.update(preview_key=key, preview=image)
        return self._cache["preview"]

    def describe_target(self):
        """'a 64×32 image', 'the colour #ff8800', 'the text “Play”'."""
        if self.detect == "color":
            pixel = self.region is not None and tuple(self.region[2:]) == (1, 1)
            return f"the colour {color_hex(self.color)}" + (" at one pixel" if pixel else "")
        if self.detect == "text":
            return f"the text “{self.text}”"
        h, w = self.template.shape[:2]
        return f"a {w}×{h} image"

    @property
    def motion(self):
        """What a click rule does with the button: "click", "hold" or "drag";
        what a key rule does with the key: "press" or "hold"."""
        if self.action == "key":
            return "hold" if self.hold > 0 else "press"
        return "drag" if self.path else "hold" if self.hold > 0 else "click"

    def validate(self):
        if self.detect not in DETECTS:
            raise ValueError(f"{self.name}: detect must be one of {', '.join(DETECTS)}")
        if self.detect == "color":
            if len(self.color) != 3 or not all(0 <= c <= 255 for c in self.color):
                raise ValueError(f"{self.name}: colour must be three values from 0 to 255")
            if not 0 <= self.tolerance <= 255:
                raise ValueError(f"{self.name}: colour tolerance must be between 0 and 255")
            if not 1 <= self.min_area <= 10_000_000:
                raise ValueError(f"{self.name}: the smallest patch must be at least 1 pixel")
        if self.detect == "text" and not self.text.strip():
            raise ValueError(f"{self.name}: text detection needs the text to look for")
        if self.action not in ACTIONS:
            raise ValueError(f"{self.name}: action must be one of {', '.join(ACTIONS)}")
        if self.action == "macro" and not self.macro:
            raise ValueError(f"{self.name}: action 'macro' needs the name of the macro to play")
        if self.button not in MOUSE_BUTTONS:
            raise ValueError(f"{self.name}: button must be one of {', '.join(MOUSE_BUTTONS)}")
        if self.action == "key":
            if not self.key:
                raise ValueError(f"{self.name}: action 'key' needs a key, e.g. \"space\" or \"ctrl+s\"")
            parse_combo(self.key)
        if self.region is not None and (len(self.region) != 4
                                        or self.region[2] <= 0 or self.region[3] <= 0):
            raise ValueError(f"{self.name}: region must be [left, top, width, height]")
        if not 0 < self.threshold <= 1:
            raise ValueError(f"{self.name}: threshold must be between 0 and 1")
        if self.cooldown < 0:
            raise ValueError(f"{self.name}: cooldown can't be negative")
        if not 0 <= self.hold <= 600:
            raise ValueError(f"{self.name}: hold must be between 0 and 600 seconds")
        if not 1 <= self.repeat <= 10000:
            raise ValueError(f"{self.name}: repeat must be between 1 and 10000")
        if not 0 <= self.repeat_gap <= 60:
            raise ValueError(f"{self.name}: the gap between repeats must be 0 to 60 seconds")
        if self.repeat > 1 and self.motion not in ("click", "press"):
            raise ValueError(f"{self.name}: only a quick click or key press can be repeated "
                             "(set the hold time to 0)")
        if any(len(p) != 3 for p in self.path):
            raise ValueError(f"{self.name}: path must be a list of [seconds, dx, dy]")
        self.path = sorted((float(t), int(dx), int(dy)) for t, dx, dy in self.path)
        if self.release_template is not None:
            if self.action not in ("click", "key") or self.motion not in ("hold", "drag"):
                raise ValueError(f"{self.name}: a release image needs a click or key with a hold "
                                 "time (the longest to hold while waiting for it)")
            if not 0 <= self.release_delay <= 5:
                raise ValueError(f"{self.name}: release delay must be between 0 and 5 seconds")
            if not 0 < self.release_threshold <= 1:
                raise ValueError(f"{self.name}: release threshold must be between 0 and 1")
            if self.release_region is not None and (len(self.release_region) != 4
                                                    or self.release_region[2] <= 0
                                                    or self.release_region[3] <= 0):
                raise ValueError(f"{self.name}: release region must be [left, top, width, height]")

    def find(self, frame, origin):
        """Best match in this rule's region -> Hit, or None if the region is smaller
        than the image. Raises ValueError if text detection can't run (no OCR)."""
        if self.detect == "color":
            return find_color(self.color, self.tolerance, self.min_area, frame, origin, self.region)
        if self.detect == "text":
            import imgocr  # only text rules need Windows' OCR packages
            return imgocr.find_text(self.text, frame, origin, self.region)
        return self._find_image(frame, origin)

    NEAR = 24  # px around where the image was last found, looked at first

    def _find_image(self, frame, origin):
        if self.seen is not None:
            x, y, scale = self.seen
            t = self.template if scale == 1 else self._scaled(scale)
            th, tw = t.shape[:2]
            near = (x - tw // 2 - self.NEAR, y - th // 2 - self.NEAR, tw + 2 * self.NEAR,
                    th + 2 * self.NEAR)
            hit = match_template(t, frame, origin, clip_region(near, self.region))
            if hit is not None and hit.score >= self.threshold:
                self.seen = (hit.x, hit.y, scale)
                return hit._replace(how=self._size_note(scale))
        hit, scale = match_template(self.template, frame, origin, self.region), 1.0
        if self.multiscale and (hit is None or hit.score < self.threshold):
            other, s = match_scaled(self.template, frame, origin, self.region)
            if other is not None and (hit is None or other.score > hit.score):
                hit, scale = other._replace(how=self._size_note(s)), s
        if self.edges and (hit is None or hit.score < self.threshold):
            other = match_outline(self._edges(), frame, origin, self.region)
            if other is not None and (hit is None or other.score > hit.score):
                hit, scale = other, 1.0
        if hit is not None and hit.score >= self.threshold and hit.how != "by outline":
            self.seen = (hit.x, hit.y, scale)
        return hit

    @staticmethod
    def _size_note(scale):
        return "" if abs(scale - 1) < 0.01 else f"at {scale * 100:.0f}% size"

    def _scaled(self, scale):
        key = ("scaled", id(self.template), scale)
        if key not in self._cache:
            self._cache[key] = resized(self.template, scale)
        return self._cache[key]

    def _edges(self):
        key = ("edges", id(self.template))
        if key not in self._cache:
            self._cache[key] = outline(self.template)
        return self._cache[key]

    def find_release(self, frame, origin):
        """Best match of the release image -> Hit, or None."""
        return match_template(self.release_template, frame, origin, self.release_region)

    @property
    def spam(self):
        """"×10 every 0.1 s" for a repeated click or key press, else ""."""
        return f"×{self.repeat} every {self.repeat_gap:g} s" if self.repeat > 1 else ""

    def _repeat(self, act, cancel):
        """Run `act()` `repeat` times, starting `repeat_gap` apart (a click itself takes
        ~40 ms, so a smaller gap just means back to back). -> a note if `cancel()` cut it short."""
        start = time.perf_counter()
        for i in range(self.repeat):
            # Check cancel even when already behind schedule (the wait then returns at once).
            due = start + i * self.repeat_gap + _human_delay()
            if i and (not _wait_until(due, cancel) or (cancel and cancel())):
                return f"stopped after {i} of {self.repeat}"
            act()
        return None

    def fire(self, x, y, cancel=None, play=None):
        """Act on a match centred at (x, y). `cancel()` returning True cuts a hold or a
        run of repeated clicks short. `play(name, cancel)` plays a macro, for action
        "macro" (the watcher and flows pass one in), and returns a note.

        -> a note for the log about how a hold, repeat or macro ended, or None.
        """
        tx, ty = x + self.offset[0], y + self.offset[1]
        note = None
        if self.action == "macro":
            note = play(self.macro, cancel) if play else f"can't play macro “{self.macro}” here"
        elif self.action == "click":
            previous = cursor_pos() if self.restore_cursor else None
            if self.motion == "click":
                note = self._repeat(lambda: click(tx, ty, self.button), cancel)
            elif self.release_template is None:
                hold_click(tx, ty, self.button, self.hold, self.path, cancel)
            else:
                note = self._hold_until_release(lambda until: hold_click(
                    tx, ty, self.button, self.hold, self.path, cancel, until))
            if previous:
                move_to(*previous)
        elif self.action == "move":
            move_to(tx, ty)
        elif self.action == "key":
            vks = parse_combo(self.key)
            if self.motion == "press":
                note = self._repeat(lambda: press(vks), cancel)
            elif self.release_template is None:
                hold_keys(vks, self.hold, cancel)
            else:
                note = self._hold_until_release(lambda until: hold_keys(vks, self.hold, cancel, until))
        return note

    # A moving bar rarely lands on exactly the recorded pixels, so the score may
    # peak just under the threshold. Within NEAR_MISS of it, let go as soon as the
    # score starts falling (by more than PEAK_DROP), i.e. just after the closest match.
    NEAR_MISS = 0.1
    PEAK_DROP = 0.03

    def _hold_until_release(self, hold):
        """Run `hold(until)` (a hold_click or hold_keys) with `until` watching for the
        release image. -> a note for the log."""
        best, last, matched, seen_at = 0.0, 0.0, None, None
        with mss.MSS() as sct:
            # Only the release area is grabbed, so it can be checked every few milliseconds.
            bounds = region_bounds(self.release_region) if self.release_region else sct.monitors[0]

            def seen():
                nonlocal best, last, matched, seen_at
                if seen_at is not None:  # seen: now just waiting out the delay
                    return time.perf_counter() >= seen_at + self.release_delay
                hit = self.find_release(*grab(sct, bounds))
                last = hit[0] if hit else 0.0
                peaked = best >= self.release_threshold - self.NEAR_MISS and last < best - self.PEAK_DROP
                best = max(best, last)
                if last >= self.release_threshold or peaked:
                    matched = "image" if last >= self.release_threshold else "closest"
                    seen_at = time.perf_counter()
                    return self.release_delay <= 0
                return False

            start = time.perf_counter()
            reason = hold(seen)
        took = time.perf_counter() - start
        delay = f" + {self.release_delay * 1000:.0f} ms" if self.release_delay else ""
        if reason == "until" and matched == "image":
            return f"released on the release image ({last:.3f}){delay} after {took:.2f} s"
        if reason == "until":
            return f"released just after the closest match ({best:.3f}){delay} after {took:.2f} s"
        if reason == "cancel":
            return f"released after {took:.2f} s: stopped"
        return f"release image not seen in {took:.1f} s (best score {best:.3f}), released anyway"


def action_words(rule):
    """'left click', 'hold e 1.5 s', 'click ×20', 'play macro “Farm”'... for the log."""
    if rule is None or rule.action == "none":
        return "do nothing"
    if rule.action == "move":
        return "move the mouse there"
    if rule.action == "macro":
        return f"play macro “{rule.macro}”"
    spam = f" ×{rule.repeat}" if rule.repeat > 1 else ""
    if rule.action == "key":
        if rule.motion == "press":
            return f"press {rule.key}{spam}"
        release = " until the release image" if rule.release_template is not None else f" {rule.hold:g} s"
        return f"hold {rule.key}{release}"
    if rule.motion == "click":
        return f"{rule.button} click{spam}"
    release = " until the release image" if rule.release_template is not None else f" {rule.hold:g} s"
    return f"{rule.button} {rule.motion}{release}"


def rule_from_dict(r, base, index=1):
    name = r.get("name", f"rule {index}")
    detect = r.get("detect", "image")
    image, template = Path(), None
    if detect == "image":
        if "image" not in r:
            raise ValueError(f"{name}: missing 'image'")
        # Image paths are relative to the config file, not the working directory.
        image = Path(r["image"])
        if not image.is_absolute():
            image = Path(base) / image
        template = read_image(image)
    region = r.get("region")
    rule = Rule(
        name=name,
        image=image,
        template=template,
        detect=detect,
        color=parse_color(r.get("color", "#ffffff")),
        tolerance=int(r.get("tolerance", 24)),
        min_area=int(r.get("min_area", 20)),
        text=str(r.get("text", "")),
        multiscale=bool(r.get("multiscale", False)),
        edges=bool(r.get("edges", False)),
        macro=str(r.get("macro", "")),
        enabled=bool(r.get("enabled", True)),
        threshold=float(r.get("threshold", 0.85)),
        region=tuple(region) if region else None,
        action=r.get("action", "click"),
        button=r.get("button", "left"),
        offset=tuple(r.get("offset", (0, 0))),
        key=r.get("key", ""),
        cooldown=float(r.get("cooldown", 1.0)),
        pause_after=max(float(r.get("pause_after", 0.0)), 0.0),
        restore_cursor=bool(r.get("restore_cursor", False)),
        hold=float(r.get("hold", 0.0)),
        repeat=int(r.get("repeat", 1)),
        repeat_gap=float(r.get("repeat_gap", 0.1)),
        path=list(r.get("path", [])),
    )
    if r.get("release_image"):
        release = Path(r["release_image"])
        if not release.is_absolute():
            release = Path(base) / release
        rule.release_image = release
        rule.release_template = read_image(release)
        rule.release_threshold = float(r.get("release_threshold", 0.9))
        rule.release_delay = float(r.get("release_delay", 0.0))
        region = r.get("release_region")
        rule.release_region = tuple(region) if region else None
    rule.validate()
    return rule


def _relative(path, base):
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # on a different drive from the config
        return str(path)


def rule_to_dict(rule, base):
    data = {"name": rule.name, "enabled": rule.enabled}
    if rule.detect == "image":
        data["image"] = _relative(rule.image, base)
        if rule.multiscale:
            data["multiscale"] = True
        if rule.edges:
            data["edges"] = True
    elif rule.detect == "color":
        data.update(detect="color", color=color_hex(rule.color), tolerance=rule.tolerance,
                    min_area=rule.min_area)
    else:
        data.update(detect="text", text=rule.text)
    data.update({
        "threshold": round(rule.threshold, 3),
        "region": list(rule.region) if rule.region else None,
        "action": rule.action,
        "button": rule.button,
        "offset": list(rule.offset),
        "cooldown": rule.cooldown,
        "restore_cursor": rule.restore_cursor,
    })
    if rule.action == "key":
        data["key"] = rule.key
    if rule.action == "macro":
        data["macro"] = rule.macro
    if rule.pause_after:
        data["pause_after"] = round(rule.pause_after, 3)
    if rule.repeat > 1:
        data["repeat"] = rule.repeat
        data["repeat_gap"] = rule.repeat_gap
    if rule.hold:
        data["hold"] = round(rule.hold, 3)
    if rule.release_image:
        data["release_image"] = _relative(rule.release_image, base)
        data["release_threshold"] = round(rule.release_threshold, 3)
        data["release_delay"] = round(rule.release_delay, 3)
        data["release_region"] = list(rule.release_region) if rule.release_region else None
    if rule.path:
        data["path"] = [[round(t, 3), dx, dy] for t, dx, dy in rule.path]
    return data


@dataclass
class Config:
    rules: list
    interval: float = 0.25
    stop_key: str = "F8"
    pause_key: str = "F7"
    start_key: str = "F6"  # used by the GUI only
    record_gap: float = 1.0  # recording: clicks closer together than this share a screenshot
    record_crop: int = 96    # recording: size of the automatic crop around each click
    record_key: str = ""     # GUI: starts and stops recording on the shown tab; "" = none
    humanize_px: int = 0     # vary each click's position by up to this many pixels...
    humanize_ms: int = 0     # ...and each click, press and spam gap by up to this many ms
    show_matches: bool = False  # GUI: draw a box on screen around each match while running
    # Flows (step-by-step macros with branches), recorded macros, schedules and the
    # Anti-AFK settings, as plain JSON for imgflow.py, imgplay.py and imgauto.py to
    # parse; this file only keeps them safe across a load and save.
    flows: list = field(default_factory=list)
    macros: list = field(default_factory=list)
    schedules: list = field(default_factory=list)
    anti_afk: dict = field(default_factory=dict)

    def hotkeys(self):
        """The hotkeys in use: start, stop, pause and (if set) record."""
        return [self.start_key, self.stop_key, self.pause_key] + ([self.record_key] if self.record_key else [])

    def validate(self):
        if self.interval <= 0:
            raise ValueError("interval must be greater than 0")
        keys = self.hotkeys()
        for key in keys:
            parse_key(key)
        if len({k.lower() for k in keys}) != len(keys):
            raise ValueError("the start, stop, pause and record keys must be different")
        if self.record_gap < 0:
            raise ValueError("the recording screenshot gap can't be negative")
        if not 8 <= self.record_crop <= 2000:
            raise ValueError("the recording crop size must be between 8 and 2000 px")
        if not 0 <= self.humanize_px <= 50 or not 0 <= self.humanize_ms <= 500:
            raise ValueError("humanize can vary clicks by 0 to 50 px and 0 to 500 ms")


def load_config(path):
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    rules = [rule_from_dict(r, path.parent, i) for i, r in enumerate(raw.get("rules", []), 1)]
    cfg = Config(rules=rules,
                 interval=float(raw.get("interval", 0.25)),
                 stop_key=raw.get("stop_key", "F8"),
                 pause_key=raw.get("pause_key", "F7"),
                 start_key=raw.get("start_key", "F6"),
                 record_gap=float(raw.get("record_gap", 1.0)),
                 record_crop=int(raw.get("record_crop", 96)),
                 record_key=raw.get("record_key", "") or "",
                 humanize_px=int(raw.get("humanize_px", 0)),
                 humanize_ms=int(raw.get("humanize_ms", 0)),
                 show_matches=bool(raw.get("show_matches", False)),
                 flows=list(raw.get("flows", [])),
                 macros=list(raw.get("macros", [])),
                 schedules=list(raw.get("schedules", [])),
                 anti_afk=dict(raw.get("anti_afk", {})))
    cfg.validate()
    return cfg


def save_config(cfg, path):
    path = Path(path)
    data = {
        "interval": cfg.interval,
        "start_key": cfg.start_key,
        "stop_key": cfg.stop_key,
        "pause_key": cfg.pause_key,
        "record_gap": cfg.record_gap,
        "record_crop": cfg.record_crop,
    }
    # Newer settings are only written when they're in use, so older configs stay as they were.
    for key, default in (("record_key", ""), ("humanize_px", 0), ("humanize_ms", 0),
                         ("show_matches", False)):
        if getattr(cfg, key) != default:
            data[key] = getattr(cfg, key)
    data["rules"] = [rule_to_dict(r, path.parent) for r in cfg.rules]
    for key in ("flows", "macros", "schedules", "anti_afk"):
        if getattr(cfg, key):
            data[key] = getattr(cfg, key)
    text = json.dumps(data, indent=2)
    # Keep each recorded drag point ([t, dx, dy]) on one line instead of five.
    text = re.sub(r"\[\s+(-?[\d.e+-]+),\s+(-?\d+),\s+(-?\d+)\s+\]", r"[\1, \2, \3]", text)
    path.write_text(text + "\n", encoding="utf-8")


def capture_bounds(sct, rules):
    """Grab only the union of rule regions when every rule has one; else the whole desktop."""
    if not rules or not all(rule.region for rule in rules):
        return sct.monitors[0]
    left = min(r.region[0] for r in rules)
    top = min(r.region[1] for r in rules)
    right = max(r.region[0] + r.region[2] for r in rules)
    bottom = max(r.region[1] + r.region[3] for r in rules)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


class Control:
    """Stop and pause for something running on a worker thread (rules, a flow, a
    macro): from the window's buttons, or the stop and pause keys, which
    `stopping()` notices, so call it often. `outcome` says how the run ended:
    "finished", "failed", "stopped" or "error"."""

    def __init__(self, cfg, on_log=print, on_pause=None):
        self.cfg = cfg
        self.on_log, self.on_pause = on_log, on_pause
        self.stop_event = threading.Event()
        self.paused = False
        self.stop_vk, self.pause_vk = parse_key(cfg.stop_key), parse_key(cfg.pause_key)
        self.pause_was_down = key_down(self.pause_vk)
        self.outcome = None

    def stop(self):
        self.stop_event.set()

    def stopping(self):
        """True once the run should stop. Also notices the stop and pause keys."""
        if key_down(self.stop_vk):
            self.stop_event.set()
        down = key_down(self.pause_vk)
        if down and not self.pause_was_down:
            self.set_paused(not self.paused)
        self.pause_was_down = down
        return self.stop_event.is_set()

    def set_paused(self, paused):
        if paused != self.paused:
            self.paused = paused
            self.on_log("Paused." if paused else "Resumed.")
            if self.on_pause:
                self.on_pause(paused)


class Watcher(Control):
    """The watch loop shared by the CLI and the GUI.

    `run()` blocks until stopped, so the GUI calls it on a worker thread and the
    callbacks fire on that thread: `on_log(text)`, `on_tick({rule: score})` after
    every screen check, `on_hit(rule, hit)` when a rule acts, and `on_pause(paused)`.
    `macros` ({name: imgplay.Macro}) are the macros a "macro" rule can play.
    """

    def __init__(self, cfg, dry_run=False, on_log=print, on_tick=None, on_pause=None,
                 on_hit=None, macros=None):
        super().__init__(cfg, on_log, on_pause)
        self.dry_run = dry_run
        self.on_tick, self.on_hit = on_tick, on_hit
        self.macros = macros or {}
        self.rules = [r for r in cfg.rules if r.enabled]
        self.resume_at = 0.0  # monotonic time; no rule is checked before it (a rule's pause_after)
        self.broken = set()  # rules that can't run here (text without OCR), reported once

    # Checked often, also while a rule holds the mouse button or plays a macro, so a
    # long action can be stopped.
    _stop_requested = Control.stopping

    def _play(self, name, cancel):
        """Play a macro for a "macro" rule. -> a note for the log."""
        import imgplay  # here: imgplay imports this file
        macro = self.macros.get(name)
        if macro is None:
            return f"there's no macro called “{name}”"
        result = imgplay.play(macro, self.cfg, stopping=cancel, paused=lambda: self.paused,
                              on_log=self.on_log, dry_run=self.dry_run)
        return f"macro “{name}” {result}"

    def run(self):
        self.outcome = "stopped"
        set_humanize(self.cfg.humanize_px, self.cfg.humanize_ms)
        try:
            with mss.MSS() as sct:
                bounds = capture_bounds(sct, self.rules)
                while not self._stop_requested():
                    tick = time.perf_counter()
                    if not self.paused and time.monotonic() >= self.resume_at:
                        self._check(sct, bounds)
                    # Sleep out the interval in short slices so the stop key stays responsive.
                    while time.perf_counter() - tick < self.cfg.interval:
                        if self._stop_requested():
                            break
                        time.sleep(0.01)
        except Exception as exc:  # report, rather than silently kill the GUI's worker thread
            self.outcome = "error"
            self.on_log(f"error: {exc}")
        self.on_log("Stopped.")

    def _check(self, sct, bounds):
        frame, origin = grab(sct, bounds)
        scores = {}
        for rule in self.rules:  # list order is priority order
            if rule in self.broken or time.monotonic() - rule.last_fired < rule.cooldown:
                continue
            try:
                hit = rule.find(frame, origin)
            except ValueError as exc:  # e.g. text detection without the OCR packages
                self.broken.add(rule)
                self.on_log(f"{rule.name}: {exc}. Skipping this rule.")
                continue
            if hit is None:
                continue
            score, x, y = hit.score, hit.x, hit.y
            scores[rule] = score
            if self.paused or self.stop_event.is_set():  # changed while we were matching
                break
            if score >= rule.needed:
                if self.on_hit:
                    self.on_hit(rule, hit)
                action = {"click": rule.motion, "key": f"{rule.motion} {rule.key}",
                          "macro": f"play macro “{rule.macro}”"}.get(rule.action, rule.action)
                if rule.action in ("click", "key") and rule.spam:
                    action += f" {rule.spam}"
                how = f" ({hit.how})" if hit.how else ""
                self.on_log(f"{time.strftime('%H:%M:%S')}  {rule.name}: "
                            f"{score:.3f}{how} at ({x}, {y}) -> {action}")
                if not self.dry_run or rule.action == "macro":  # a macro dry-runs itself
                    note = rule.fire(x, y, cancel=self._stop_requested, play=self._play)
                    if note:
                        self.on_log(f"{time.strftime('%H:%M:%S')}  {rule.name}: {note}")
                rule.last_fired = time.monotonic()
                rule.fired += 1
                if rule.pause_after:
                    self.resume_at = rule.last_fired + rule.pause_after
                    self.on_log(f"{time.strftime('%H:%M:%S')}  {rule.name}: "
                                f"pausing {rule.pause_after:g} s")
                # Acting changes the screen, so re-capture before checking more rules.
                break
        if self.on_tick:
            self.on_tick(scores)


class Selection(NamedTuple):
    crop: np.ndarray   # the selected pixels
    region: list       # [left, top, width, height] in screen pixels
    frame: np.ndarray  # the whole desktop screenshot, for re-cropping later
    origin: tuple      # screen coordinates of the frame's top-left corner


def select_region(master=None, point=False):
    """Freeze the desktop in a fullscreen overlay and let the user drag a box.

    With `point`, a plain click picks one pixel (a 1×1 selection), e.g. to pick a
    colour; dragging a box still works. -> Selection, or None if cancelled. Pass
    the GUI's root window as `master`; the CLI passes None.
    """
    import tkinter as tk

    with mss.MSS() as sct:
        bounds = sct.monitors[0]
        frame, (left, top) = grab(sct, bounds)

    root = master or tk.Tk()
    if master is None:
        root.withdraw()
    overlay = tk.Toplevel(root)
    overlay.overrideredirect(True)
    overlay.attributes("-topmost", True)
    # "+-1920" (not "-1920") keeps negative offsets measured from the left edge.
    overlay.geometry(f"{bounds['width']}x{bounds['height']}+{left}+{top}")
    canvas = tk.Canvas(overlay, highlightthickness=0, cursor="cross")
    canvas.pack(fill="both", expand=True)
    photo = tk.PhotoImage(master=overlay, data=ppm_data(frame), format="PPM")
    canvas.create_image(0, 0, anchor="nw", image=photo)
    hint = ("Click a pixel, or drag a box   ·   Esc to cancel" if point
            else "Drag to select   ·   Esc to cancel")
    canvas.create_rectangle(10, 10, 560 if point else 420, 46, fill="black", outline="")
    canvas.create_text(20, 28, anchor="w", fill="yellow", font=("Segoe UI", 13, "bold"), text=hint)
    box = canvas.create_rectangle(0, 0, 0, 0, outline="red", width=2)
    size = canvas.create_text(0, 0, anchor="sw", fill="red", font=("Segoe UI", 10, "bold"))
    selection = {}

    def on_press(e):
        selection["start"] = (e.x, e.y)

    def on_drag(e):
        if "start" in selection:
            ax, ay = selection["start"]
            canvas.coords(box, ax, ay, e.x, e.y)
            canvas.coords(size, min(ax, e.x), min(ay, e.y) - 4)
            canvas.itemconfigure(size, text=f"{abs(e.x - ax)} × {abs(e.y - ay)}")

    def on_release(e):
        if "start" not in selection:
            return
        (ax, ay), (bx, by) = selection["start"], (e.x, e.y)
        x0, x1 = sorted((max(ax, 0), max(bx, 0)))
        y0, y1 = sorted((max(ay, 0), max(by, 0)))
        if x1 - x0 >= 4 and y1 - y0 >= 4:
            selection["rect"] = (x0, y0, x1, y1)
            overlay.destroy()
        elif point and 0 <= bx < frame.shape[1] and 0 <= by < frame.shape[0]:
            selection["rect"] = (bx, by, bx + 1, by + 1)
            overlay.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", lambda e: overlay.destroy())
    overlay.focus_force()
    overlay.grab_set()
    root.wait_window(overlay)
    if master is None:
        root.destroy()

    if "rect" not in selection:
        return None
    x0, y0, x1, y1 = selection["rect"]
    return Selection(frame[y0:y1, x0:x1].copy(), [left + x0, top + y0, x1 - x0, y1 - y0],
                     frame, (left, top))


# --- Click and key recording -------------------------------------------------

@dataclass(eq=False)
class Capture:
    """One recorded click, hold, drag or key press, and the full-desktop screenshot
    taken for it."""
    x: int                # screen coordinates where the button went down (keys: the mouse)
    y: int
    button: str           # "left" or "right"; "" for a key
    origin: tuple         # screen coordinates of the screenshot's top-left corner
    path: Path            # the screenshot, written in the background
    rect: tuple           # automatic crop around the click, in screenshot pixels (x0, y0, x1, y1)
    crop: np.ndarray = field(repr=False)
    time: float = 0.0     # time.monotonic() of the most recent click counted here
    clicks: int = 1       # clicks merged into this screenshot (more than 1 = spam-clicking)
    start: float = 0.0    # time.monotonic() of the first click: with `time`, how fast a burst was
    hold: float = 0.0     # seconds the button was held, for a hold or drag (0 = a click)
    moves: list = field(default_factory=list, repr=False)  # drag: [(t, dx, dy), ...] from (x, y)
    # For a hold or drag: screenshots of the monitor it was on, from just before to
    # just after the button came up, to crop a "release when this appears" image
    # from. [(path, seconds from the release)], in order.
    release_shots: list = field(default_factory=list, repr=False)
    release_choice: int = 0             # the one to show first: the last before the release
    release_origin: tuple | None = None  # screen coordinates of their top-left corner
    release_point: tuple | None = None  # where the mouse was on release, in release-shot pixels
    key: str = ""         # for a key press: the key or combo, e.g. "e" or "ctrl+s"

    @property
    def point(self):
        """The press point in screenshot pixels."""
        return self.x - self.origin[0], self.y - self.origin[1]

    @property
    def kind(self):
        """"click", "hold" or "drag" for the mouse; "key" or "key hold" for a key."""
        if self.key:
            return "key hold" if self.hold else "key"
        return "drag" if self.moves else "hold" if self.hold else "click"


def crop_around(x, y, size, width, height):
    """A size x size box centred on (x, y), kept inside a width x height image."""
    w, h = min(size, width), min(size, height)
    x0 = min(max(x - w // 2, 0), width - w)
    y0 = min(max(y - h // 2, 0), height - h)
    return x0, y0, x0 + w, y0 + h


# Clicks on these never become captures: the taskbar, which you may click to get
# back to imgmacro when you're done.
IGNORED_WINDOW_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


@dataclass(eq=False)
class _Press:
    """A mouse button or key that's down right now, while the recorder follows it."""
    button: str
    x: int
    y: int
    start: float
    frame: np.ndarray | None  # None when this press may just be another spam click
    origin: tuple | None
    moves: list = field(default_factory=list)
    reach: int = 0            # furthest the mouse has gone from the press point, in px
    released: list = field(default_factory=list)  # holds: [(seconds from release, frame)]
    released_origin: tuple | None = None
    released_choice: int = 0
    key: str = ""             # for a key: the key or combo
    modifier: bool = False    # a ctrl/shift/alt key: only recorded if pressed on its own
    used: bool = False        # a modifier that became part of a combo, e.g. ctrl in ctrl+s


class ClickRecorder:
    """Record mouse clicks, holds and drags, and key presses and holds, with a
    full-desktop screenshot for each.

    Buttons and keys are noticed by polling their state every few milliseconds,
    the same way the hotkeys work, so no input hook is installed. While a button
    is down the mouse is followed, so on release the recorder knows whether it was
    a click, a hold (down for HOLD_MIN seconds or more) or a drag (moved DRAG_MIN
    px or more), and the path it took. Keys are a press or a hold in the same way;
    ctrl, shift and alt held with another key make a combo such as "ctrl+s".

    Spam-clicking doesn't produce a pile of screenshots: a click less than `gap`
    seconds after the previous click, near the same spot, is counted against that
    capture instead, and the same goes for pressing the same key again. Holds and
    drags always get their own capture. Clicks on our own windows and on the
    taskbar, keys typed into our own windows, and the hotkeys are ignored.

    Callbacks fire on the recorder thread: `on_capture(capture)` for a new capture
    (on release, once its kind is known), `on_merge(capture)` when a click is
    counted against an existing one, and `on_stop()` once recording has ended and
    every screenshot is on disk.
    """

    BUTTONS = {"left": 0x01, "right": 0x02}  # VK_LBUTTON, VK_RBUTTON
    HOLD_MIN = 0.3     # seconds; a quick click is well under this
    DRAG_MIN = 8       # px the mouse must move while held to count as a drag
    SAMPLE_GAP = 0.015  # seconds between recorded drag points
    MAX_POINTS = 4000
    KEEP_AFTER = 0.15   # seconds held before the screen is grabbed continuously, for release shots
    SHOTS_BEFORE = 3    # release screenshots from just before the button came up...
    SHOTS_AFTER = 2     # ...and just after, since the screen lags the game a little

    def __init__(self, folder, gap=1.0, crop_size=96, stop_key="F8", ignore_keys=(),
                 on_capture=None, on_merge=None, on_stop=None, on_log=print, also_stop=()):
        self.folder = Path(folder)
        self.gap, self.crop_size = gap, crop_size
        # The stop key ends a recording, and so do `also_stop` keys (the record key).
        self.stop_vks = {parse_key(k) for k in (stop_key, *also_stop) if k}
        # Every key with a name imgmacro can press, except the stop key and other hotkeys.
        skip = {*self.stop_vks, *(parse_key(k) for k in ignore_keys if k)}
        self.key_vks = [vk for vk in VK_TO_NAME if vk not in skip]
        self.on_capture, self.on_merge = on_capture, on_merge
        self.on_stop, self.on_log = on_stop, on_log
        self.captures = []
        self.clicks = 0
        self.keys = 0
        self.last_frame = None  # (frame, origin, screenshot path) of the latest capture
        # While a button is held, a second thread keeps grabbing the screen, so the
        # moments just before the release can be shown too.
        self.keeping = threading.Event()
        self.kept = []  # [(monotonic time the grab started, frame, origin)], the latest few
        self.keep_bounds = None  # the monitor being held on: faster to grab than the whole desktop
        self.kept_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        own_pid = os.getpid()
        # A button already held when recording starts (e.g. on the Record button) isn't a click.
        was_down = {b: key_down(vk) for b, vk in self.BUTTONS.items()}
        key_was_down = {vk: key_down(vk) for vk in self.key_vks}
        held = {}       # button -> _Press
        keys_held = {}  # vk -> _Press
        writer = ThreadPoolExecutor(max_workers=1)  # PNG encoding mustn't delay the next click
        keeper = threading.Thread(target=self._keep_frames, daemon=True)
        keeper.start()
        try:
            with mss.MSS() as sct:
                bounds = sct.monitors[0]
                while not self.stop_event.is_set():
                    if any(key_down(vk) for vk in self.stop_vks):
                        break
                    for button, vk in self.BUTTONS.items():
                        down = key_down(vk)
                        if down and not was_down[button]:
                            press = self._begin(sct, bounds, button, own_pid)
                            if press:
                                held[button] = press
                        elif down and button in held:
                            self._follow(held[button])
                        elif not down and button in held:
                            self._finish(held.pop(button), writer, sct, bounds)
                        was_down[button] = down
                    typing_here = foreground_pid() == own_pid  # e.g. naming a rule
                    for vk in self.key_vks:
                        down = key_down(vk)
                        if down and not key_was_down[vk] and not typing_here:
                            press = self._begin_key(sct, bounds, vk, keys_held)
                            if press:
                                keys_held[vk] = press
                        elif not down and vk in keys_held:
                            press = keys_held.pop(vk)
                            if not press.used:
                                self._finish(press, writer, sct, bounds)
                        key_was_down[vk] = down
                    now = time.monotonic()
                    holding = [p for p in [*held.values(), *keys_held.values()]
                               if now - p.start >= self.KEEP_AFTER and not p.used]
                    if holding:
                        self.keep_bounds = monitor_at(sct, holding[0].x, holding[0].y)
                        self.keeping.set()
                    else:
                        self.keeping.clear()
                    time.sleep(0.005)
                for press in [*held.values(), *keys_held.values()]:  # held when recording stopped
                    if not press.used:
                        self._finish(press, writer, sct, bounds)
        except Exception as exc:
            self.on_log(f"error while recording: {exc}")
        finally:
            self.stop_event.set()
            self.keeping.set()  # wake the keeper so it sees the stop
            keeper.join(2)
            writer.shutdown(wait=True)
            if self.on_stop:
                self.on_stop()

    def _begin(self, sct, bounds, button, own_pid):
        x, y = cursor_pos()
        owner = window_at(x, y)
        if owner and (owner[0] == own_pid or owner[1] in IGNORED_WINDOW_CLASSES):
            return None
        self.clicks += 1
        now = time.monotonic()
        last = self.captures[-1] if self.captures else None
        if (last is not None and last.kind == "click" and now - last.time < self.gap
                and abs(x - last.x) <= self.crop_size and abs(y - last.y) <= self.crop_size):
            # Probably another spam click: skip the screenshot. Decided on release.
            return _Press(button, x, y, now, None, None)
        # Grab now, while the screen still shows what was clicked.
        frame, origin = grab(sct, bounds)
        return _Press(button, x, y, now, frame, origin)

    def _begin_key(self, sct, bounds, vk, keys_held):
        # Keys go to the focused window, so that's where the crop starts and which
        # monitor the release screenshots show; the mouse may be anywhere.
        x, y = foreground_center() or cursor_pos()
        now = time.monotonic()
        name = VK_TO_NAME[vk]
        frame = origin = None
        if vk in MODIFIER_VKS:
            combo = name
        else:
            # Held modifiers make a combo, and aren't recorded on their own.
            mods = [p for p in keys_held.values() if p.modifier and not p.used]
            for p in mods:
                p.used = True
            order = list(MODIFIER_VKS.values())
            combo = "+".join(sorted({p.key for p in mods}, key=order.index) + [name])
            # The screen when the first modifier went down is the screen you reacted to.
            first = min(mods, key=lambda p: p.start, default=None)
            if first is not None and first.frame is not None:
                frame, origin = first.frame, first.origin
        self.keys += 1
        last = self.captures[-1] if self.captures else None
        if (frame is None and last is not None and last.kind == "key" and last.key == combo
                and now - last.time < self.gap):
            # Probably the same key pressed again quickly: skip the screenshot.
            return _Press("", x, y, now, None, None, key=combo, modifier=vk in MODIFIER_VKS)
        if frame is None:
            frame, origin = grab(sct, bounds)
        return _Press("", x, y, now, frame, origin, key=combo, modifier=vk in MODIFIER_VKS)

    def _keep_frames(self):
        with mss.MSS() as sct:
            while not self.stop_event.is_set():
                if not self.keeping.wait(0.05):
                    with self.kept_lock:
                        self.kept = []
                    continue
                bounds = self.keep_bounds
                if bounds is None:
                    continue
                t = time.monotonic()
                frame, origin = grab(sct, bounds)
                with self.kept_lock:
                    self.kept = (self.kept + [(t, frame, origin)])[-self.SHOTS_BEFORE:]

    def _release_frames(self, press, when, sct):
        """Screens of the press's monitor around a release at `when` ->
        ([(seconds from `when`, frame)], origin, index of the last one before it)."""
        bounds = monitor_at(sct, press.x, press.y)
        origin = (bounds["left"], bounds["top"])
        with self.kept_lock:
            before = [(t - when, f) for t, f, o in self.kept if press.start <= t <= when and o == origin]
        after = []
        for _ in range(self.SHOTS_AFTER):
            t = time.monotonic()
            after.append((t - when, grab(sct, bounds)[0]))
        # Many games react to the release at once, so the last one before it is the best guess.
        return before + after, origin, max(len(before) - 1, 0)

    def _follow(self, press):
        x, y = cursor_pos()
        dx, dy = x - press.x, y - press.y
        press.reach = max(press.reach, abs(dx), abs(dy))
        t = time.monotonic() - press.start
        last = press.moves[-1] if press.moves else (0.0, 0, 0)
        if ((dx, dy) != last[1:] and t - last[0] >= self.SAMPLE_GAP
                and len(press.moves) < self.MAX_POINTS):
            press.moves.append((round(t, 3), dx, dy))

    def _finish(self, press, writer, sct, bounds):
        self._follow(press)
        now = time.monotonic()
        held_for = now - press.start
        drag = not press.key and press.reach >= self.DRAG_MIN
        if not drag and held_for < self.HOLD_MIN:  # a plain click or key press
            if press.frame is None:
                last = self.captures[-1]
                last.clicks += 1
                last.time = now  # measure the gap from the latest click, so a long burst stays one capture
                if self.on_merge:
                    self.on_merge(last)
                return
            self._add(press, writer, now, 0.0, [])
            return
        moves = press.moves if drag else []
        if moves and moves[-1][0] < held_for:  # where the button came up
            x, y = cursor_pos()
            moves.append((round(held_for, 3), x - press.x, y - press.y))
        press.released, press.released_origin, press.released_choice = \
            self._release_frames(press, now, sct)
        self._add(press, writer, now, round(held_for, 2), moves)

    def _add(self, press, writer, now, hold, moves):
        if press.frame is not None:
            frame, origin = press.frame, press.origin
            shot = self.folder / f"shot-{len(self.captures) + 1:03d}.png"
            writer.submit(write_image, shot, frame, True)
        else:
            # A hold right after a click on the same spot: reuse that click's screenshot.
            frame, origin, shot = self.last_frame
        px, py = press.x - origin[0], press.y - origin[1]
        rect = crop_around(px, py, self.crop_size, frame.shape[1], frame.shape[0])
        x0, y0, x1, y1 = rect
        capture = Capture(x=press.x, y=press.y, button=press.button, origin=origin, rect=rect,
                          path=shot, crop=frame[y0:y1, x0:x1].copy(), time=now, start=press.start,
                          hold=hold, moves=moves, key=press.key)
        if hold and press.released:
            n = len(self.captures) + 1
            for i, (t, release_frame) in enumerate(press.released, 1):
                path = self.folder / f"release-{n:03d}-{i}.png"
                writer.submit(write_image, path, release_frame, True)
                capture.release_shots.append((path, round(t, 3)))
            capture.release_choice = press.released_choice
            capture.release_origin = press.released_origin
            ex, ey = moves[-1][1:] if moves else (0, 0)
            capture.release_point = (press.x + ex - press.released_origin[0],
                                     press.y + ey - press.released_origin[1])
        self.last_frame = (frame, origin, shot)
        self.captures.append(capture)
        if self.on_capture:
            self.on_capture(capture)


# --- Commands ----------------------------------------------------------------

def _load_with_rules(path):
    cfg = load_config(path)
    if not any(r.enabled for r in cfg.rules):
        raise ValueError("config has no enabled rules")
    return cfg


def cmd_run(args):
    cfg = _load_with_rules(args.config)
    macros = {}
    if any(r.action == "macro" for r in cfg.rules):
        import imgplay
        macros = {m.name: m for m in imgplay.load_macros(cfg, args.config)}
    watcher = Watcher(cfg, dry_run=args.dry_run, macros=macros)
    mode = " (dry run: no input will be sent)" if args.dry_run else ""
    print(f"Watching {len(watcher.rules)} rule(s){mode}. "
          f"{cfg.stop_key} stops, {cfg.pause_key} pauses, Ctrl+C also stops.")
    watcher.run()


def cmd_test(args):
    cfg = _load_with_rules(args.config)
    rules = [r for r in cfg.rules if r.enabled]
    with mss.MSS() as sct:
        frame, origin = grab(sct, capture_bounds(sct, rules))
    for rule in rules:
        try:
            hit = rule.find(frame, origin)
        except ValueError as exc:
            print(f"{rule.name}: {exc}")
            continue
        if hit is None:
            print(f"{rule.name}: region is smaller than the template")
            continue
        verdict = "MATCH" if hit.score >= rule.needed else "no match"
        how = f" ({hit.how})" if hit.how else ""
        print(f"{rule.name}: best {hit.score:.3f}{how} at ({hit.x}, {hit.y}), "
              f"needs {rule.needed:g} -> {verdict}")


def cmd_snip(args):
    if args.delay:
        print(f"Capturing in {args.delay}s, switch to the window you want...")
        time.sleep(args.delay)
    picked = select_region()
    if picked is None:
        print("Cancelled.")
        return
    crop, region = picked.crop, picked.region
    if args.out:
        write_image(args.out, crop)
        print(f"Saved {args.out} ({region[2]}x{region[3]}).")
    print(f"Region (for a rule's \"region\"): {json.dumps(region)}")


def cmd_pick(args):
    if args.delay:
        print(f"Picking in {args.delay}s, switch to the window you want...")
        time.sleep(args.delay)
    picked = select_region(point=True)
    if picked is None:
        print("Cancelled.")
        return
    rgb = tuple(int(v) for v in np.median(picked.crop.reshape(-1, 3), axis=0)[::-1])
    print(f"Colour (for a rule's \"color\"): \"{color_hex(rgb)}\"")
    print(f"Region (for a rule's \"region\"): {json.dumps(picked.region)}")


def main():
    sys.stdout.reconfigure(errors="replace")  # window titles and names may not fit the console's code page
    parser = argparse.ArgumentParser(
        description="Image-triggered macros for Windows. Run imgmacro_gui.py for the window.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="watch the screen and act on matches")
    p.add_argument("config")
    p.add_argument("--dry-run", action="store_true", help="log matches without sending input")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("test", help="capture once and print each rule's best match score")
    p.add_argument("config")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("snip", help="drag a box on screen to save a template / print a region")
    p.add_argument("out", nargs="?", help="PNG to save; omit to only print the region")
    p.add_argument("--delay", type=float, default=0, help="seconds to wait before capturing")
    p.set_defaults(func=cmd_snip)

    p = sub.add_parser("pick", help="click a pixel to print its colour and position, "
                                    "for a colour rule")
    p.add_argument("--delay", type=float, default=0, help="seconds to wait before capturing")
    p.set_defaults(func=cmd_pick)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("Stopped.")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
