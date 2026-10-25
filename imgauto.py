"""imgauto: running unattended. Anti-AFK, schedules and the run history.

- Anti-AFK presses a key (a jump, by default) in a game window every few
  minutes, so the game doesn't kick you for being idle: it switches to the
  window, presses the key and switches straight back to what you were using.
- Schedules start a flow or a macro every N minutes, or at a time of day.
- The run history keeps how often each flow, macro and set of rules ran, for
  how long and how it ended, in <config>.stats.json next to the config.

Schedules and Anti-AFK run while the window (or this command line) is open.

    python imgauto.py windows                              # list windows to pick from
    python imgauto.py antiafk "Roblox" [--every 15] [--key space]
    python imgauto.py schedule config.json                 # run the config's schedules
"""

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import imgmacro as im

# --- windows ----------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # our own, so argtypes don't clash
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
_user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.IsWindowVisible.argtypes = (wintypes.HWND,)
_user32.IsWindow.argtypes = (wintypes.HWND,)
_user32.IsIconic.argtypes = (wintypes.HWND,)
_user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.BringWindowToTop.argtypes = (wintypes.HWND,)
_user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_dwmapi = ctypes.WinDLL("dwmapi")
_dwmapi.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    exe: str  # e.g. "RobloxPlayerBeta.exe"

    def label(self):
        return f"{self.title}  ({self.exe}, pid {self.pid})"


def _exe(pid):
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return "?"
    try:
        size = wintypes.DWORD(260)
        name = ctypes.create_unicode_buffer(260)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return Path(name.value).name
        return "?"
    finally:
        _kernel32.CloseHandle(handle)


def window_pid(hwnd):
    pid = wintypes.DWORD()
    tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, tid


def _shown(hwnd):
    """True for a window you can actually see on the taskbar: not hidden, not a
    tool window, not cloaked (a suspended Store app), not zero-sized."""
    if not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, 4):  # GW_OWNER: skip dialogs
        return False
    ex = _user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    if ex & 0x80 and not ex & 0x40000:  # WS_EX_TOOLWINDOW without WS_EX_APPWINDOW
        return False
    cloaked = wintypes.DWORD()
    if not _dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) and cloaked.value:
        return False
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left > 1 and rect.bottom - rect.top > 1


def list_windows():
    """Visible, titled top-level windows of other programs, for picking a game."""
    own = os.getpid()
    found = []

    def visit(hwnd, _):
        if not _shown(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, title, length + 1)
        pid, _ = window_pid(hwnd)
        if pid != own and title.value not in ("Program Manager", "Windows Input Experience"):
            found.append(Window(int(hwnd), title.value, pid, _exe(pid)))
        return True
    _user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


def window_alive(window):
    """True if `window` is still open and still the same program (handles get reused)."""
    return bool(window and _user32.IsWindow(window.hwnd) and window_pid(window.hwnd)[0] == window.pid)


def foreground():
    return _user32.GetForegroundWindow()


def focus(hwnd):
    """Bring a window to the front and give it the keyboard. -> True if it worked.

    Windows only lets the program you're using change the foreground window, so
    this briefly joins that program's input queue to be allowed to.
    """
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if _user32.SetForegroundWindow(hwnd) and foreground() == hwnd:
        return True
    current = foreground()
    mine = _kernel32.GetCurrentThreadId()
    theirs = window_pid(current)[1] if current else 0
    attached = bool(theirs and theirs != mine and _user32.AttachThreadInput(mine, theirs, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(mine, theirs, False)
    for _ in range(20):  # the switch can take a moment
        if foreground() == hwnd:
            return True
        time.sleep(0.01)
    return False


# --- Anti-AFK ---------------------------------------------------------------------

class AntiAfk:
    """Press a key in a game window every few minutes, from a background thread.

    Each time: check the window is still open, remember the window you're using,
    switch to the game, press the key, and switch back. If Windows won't switch
    to the game, nothing is pressed (so the key can't land in the wrong window).
    `blocked()` returning True (e.g. while a macro runs) puts it off for a moment.
    The first press is right away when it's switched on.
    """

    def __init__(self, on_log=print, blocked=lambda: False):
        self.on_log, self.blocked = on_log, blocked
        self.window = None
        self.every = 15.0  # minutes
        self.key = "space"
        self.enabled = False
        self.next_at = None  # time.monotonic() of the next press
        self.status = "Off"
        self.presses = 0
        self.cond = threading.Condition()
        self.closed = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def configure(self, window=None, every=None, key=None, enabled=None):
        """Change any of the settings. Switching on, or changing how often, restarts the countdown."""
        with self.cond:
            if key is not None:
                im.parse_combo(key)
                self.key = key
            if window is not None:
                self.window = window
            if every is not None:
                if not 1 <= every <= 120:
                    raise ValueError("Anti-AFK presses every 1 to 120 minutes")
                restart = every != self.every
                self.every = float(every)
                if restart and self.enabled:
                    self.next_at = time.monotonic() + self.every * 60
            if enabled is not None and enabled != self.enabled:
                if enabled and self.window is None:
                    raise ValueError("pick the game's window first")
                self.enabled = enabled
                self.next_at = time.monotonic() if enabled else None
                self.status = "Starting" if enabled else "Off"
            self.cond.notify()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify()

    def seconds_left(self):
        return None if self.next_at is None else max(self.next_at - time.monotonic(), 0)

    def run(self):
        while True:
            with self.cond:
                while not self.closed and (not self.enabled or time.monotonic() < self.next_at):
                    wait = None if not self.enabled else self.next_at - time.monotonic()
                    self.cond.wait(wait if wait is None else max(wait, 0.05))
                if self.closed:
                    return
                window, key = self.window, self.key
            if self.blocked():
                with self.cond:
                    self.next_at = time.monotonic() + 5
                    self.status = "Waiting: something is running (or dry run is on)"
                continue
            result = self.fire(window, key)
            with self.cond:
                if self.enabled:
                    self.next_at = time.monotonic() + self.every * 60
                self.status = result

    def fire(self, window, key):
        """One press. -> a status line."""
        stamp = time.strftime("%H:%M:%S")
        if not window_alive(window):
            self.on_log(f"{stamp}  Anti-AFK: the window “{window.title}” has closed; pick it again")
            return "Paused: the window has closed"
        previous = foreground()
        try:
            if previous != window.hwnd and not focus(window.hwnd):
                self.on_log(f"{stamp}  Anti-AFK: Windows wouldn't switch to “{window.title}”; "
                            "didn't press anything")
                return f"Couldn't switch to the game at {stamp}"
            time.sleep(0.05)  # let the game notice it has focus before the key arrives
            im.press(im.parse_combo(key), hold=0.08)
            self.presses += 1
            self.on_log(f"{stamp}  Anti-AFK: pressed {key} in “{window.title}”")
            return f"Pressed {key} at {stamp}"
        finally:
            if previous and previous != window.hwnd and _user32.IsWindow(previous):
                time.sleep(0.05)
                focus(previous)


# --- schedules --------------------------------------------------------------------

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(eq=False)
class Schedule:
    what: str = "flow"     # "flow" or "macro"
    target: str = ""       # its name
    kind: str = "every"    # "every": every `every` minutes; "daily": at `at` on `days`
    every: float = 60.0    # minutes
    at: str = "09:00"      # HH:MM, 24-hour
    days: tuple = ()       # weekdays it runs on, 0 = Monday; empty = every day
    enabled: bool = True

    def validate(self):
        if self.what not in ("flow", "macro"):
            raise ValueError("a schedule runs a flow or a macro")
        if not self.target:
            raise ValueError(f"pick the {self.what} to run")
        if self.kind == "every":
            if not 1 <= self.every <= 7 * 24 * 60:
                raise ValueError("a schedule repeats every 1 minute to 7 days")
        elif self.kind == "daily":
            parse_time(self.at)
            if any(not 0 <= d <= 6 for d in self.days):
                raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        else:
            raise ValueError("a schedule's kind is 'every' or 'daily'")

    def describe(self):
        if self.kind == "every":
            n = self.every
            when = (f"every {n:g} min" if n < 60 else
                    f"every {n / 60:g} hour{'s' if n != 60 else ''}")
        else:
            days = "" if not self.days or len(self.days) == 7 else \
                " on " + ", ".join(DAYS[d] for d in sorted(self.days))
            when = f"daily at {self.at}{days}"
        return f"{self.what} “{self.target}”, {when}"


def parse_time(text):
    """'09:30' -> (9, 30)."""
    try:
        hours, minutes = (int(part) for part in text.strip().split(":"))
    except ValueError:
        raise ValueError(f"times look like 09:30, not {text!r}") from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"{text!r} isn't a time of day")
    return hours, minutes


def next_run(schedule, after):
    """When `schedule` should next run, after the datetime `after`."""
    if schedule.kind == "every":
        return after + timedelta(minutes=schedule.every)
    hours, minutes = parse_time(schedule.at)
    when = after.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if when <= after:
        when += timedelta(days=1)
    for _ in range(7):
        if not schedule.days or when.weekday() in schedule.days:
            return when
        when += timedelta(days=1)
    return when


def schedule_to_dict(s):
    data = {"what": s.what, "target": s.target, "kind": s.kind, "enabled": s.enabled}
    if s.kind == "every":
        data["every"] = s.every
    else:
        data["at"] = s.at
        if s.days:
            data["days"] = sorted(s.days)
    return data


def schedule_from_dict(d):
    s = Schedule(what=d.get("what", "flow"), target=str(d.get("target", "")),
                 kind=d.get("kind", "every"), every=float(d.get("every", 60)),
                 at=str(d.get("at", "09:00")), days=tuple(int(x) for x in d.get("days", ())),
                 enabled=bool(d.get("enabled", True)))
    s.validate()
    return s


def load_schedules(cfg):
    return [schedule_from_dict(d) for d in cfg.schedules]


def store_schedules(cfg, schedules):
    cfg.schedules = [schedule_to_dict(s) for s in schedules]


class Scheduler:
    """Keeps track of when each schedule next runs. Counting starts when it's made
    (or a schedule is added), so a schedule set to every 60 minutes first runs an
    hour after the window opens; a missed run isn't made up later."""

    def __init__(self, schedules=()):
        self.due = {}  # schedule -> datetime
        self.update(schedules)

    def update(self, schedules, now=None):
        """Track `schedules`, keeping the countdowns of ones already tracked."""
        now = now or datetime.now()
        self.due = {s: self.due.get(s) or next_run(s, now) for s in schedules}

    def reset(self, schedule, now=None):
        self.due[schedule] = next_run(schedule, now or datetime.now())

    def take_due(self, now=None):
        """The switched-on schedules whose time has come; their next run is set."""
        now = now or datetime.now()
        ready = [s for s, when in self.due.items() if s.enabled and when <= now]
        for s in ready:
            self.due[s] = next_run(s, now)
        return ready


# --- run history ------------------------------------------------------------------

def stats_path(config_path):
    """sae.json -> sae.stats.json, next to it."""
    path = Path(config_path)
    return path.with_name(path.stem + ".stats.json")


class Stats:
    """How often each flow, macro and set of rules has run, and the latest runs.
    Kept in its own file, so the config isn't changed by running things."""

    HISTORY = 200

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.totals = {}   # "flow:Name" -> {"runs", "seconds", "first", "last"}
        self.history = []  # newest last: {"what", "name", "at", "seconds", "outcome"}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.totals = dict(raw.get("totals", {}))
                self.history = list(raw.get("history", []))[-self.HISTORY:]
            except (ValueError, OSError):
                pass  # an unreadable history isn't worth stopping for; it starts again

    def record(self, what, name, seconds, outcome):
        """Note one run: `what` is "rules", "flow" or "macro"."""
        now = time.time()
        with self.lock:
            key = f"{what}:{name}"
            t = self.totals.setdefault(key, {"runs": 0, "seconds": 0.0, "first": now, "last": now})
            t["runs"] += 1
            t["seconds"] = round(t["seconds"] + seconds, 1)
            t["last"] = now
            t[outcome] = t.get(outcome, 0) + 1
            self.history.append({"what": what, "name": name, "at": round(now), "seconds": round(seconds, 1),
                                 "outcome": outcome})
            self.history = self.history[-self.HISTORY:]
            self.save()

    def total(self, what, name):
        return self.totals.get(f"{what}:{name}", {})

    def clear(self):
        with self.lock:
            self.totals, self.history = {}, []
            self.save()

    def save(self):
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"totals": self.totals, "history": self.history}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)


# --- command line -----------------------------------------------------------------

def cmd_windows(args):
    for w in list_windows():
        print(w.label())


def cmd_antiafk(args):
    matches = [w for w in list_windows() if args.window.lower() in w.title.lower()
               or args.window.lower() == w.exe.lower()]
    if not matches:
        raise ValueError(f"no window has {args.window!r} in its title (try: imgauto.py windows)")
    if len(matches) > 1:
        names = "\n  ".join(w.label() for w in matches)
        raise ValueError(f"{len(matches)} windows match {args.window!r}; be more specific:\n  {names}")
    afk = AntiAfk()
    afk.configure(window=matches[0], every=args.every, key=args.key, enabled=True)
    print(f"Anti-AFK: pressing {args.key} in “{matches[0].title}” every {args.every:g} min. Ctrl+C stops.")
    while True:
        time.sleep(1)


def cmd_schedule(args):
    import imgflow
    import imgplay
    cfg = im.load_config(args.config)
    schedules = [s for s in load_schedules(cfg) if s.enabled]
    if not schedules:
        raise ValueError("the config has no switched-on schedules")
    flows = {f.name: f for f in imgflow.load_flows(cfg, args.config)}
    macros = {m.name: m for m in imgplay.load_macros(cfg, args.config)}
    stats = Stats(stats_path(args.config))
    scheduler = Scheduler(schedules)
    for s in schedules:
        print(f"{s.describe()}: next at {scheduler.due[s]:%a %H:%M}")
    print("Waiting. The stop key stops a run; Ctrl+C stops the schedules.")
    while True:
        for s in scheduler.take_due():
            target = (flows if s.what == "flow" else macros).get(s.target)
            if target is None:
                print(f"{time.strftime('%H:%M:%S')}  schedule: there's no {s.what} called “{s.target}”")
                continue
            print(f"{time.strftime('%H:%M:%S')}  schedule: running {s.what} “{s.target}”")
            runner = (imgflow.FlowRunner(target, cfg, flows=flows.values(), macros=macros)
                      if s.what == "flow" else imgplay.MacroRunner(target, cfg))
            start = time.monotonic()
            runner.run()
            stats.record(s.what, s.target, time.monotonic() - start, runner.outcome or "stopped")
            print(f"{s.describe()}: next at {scheduler.due[s]:%a %H:%M}")
        time.sleep(1)


def main(argv=None):
    sys.stdout.reconfigure(errors="replace")  # window titles and names may not fit the console's code page
    parser = argparse.ArgumentParser(prog="imgauto", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("windows", help="list the windows Anti-AFK can pick")
    p.set_defaults(func=cmd_windows)
    p = sub.add_parser("antiafk", help="press a key in a game window every few minutes")
    p.add_argument("window", help="part of the window's title, or its program, e.g. RobloxPlayerBeta.exe")
    p.add_argument("--every", type=float, default=15, help="minutes between presses (default 15)")
    p.add_argument("--key", default="space", help="the key or combo to press (default space)")
    p.set_defaults(func=cmd_antiafk)
    p = sub.add_parser("schedule", help="run the config's schedules until Ctrl+C")
    p.add_argument("config")
    p.set_defaults(func=cmd_schedule)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("Stopped.")
    except (ValueError, OSError) as exc:
        print(f"imgauto: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
