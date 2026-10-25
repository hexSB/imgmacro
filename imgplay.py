"""imgplay: record the mouse and keyboard as a macro, and play it back.

A macro is a recording of every mouse move, button, wheel turn and key, with
its timing. It plays back at any speed, once, a number of times or until
stopped. While it plays, its safeguards watch the screen: when one's image
appears (a disconnect dialog, say), playback pauses, lets go of anything held,
the safeguard acts, and after a moment playback carries on where it stopped.
Checkpoints wait at a moment in the recording until an image appears, so a
slow loading screen can't throw the rest of the timing off.

    python imgplay.py record config.json "Macro name"     # F8 stops
    python imgplay.py run config.json "Macro name" [--speed 2] [--loops 3] [--dry-run]
    python imgplay.py list config.json

Recordings are kept next to the config, in macros/<name>.json. The config lists
the macros with their settings, safeguards and checkpoints, under "macros".
"""

import argparse
import ctypes
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

import mss

import imgmacro as im

EVENTS = ("move", "down", "up", "wheel", "hwheel", "kdown", "kup")


@dataclass(eq=False)
class Checkpoint:
    """Wait at a moment in the recording until something appears on screen."""
    at: float                # seconds into the recording
    rule: im.Rule            # what to wait for, and what to do when it appears
    timeout: float = 10.0    # the longest to wait
    on_fail: str = "stop"    # when it doesn't appear: "stop" (the macro fails) or "continue"


@dataclass(eq=False)
class Macro:
    name: str
    file: Path               # the recording, macros/<name>.json next to the config
    events: list = field(default_factory=list, repr=False)  # [(t, kind, *args)], t in seconds
    screen: tuple = (0, 0, 0, 0)  # the desktop (left, top, width, height) it was recorded on
    speed: float = 1.0
    loops: int = 1           # 0 = until stopped
    notes: str = ""
    guards: list = field(default_factory=list)       # safeguards: Rules checked while it plays
    resume_delay: float = 1.0  # seconds to wait after a safeguard acts, before carrying on
    checkpoints: list = field(default_factory=list)  # Checkpoints

    @property
    def duration(self):
        return self.events[-1][0] if self.events else 0.0

    def summary(self):
        """'1:05, 12 clicks, 30 keys'."""
        clicks = sum(e[1] == "down" for e in self.events)
        keys = sum(e[1] == "kdown" for e in self.events)
        parts = [clock(self.duration)]
        parts += [f"{n} {word}{'s' if n != 1 else ''}" for n, word in ((clicks, "click"), (keys, "key"))
                  if n]
        if any(e[1] in ("wheel", "hwheel") for e in self.events):
            parts.append("scrolls")
        return ", ".join(parts)

    def validate(self):
        if not 0.05 <= self.speed <= 20:
            raise ValueError(f"{self.name}: speed must be between 0.05× and 20×")
        if self.loops < 0:
            raise ValueError(f"{self.name}: loops can't be negative (0 = until stopped)")
        if not 0 <= self.resume_delay <= 600:
            raise ValueError(f"{self.name}: the wait after a safeguard must be 0 to 600 seconds")
        for g in self.guards:
            if g.action == "macro":
                raise ValueError(f"{self.name}: a safeguard can't play a macro")
        for c in self.checkpoints:
            if c.on_fail not in ("stop", "continue"):
                raise ValueError(f"{self.name}: a checkpoint's on_fail must be 'stop' or 'continue'")
            if c.at < 0 or c.timeout < 0:
                raise ValueError(f"{self.name}: checkpoint times can't be negative")
            if c.rule.action == "macro":
                raise ValueError(f"{self.name}: a checkpoint can't play a macro")


def clock(seconds):
    """65.3 -> '1:05'; 4.25 -> '4.3 s'."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


# --- files ------------------------------------------------------------------------

def check_event(e):
    """One recorded event from JSON -> a tuple, or ValueError."""
    if not isinstance(e, list) or len(e) < 2 or e[1] not in EVENTS:
        raise ValueError(f"bad event {e!r}")
    t, kind, args = float(e[0]), e[1], e[2:]
    if t < 0:
        raise ValueError(f"bad event time in {e!r}")
    if kind == "move" and len(args) == 2:
        return (t, kind, int(args[0]), int(args[1]))
    if kind in ("down", "up") and len(args) == 3 and args[0] in im.MOUSE_BUTTONS:
        return (t, kind, args[0], int(args[1]), int(args[2]))
    if kind in ("wheel", "hwheel") and len(args) == 1:
        return (t, kind, int(args[0]))
    if kind in ("kdown", "kup") and len(args) == 1:
        im.parse_key(str(args[0]))
        return (t, kind, str(args[0]))
    raise ValueError(f"bad event {e!r}")


def read_recording(path):
    """A recording file -> (events, screen)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("format") != "imgmacro-macro":
        raise ValueError(f"{path} isn't an imgmacro recording")
    events = sorted((check_event(e) for e in raw.get("events", [])), key=lambda e: e[0])
    screen = tuple(int(v) for v in raw.get("screen", (0, 0, 0, 0)))
    if len(screen) != 4:
        raise ValueError(f"{path}: screen must be [left, top, width, height]")
    return events, screen


def write_recording(macro):
    """Save the events, one per line so the file stays readable."""
    path = Path(macro.file)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = ",\n".join("    " + json.dumps([round(e[0], 4), *e[1:]]) for e in macro.events)
    path.write_text('{\n  "format": "imgmacro-macro",\n  "version": 1,\n'
                    f'  "screen": {json.dumps(list(macro.screen))},\n'
                    f'  "events": [\n{events}\n  ]\n}}\n', encoding="utf-8")


def recording_path(folder, name):
    """A new file for a recording called `name`, in the macros folder next to the config."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "macro"
    folder = Path(folder) / "macros"
    path, n = folder / f"{slug}.json", 2
    while path.exists():
        path, n = folder / f"{slug}-{n}.json", n + 1
    return path


def macro_to_dict(m, base):
    data = {"name": m.name, "file": im._relative(m.file, base), "speed": m.speed, "loops": m.loops}
    if m.notes:
        data["notes"] = m.notes
    if m.guards:
        data["guards"] = [im.rule_to_dict(g, base) for g in m.guards]
        data["resume_delay"] = m.resume_delay
    if m.checkpoints:
        data["checkpoints"] = [{"at": round(c.at, 3), "timeout": c.timeout, "on_fail": c.on_fail,
                                "rule": im.rule_to_dict(c.rule, base)} for c in m.checkpoints]
    return data


def macro_from_dict(d, base):
    name = d.get("name", "Macro")
    if "file" not in d:
        raise ValueError(f"{name}: missing 'file' (the recording)")
    path = Path(d["file"])
    if not path.is_absolute():
        path = Path(base) / path
    events, screen = read_recording(path)
    macro = Macro(name=name, file=path, events=events, screen=screen,
                  speed=float(d.get("speed", 1.0)), loops=int(d.get("loops", 1)),
                  notes=str(d.get("notes", "")),
                  guards=[im.rule_from_dict(g, base, i) for i, g in enumerate(d.get("guards", []), 1)],
                  resume_delay=float(d.get("resume_delay", 1.0)),
                  checkpoints=[Checkpoint(at=float(c.get("at", 0)), timeout=float(c.get("timeout", 10)),
                                          on_fail=c.get("on_fail", "stop"),
                                          rule=im.rule_from_dict(c["rule"], base))
                               for c in d.get("checkpoints", [])])
    macro.validate()
    return macro


def load_macros(cfg, config_path, problems=None):
    """The config's macros. With a `problems` list, a macro that won't load (say its
    recording was moved) is left out and added there as (its JSON, the error),
    rather than failing them all."""
    base = Path(config_path).parent
    macros = []
    for d in cfg.macros:
        try:
            macros.append(macro_from_dict(d, base))
        except (ValueError, OSError, KeyError) as exc:
            if problems is None:
                raise
            problems.append((d, exc))
    return macros


def store_macros(cfg, macros, config_path, keep=()):
    """Put `macros` into `cfg` as JSON, ready for im.save_config(cfg, config_path).
    `keep` is JSON of macros that didn't load, saved back unchanged."""
    base = Path(config_path).parent
    cfg.macros = [macro_to_dict(m, base) for m in macros] + list(keep)


# --- recording --------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # our own, so argtypes don't clash
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.CallNextHookEx.restype = LRESULT
_user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
_user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


WH_MOUSE_LL, WM_QUIT, WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 14, 0x0012, 0x020A, 0x020E


class WheelHook:
    """Count mouse-wheel turns while recording. Buttons and keys can be polled, the
    wheel can't, so this installs a low-level mouse hook, which only reads the wheel
    and passes every event on unchanged. It's removed when recording stops."""

    def __init__(self):
        self.turns = []  # [(monotonic time, delta, horizontal, x, y)]
        self.thread_id = None
        self.ok = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.proc = HOOKPROC(self.on_mouse)  # keep a reference: Windows calls it

    def start(self):
        self.thread.start()
        return self.ok.wait(1.0)

    def stop(self):
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            self.thread.join(1.0)

    def on_mouse(self, code, wparam, lparam):
        if code >= 0 and wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not info.flags & 1:  # LLMHF_INJECTED: not ours, or another macro's
                delta = ctypes.c_short(info.mouseData >> 16).value
                self.turns.append((time.monotonic(), delta, wparam == WM_MOUSEHWHEEL,
                                   info.pt.x, info.pt.y))
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def run(self):
        self.thread_id = _kernel32.GetCurrentThreadId()
        hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self.proc, _kernel32.GetModuleHandleW(None), 0)
        if not hook:
            return
        self.ok.set()
        msg = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            _user32.UnhookWindowsHookEx(hook)


class MacroRecorder:
    """Record the mouse and keyboard with their timing.

    Buttons, keys and the mouse position are polled every couple of milliseconds,
    the same way the hotkeys work; the wheel comes from a WheelHook. Clicks on our
    own windows and the taskbar, keys typed into our own windows, and the hotkeys
    aren't recorded. Ctrl, shift and alt are recorded as keys of their own, so a
    combo plays back as it was pressed.

    The recording starts at the first click or key, with the mouse where it was
    just before; the stop key (or `also_stop`) ends it. Callbacks fire on the
    recorder thread: `on_event(kind)` for each click or key, `on_stop()` at the end.
    `events` and `screen` hold the result.
    """

    POLL = 0.002
    MOVE_GAP = 0.008   # seconds between recorded mouse positions
    BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04}

    def __init__(self, stop_key="F8", ignore_keys=(), also_stop=(), on_event=None, on_stop=None,
                 on_log=print):
        self.stop_vks = {im.parse_key(k) for k in (stop_key, *also_stop) if k}
        skip = {*self.stop_vks, *(im.parse_key(k) for k in ignore_keys if k)}
        self.key_vks = [vk for vk in im.VK_TO_NAME if vk not in skip]
        self.on_event, self.on_stop, self.on_log = on_event, on_stop, on_log
        self.events = []
        self.screen = im.virtual_screen()
        self.clicks = self.keys = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        own_pid = os.getpid()
        raw = []  # [(monotonic time, kind, *args)]
        hook = WheelHook()
        if not hook.start():
            self.on_log("The mouse wheel can't be recorded here; everything else is.")
        was = {b: im.key_down(vk) for b, vk in self.BUTTONS.items()}
        key_was = {vk: im.key_down(vk) for vk in self.key_vks}
        ignored = set()  # buttons pressed on our own windows: their release isn't recorded either
        ignored_keys = set()  # the same for keys typed into our own windows
        last_pos, last_move = None, 0.0
        try:
            while not self.stop_event.is_set():
                if any(im.key_down(vk) for vk in self.stop_vks):
                    break
                now = time.monotonic()
                pos = im.cursor_pos()
                if pos != last_pos and now - last_move >= self.MOVE_GAP:
                    raw.append((now, "move", *pos))
                    last_pos, last_move = pos, now
                for button, vk in self.BUTTONS.items():
                    down = im.key_down(vk)
                    if down != was[button]:
                        owner = im.window_at(*pos)
                        if down and owner and (owner[0] == own_pid
                                               or owner[1] in im.IGNORED_WINDOW_CLASSES):
                            ignored.add(button)
                        elif not down and button in ignored:
                            ignored.discard(button)
                        else:
                            raw.append((now, "down" if down else "up", button, *pos))
                            if down:
                                self.clicks += 1
                                if self.on_event:
                                    self.on_event("click")
                        was[button] = down
                typing_here = im.foreground_pid() == own_pid  # e.g. naming the macro
                for vk in self.key_vks:
                    down = im.key_down(vk)
                    if down != key_was[vk]:
                        key_was[vk] = down
                        if down and typing_here:
                            ignored_keys.add(vk)
                            continue
                        if not down and vk in ignored_keys:
                            ignored_keys.discard(vk)
                            continue
                        raw.append((now, "kdown" if down else "kup", im.VK_TO_NAME[vk]))
                        if down:
                            self.keys += 1
                            if self.on_event:
                                self.on_event("key")
                time.sleep(self.POLL)
            end = time.monotonic()
            for button, down in was.items():  # still held when recording stopped
                if down and button not in ignored:
                    raw.append((end, "up", button, *im.cursor_pos()))
            for vk, down in key_was.items():
                if down and vk not in ignored_keys:
                    raw.append((end, "kup", im.VK_TO_NAME[vk]))
        except Exception as exc:
            self.on_log(f"error while recording: {exc}")
        finally:
            hook.stop()
            own = {os.getpid()}
            for t, delta, horizontal, x, y in hook.turns:
                owner = im.window_at(x, y)
                if not (owner and (owner[0] in own or owner[1] in im.IGNORED_WINDOW_CLASSES)):
                    raw.append((t, "hwheel" if horizontal else "wheel", delta))
            self.events = trim(sorted(raw, key=lambda e: e[0]))
            self.stop_event.set()
            if self.on_stop:
                self.on_stop()


def trim(raw):
    """Start the recording at its first click, key or wheel turn, with the mouse
    where it was just before it, and count time from there."""
    first = next((i for i, e in enumerate(raw) if e[1] != "move"), None)
    if first is None:
        start = 0
    else:
        moves = [i for i in range(first) if raw[i][1] == "move"]
        start = moves[-1] if moves else first
    raw = raw[start:]
    if not raw:
        return []
    t0 = raw[0][0]
    return [(round(e[0] - t0, 4), *e[1:]) for e in raw]


# --- playback ---------------------------------------------------------------------

class _Guards(threading.Thread):
    """Watch the screen for a macro's safeguards while it plays. The player asks
    `pending()` between events; while it handles a hit, this waits."""

    def __init__(self, rules, interval, on_log, paused=lambda: False):
        super().__init__(daemon=True)
        self.paused = paused
        self.rules = [r for r in rules if r.enabled]
        self.interval, self.on_log = interval, on_log
        self.hit = None  # (rule, Hit) until the player is done with it
        self.broken = set()
        self.done = threading.Event()

    def pending(self):
        return self.hit

    def handled(self):
        self.hit = None

    def run(self):
        try:
            with mss.MSS() as sct:
                bounds = im.capture_bounds(sct, self.rules)
                while not self.done.is_set():
                    tick = time.perf_counter()
                    if self.hit is None and not self.paused():
                        frame, origin = im.grab(sct, bounds)
                        for rule in self.rules:
                            if rule in self.broken or time.monotonic() - rule.last_fired < rule.cooldown:
                                continue
                            try:
                                hit = rule.find(frame, origin)
                            except ValueError as exc:
                                self.broken.add(rule)
                                self.on_log(f"safeguard {rule.name}: {exc}. Skipping it.")
                                continue
                            if hit is not None and hit.score >= rule.needed:
                                self.hit = (rule, hit)
                                break
                    self.done.wait(max(self.interval - (time.perf_counter() - tick), 0.01))
        except Exception as exc:
            self.on_log(f"safeguards stopped: {exc}")


class _Player:
    """One play of a macro (all its loops). See play()."""

    def __init__(self, macro, cfg, stopping, paused, on_log, dry_run, speed, on_hit):
        self.macro, self.cfg = macro, cfg
        self._stopping, self.paused = stopping, paused
        self.on_log, self.dry_run, self.on_hit = on_log, dry_run, on_hit
        self.speed = speed
        self.stopped = False
        self.shift = 0.0       # seconds the rest of the recording is pushed back by (pauses, safeguards, checkpoints)
        self.buttons = {}      # held mouse buttons -> where they went down
        self.keys = []         # held keys (virtual-key codes), in the order pressed
        self.guards = None

    def log(self, text):
        self.on_log(f"{time.strftime('%H:%M:%S')}  {self.macro.name}: {text}")

    def stopping(self):
        if not self.stopped and self._stopping():
            self.stopped = True
        return self.stopped

    def run(self, loops):
        if self.macro.screen[2:] != (0, 0) and im.virtual_screen() != tuple(self.macro.screen):
            w, h = self.macro.screen[2:]
            nw, nh = im.virtual_screen()[2:]
            self.log(f"recorded on a {w}×{h} desktop, this one is {nw}×{nh}: clicks may land "
                     "in the wrong place")
        if self.macro.guards:
            self.guards = _Guards(self.macro.guards, self.cfg.interval, self.on_log, self.paused)
            self.guards.start()
        try:
            n = 0
            while loops == 0 or n < loops:
                n += 1
                if loops != 1:
                    self.log(f"run {n}" + (f" of {loops}" if loops else ""))
                outcome = self.play_once()
                if outcome != "finished":
                    return outcome
            return "finished"
        finally:
            try:
                self.release()
            finally:
                if self.guards:
                    self.guards.done.set()
                    self.guards.join(2)

    def play_once(self):
        start, self.shift = time.perf_counter(), 0.0
        checkpoints = sorted(self.macro.checkpoints, key=lambda c: c.at)
        events = self.macro.events
        i = 0
        for cp in checkpoints:
            while i < len(events) and events[i][0] < cp.at:
                if not self.wait_until(start + events[i][0] / self.speed):
                    return "stopped"
                self.send(events[i])
                i += 1
            if not self.wait_until(start + cp.at / self.speed):
                return "stopped"
            outcome = self.checkpoint(cp)
            if outcome != "ok":
                return outcome
        for event in events[i:]:
            if not self.wait_until(start + event[0] / self.speed):
                return "stopped"
            self.send(event)
        return "finished"

    def wait_until(self, due):
        """Wait for a moment in the recording (perf_counter time, before any pauses),
        handling pauses and safeguards meanwhile. -> False if stopped."""
        while True:
            if self.stopping():
                return False
            if self.paused():
                self.suspend(self.wait_out_pause)
                continue
            if self.guards and self.guards.pending():
                self.safeguard(*self.guards.pending())
                continue
            left = due + self.shift - time.perf_counter()
            if left <= 0:
                return True
            time.sleep(min(left, 0.002))

    def wait_out_pause(self):
        while self.paused() and not self.stopping():
            time.sleep(0.02)
        if self.guards:  # anything seen while paused may be long gone (closed by hand)
            self.guards.handled()

    def suspend(self, during):
        """Let go of everything held, run `during()`, press it all again, and push the
        rest of the recording back by however long that took."""
        t0 = time.perf_counter()
        held = self.release()
        try:
            during()
        finally:
            if not self.stopping():
                self.press(held)
            self.shift += time.perf_counter() - t0

    def safeguard(self, rule, hit):
        what = im.action_words(rule)
        self.log(f"safeguard “{rule.name}” ({hit.score:.3f}) at ({hit.x}, {hit.y}): pausing to {what}")
        if self.on_hit:
            self.on_hit(rule, hit)

        def act():
            if self.dry_run:
                self.log(f"would {what}")
            else:
                note = rule.fire(hit.x, hit.y, cancel=self.stopping)
                if note:
                    self.log(f"safeguard “{rule.name}”: {note}")
            end = time.perf_counter() + self.macro.resume_delay
            while time.perf_counter() < end and not self.stopping():
                time.sleep(0.01)
        try:
            self.suspend(act)
        finally:
            rule.last_fired = time.monotonic()
            rule.fired += 1
            self.guards.handled()
        if not self.stopped:
            self.log("carrying on")

    def checkpoint(self, cp):
        """Wait for a checkpoint's image (acting on it if its rule says so).
        -> "ok", "failed" or "stopped"."""
        rule = cp.rule
        t0, shift = time.perf_counter(), self.shift
        self.log(f"checkpoint at {cp.at:g} s: waiting up to {cp.timeout:g} s for {rule.describe_target()}")
        best = 0.0
        try:
            with mss.MSS() as sct:
                bounds = im.capture_bounds(sct, [rule])
                while True:
                    if self.stopping():
                        return "stopped"
                    if self.paused():
                        self.suspend(self.wait_out_pause)
                        continue
                    if self.guards and self.guards.pending():
                        self.safeguard(*self.guards.pending())
                        continue
                    tick = time.perf_counter()
                    try:
                        hit = rule.find(*im.grab(sct, bounds))
                    except ValueError as exc:
                        self.log(f"checkpoint: {exc}")
                        return "failed"
                    if hit is not None:
                        best = max(best, hit.score)
                        if hit.score >= rule.needed:
                            self.log(f"checkpoint: found ({hit.score:.3f}) at ({hit.x}, {hit.y})")
                            if self.on_hit:
                                self.on_hit(rule, hit)
                            if rule.action != "none":
                                if self.dry_run:
                                    self.log(f"would {im.action_words(rule)}")
                                else:
                                    note = rule.fire(hit.x, hit.y, cancel=self.stopping)
                                    if note:
                                        self.log(f"checkpoint: {note}")
                            return "ok"
                    # The deadline moves back by any pause or safeguard while waiting.
                    if tick >= t0 + cp.timeout + (self.shift - shift):
                        if cp.on_fail == "continue":
                            self.log(f"checkpoint: not found (best {best:.3f}), carrying on anyway")
                            return "ok"
                        self.log(f"checkpoint: not found in {cp.timeout:g} s (best {best:.3f}), "
                                 "stopping the macro")
                        return "failed"
                    end = tick + self.cfg.interval
                    while time.perf_counter() < end and not self.stopping():
                        time.sleep(0.005)
        finally:
            # Waiting here pushes the rest of the recording back by however long it took.
            self.shift = shift + (time.perf_counter() - t0)

    def send(self, event):
        _, kind, *args = event
        if kind == "down":
            self.buttons[args[0]] = (args[1], args[2])
        elif kind == "up":
            self.buttons.pop(args[0], None)
        elif kind in ("kdown", "kup"):
            vk = im.parse_key(args[0])
            if vk in self.keys:
                self.keys.remove(vk)
            if kind == "kdown":
                self.keys.append(vk)
        if self.dry_run:
            return
        if kind == "move":
            im.move_to(*args)
        elif kind in ("down", "up"):
            im.move_to(args[1], args[2])
            im.button_event(args[0], up=kind == "up")
        elif kind in ("wheel", "hwheel"):
            im.wheel(args[0], horizontal=kind == "hwheel")
        else:
            im.key_event(im.parse_key(args[0]), up=kind == "kup")

    def release(self):
        """Let go of every held button and key. -> what was held, for press()."""
        held = (dict(self.buttons), list(self.keys))
        self.buttons, self.keys = {}, []
        if not self.dry_run:
            # Each on its own: if Windows refuses one (the PC locked, a UAC prompt),
            # still try to let go of the rest.
            ups = [lambda vk=vk: im.key_event(vk, up=True) for vk in reversed(held[1])]
            ups += [lambda b=b: im.button_event(b, up=True) for b in held[0]]
            failed = None
            for up in ups:
                try:
                    up()
                except OSError as exc:
                    failed = exc
            if failed is not None:
                raise failed
        return held

    def press(self, held):
        """Press again what release() let go of, so a held key or drag carries on."""
        buttons, keys = held
        if not self.dry_run:
            back = im.cursor_pos()
            for button, (x, y) in buttons.items():
                im.move_to(x, y)
                im.button_event(button, up=False)
            if buttons:
                im.move_to(*back)
            for vk in keys:
                im.key_event(vk, up=False)
        self.buttons, self.keys = dict(buttons), list(keys)


def play(macro, cfg, stopping, paused=lambda: False, on_log=print, dry_run=False,
         speed=None, loops=None, on_hit=None):
    """Play `macro` on this thread, `loops` times at `speed` (default: the macro's own).

    `stopping()` is called often and returns True to stop (it may also notice the
    pause key); `paused()` says whether to wait. Everything held is released at the
    end, even on an error. -> "finished", "failed" (a checkpoint didn't appear) or
    "stopped".
    """
    speed = macro.speed if speed is None else speed
    loops = macro.loops if loops is None else loops
    if not macro.events and not macro.checkpoints:
        on_log(f"{macro.name}: the recording is empty")
        return "finished"
    if dry_run:
        on_log(f"{time.strftime('%H:%M:%S')}  {macro.name}: dry run, playing "
               f"{len(macro.events)} events at {speed:g}× without sending them")
    return _Player(macro, cfg, stopping, paused, on_log, dry_run, speed, on_hit).run(loops)


class MacroRunner(im.Control):
    """Play a macro on a worker thread, like FlowRunner does for flows. Callbacks fire
    on the runner thread: `on_log(text)`, `on_pause(paused)`, `on_hit(rule, hit)`."""

    def __init__(self, macro, cfg, dry_run=False, speed=None, loops=None, on_log=print,
                 on_pause=None, on_hit=None):
        super().__init__(cfg, on_log, on_pause)
        self.macro, self.dry_run = macro, dry_run
        self.speed = macro.speed if speed is None else speed
        self.loops = macro.loops if loops is None else loops
        self.on_hit = on_hit

    def run(self):
        im.set_humanize(self.cfg.humanize_px, self.cfg.humanize_ms)
        try:
            self.outcome = play(self.macro, self.cfg, self.stopping, lambda: self.paused,
                                self.on_log, self.dry_run, self.speed, self.loops, self.on_hit)
            if self.outcome != "stopped":
                self.on_log(f"{time.strftime('%H:%M:%S')}  {self.macro.name}: {self.outcome}.")
        except Exception as exc:  # report, rather than silently kill the GUI's worker thread
            self.outcome = "error"
            self.on_log(f"error: {exc}")
        finally:
            self.on_log("Stopped.")


# --- command line -----------------------------------------------------------------

def main(argv=None):
    sys.stdout.reconfigure(errors="replace")  # window titles and names may not fit the console's code page
    parser = argparse.ArgumentParser(prog="imgplay", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("record", help="record a macro into the config (F8 stops)")
    p.add_argument("config")
    p.add_argument("name")
    p.add_argument("--delay", type=float, default=3, help="seconds before recording starts")
    p = sub.add_parser("run", help="play a macro (F8 stops, F7 pauses)")
    p.add_argument("config")
    p.add_argument("name")
    p.add_argument("--speed", type=float, help="playback speed, e.g. 2 for twice as fast")
    p.add_argument("--loops", type=int, help="times to play it; 0 = until stopped")
    p.add_argument("--dry-run", action="store_true", help="time it, but send no input")
    p = sub.add_parser("list", help="list the config's macros")
    p.add_argument("config")
    args = parser.parse_args(argv)
    try:
        cfg = im.load_config(args.config)
        macros = load_macros(cfg, args.config)
        by_name = {m.name: m for m in macros}
        if args.command == "list":
            for m in macros:
                print(f"{m.name}: {m.summary()}, {m.speed:g}×, "
                      + ("until stopped" if m.loops == 0 else f"{m.loops}×"))
            return 0
        if args.command == "record":
            if args.name in by_name:
                raise ValueError(f"there's already a macro called {args.name!r}")
            return record_cli(cfg, macros, args)
        if args.name not in by_name:
            raise ValueError(f"no macro named {args.name!r} (have: {', '.join(by_name) or 'none'})")
    except (ValueError, OSError, KeyError) as exc:
        print(f"imgplay: {exc}", file=sys.stderr)
        return 2
    macro = by_name[args.name]
    print(f"Playing {macro.name!r} ({macro.summary()}). {cfg.stop_key} stops, {cfg.pause_key} pauses.")
    runner = MacroRunner(macro, cfg, dry_run=args.dry_run, speed=args.speed, loops=args.loops)
    runner.run()
    return 0 if runner.outcome == "finished" else 1


def record_cli(cfg, macros, args):
    print(f"Recording starts in {args.delay:g} s: switch to the game. {cfg.stop_key} stops.")
    time.sleep(args.delay)
    recorder = MacroRecorder(stop_key=cfg.stop_key, ignore_keys=(cfg.start_key, cfg.pause_key))
    print("Recording...")
    recorder.run()
    if not recorder.events:
        print("Nothing was recorded.")
        return 1
    folder = Path(args.config).parent
    macro = Macro(args.name, recording_path(folder, args.name), recorder.events, recorder.screen)
    write_recording(macro)
    macros.append(macro)
    store_macros(cfg, macros, args.config)
    im.save_config(cfg, args.config)
    print(f"Saved {macro.name!r} ({macro.summary()}) to {macro.file}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
