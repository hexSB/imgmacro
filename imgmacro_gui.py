"""imgmacro GUI: a window for building and running image-triggered macros.

Built on tkinter from the standard library and the engines in imgmacro.py
(rules), imgflow.py (flows), imgplay.py (recorded macros) and imgauto.py
(schedules, Anti-AFK, run history), so the code to trust is still these files
plus numpy, opencv-python and mss. Configs are the same JSON files the command
line uses. Four tabs: Watch (rules, checked all the time), Flows (step-by-step
macros with branches, drawn as a graph by imgflow_gui.py), Macros (recordings,
imgplay_gui.py) and Autopilot (imgauto_gui.py).

    python imgmacro_gui.py [config.json]
"""

import dataclasses
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import mss
import numpy as np

import imgauto as au
import imgflow as fl
import imgmacro as im
import imgplay as ip
import imgtheme as T

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.json"
HOTKEYS = [f"F{n}" for n in range(1, 13)]
NO_KEY = "(none)"
ACTION_LABELS = {
    "click": "Click it",
    "move": "Move the mouse to it",
    "key": "Press a key",
    "macro": "Play a macro",
    "none": "Only log it",
}
LABEL_ACTIONS = {label: action for action, label in ACTION_LABELS.items()}
DETECT_LABELS = {"image": "An image", "color": "A colour", "text": "Text"}
# Windows virtual-key code -> imgmacro key name, for the "Record" button.
VK_TO_NAME = im.VK_TO_NAME
MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def to_photo(master, image, max_w, max_h):
    """BGR array -> tk.PhotoImage, scaled down to fit max_w x max_h."""
    h, w = image.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    return tk.PhotoImage(master=master, data=im.ppm_data(image), format="PPM")


def describe_region(region):
    if not region:
        return "Whole screen"
    left, top, width, height = region
    return f"{width}×{height} at ({left}, {top})"


def describe_action(rule):
    offset = "" if tuple(rule.offset) == (0, 0) else f" {rule.offset[0]:+d},{rule.offset[1]:+d}"
    if rule.action == "macro":
        return f"Play “{rule.macro}”"
    if rule.action == "click":
        if rule.motion == "click":
            times = f" ×{rule.repeat}" if rule.repeat > 1 else ""
            return f"{rule.button.capitalize()} click{times}{offset}"
        if rule.release_template is not None:
            return f"{rule.button.capitalize()} {rule.motion} → image{offset}"
        return f"{rule.button.capitalize()} {rule.motion} {hold_seconds(rule):g}s{offset}"
    if rule.action == "move":
        return f"Move mouse{offset}"
    if rule.action == "key":
        if rule.motion == "press":
            return f"Press {rule.key}" + (f" ×{rule.repeat}" if rule.repeat > 1 else "")
        if rule.release_template is not None:
            return f"Hold {rule.key} → image"
        return f"Hold {rule.key} {rule.hold:g}s"
    return "Log only"


def spam_seconds(rule):
    """How long a run of repeated clicks or presses takes."""
    return round((rule.repeat - 1) * rule.repeat_gap, 2)


def hold_seconds(rule):
    """How long a hold or drag keeps the button down: the hold, or the whole path if longer."""
    return round(max(rule.hold, rule.path[-1][0] if rule.path else 0), 2)


def hide_from_capture(window, hidden):
    """Make a window invisible to screen capture (Windows 10 2004+), so a running
    macro can't match the thumbnails in our own window. It stays visible to you."""
    hwnd = im.user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT: the real top-level window
    im.user32.SetWindowDisplayAffinity(hwnd, 0x11 if hidden else 0)  # WDA_EXCLUDEFROMCAPTURE


def capture_desktop():
    with mss.MSS() as sct:
        return im.grab(sct, sct.monitors[0])


def describe_needed(rule):
    """The "Needs" column: a similarity, or for a colour the patch size."""
    return f"{rule.min_area} px" if rule.detect == "color" else f"{rule.threshold:.2f}"


def copy_rule(rule, **changes):
    """A copy of a rule, with its run-time state (scores, where it was seen) reset."""
    return dataclasses.replace(rule, path=list(rule.path), seen=None, fired=0,
                               last_fired=float("-inf"), _cache={}, **changes)


class Overlay(tk.Toplevel):
    """Shows what's happening on screen, over the game: a box around each match
    for a moment, and a small badge saying what's running and how to stop it.

    It covers the whole desktop but is see-through, lets every click through, and
    is hidden from screen capture, so the macros never see it.
    """

    KEY = "#010203"   # this colour is drawn as see-through
    SHOW_FOR = 0.7    # seconds a match box stays up

    def __init__(self, root):
        super().__init__(root)
        self.overrideredirect(True)
        self.configure(background=self.KEY)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", self.KEY)
        left, top, width, height = im.virtual_screen()
        self.origin = (left, top)
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.canvas = tk.Canvas(self, background=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.boxes = []  # (canvas items, time to remove)
        self.badge_text = ""
        self.update_idletasks()
        hwnd = im.user32.GetAncestor(self.winfo_id(), 2)
        ex = im.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        # Layered + transparent: clicks go through. Tool window: no taskbar button. No-activate.
        im.user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
        im.user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
        self.withdraw()

    def show_hit(self, rule, hit):
        if not hit.box:
            return
        left, top, w, h = hit.box
        x0, y0 = left - self.origin[0] - 3, top - self.origin[1] - 3
        c = self.canvas
        items = [c.create_rectangle(x0, y0, x0 + w + 6, y0 + h + 6, outline=T.GOOD, width=3),
                 c.create_text(x0, y0 - 4, anchor="sw", text=f"{rule.name}  {hit.score:.2f}",
                               fill=T.GOOD, font=("Segoe UI", 10, "bold"))]
        self.boxes.append((items, time.monotonic() + self.SHOW_FOR))
        self.deiconify()
        self.lift()

    def badge(self, text):
        """The status badge at the top of the main screen; "" hides it."""
        if text == self.badge_text:
            return
        self.badge_text = text
        c = self.canvas
        c.delete("badge")
        if text:
            x = -self.origin[0] + im.user32.GetSystemMetrics(0) // 2  # the middle of the main monitor
            y = -self.origin[1] + 14
            label = c.create_text(x, y + 13, text=text, fill=T.TEXT, font=("Segoe UI", 10, "bold"),
                                  tags="badge")
            x0, y0, x1, y1 = c.bbox(label)
            c.create_rectangle(x0 - 14, y0 - 7, x1 + 14, y1 + 7, fill=T.PANEL, outline=T.ACCENT_DARK,
                               tags="badge")
            c.tag_raise(label)
        self.tidy()

    def tidy(self):
        """Drop match boxes that have been up long enough; hide when there's nothing to show."""
        now = time.monotonic()
        for items, until in [b for b in self.boxes if b[1] <= now]:
            for item in items:
                self.canvas.delete(item)
        self.boxes = [b for b in self.boxes if b[1] > now]
        if not self.boxes and not self.badge_text:
            self.withdraw()
        elif self.state() == "withdrawn":
            self.deiconify()
            self.lift()


class App:
    def __init__(self, root, path=None):
        self.root = root
        self.cfg = im.Config(rules=[])
        self.path = None
        self.dirty = False
        self.watcher = None
        self.thread = None
        self.events = queue.Queue()
        self.thumbs = {}       # rule -> (template it was made from, PhotoImage)
        self.preview_photo = None
        self.start_was_down = False
        self.record_was_down = False
        self.recorder = None   # the ClickRecorder (or, for a macro, MacroRecorder) while recording
        self.record_into = "rules"  # or "flow" or "macro": where the recording goes
        self.flows = []        # imgflow.Flow objects; stored in self.cfg.flows on save
        self.macros = []       # imgplay.Macro objects; stored in self.cfg.macros on save
        self.unloaded_macros = []  # JSON of macros that didn't load, saved back unchanged
        self.schedules = []    # imgauto.Schedule objects; stored in self.cfg.schedules on save
        self.scheduler = au.Scheduler()
        self.next_schedule_check = 0.0
        self.stats = au.Stats(None)
        self.flow_runner = None
        self.flow_thread = None
        self.macro_runner = None
        self.macro_thread = None
        self.run_info = None   # (what, name, start time) of the run going on, for the history
        self.page = "watch"
        self.gui = sys.modules[__name__]  # for the other tabs, which reuse the dialogs here

        self.dry_run = tk.BooleanVar(value=False)
        self.minimize = tk.BooleanVar(value=True)
        self.interval = tk.StringVar()
        self.start_key = tk.StringVar()
        self.stop_key = tk.StringVar()
        self.pause_key = tk.StringVar()
        self.record_key = tk.StringVar()
        self.record_gap = tk.StringVar()
        self.record_crop = tk.StringVar()
        self.humanize_px = tk.StringVar()
        self.humanize_ms = tk.StringVar()
        self.show_matches = tk.BooleanVar()
        self.status = tk.StringVar()
        self.setting_widgets = []
        for var in (self.interval, self.start_key, self.stop_key, self.pause_key, self.record_key,
                    self.record_gap, self.record_crop, self.humanize_px, self.humanize_ms,
                    self.show_matches):
            var.trace_add("write", lambda *a: self.apply_settings())

        # Anti-AFK waits while anything runs or records, and in dry-run mode. Its thread
        # reads this plain flag, which update_status keeps current (Tk isn't thread-safe).
        self.afk_hold = False
        self.dry_run.trace_add("write", lambda *a: self.update_status())
        self.anti_afk = au.AntiAfk(on_log=lambda text: self.events.put(("log", text)),
                                   blocked=lambda: self.afk_hold)
        self.build()
        self.overlay = Overlay(root)
        self.show_config()
        if path:
            self.load(path)
        elif DEFAULT_CONFIG.exists():
            self.load(DEFAULT_CONFIG)
        if self.path is None:  # nothing opened: show the empty config on every tab
            self.after_load()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()

    # --- layout ---------------------------------------------------------------

    def build(self):
        root = self.root
        root.title("imgmacro")
        root.geometry("1280x800")
        root.minsize(960, 620)
        T.apply(root)

        menu = tk.Menu(root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="New", accelerator="Ctrl+N", command=self.new)
        file_menu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open)
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        file_menu.add_command(label="Save as…", command=self.save_as)
        file_menu.add_separator()
        file_menu.add_command(label="Export bundle…", command=self.export_bundle)
        file_menu.add_command(label="Import bundle…", command=self.import_bundle)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)
        root.config(menu=menu)
        self.file_menu = file_menu
        root.bind("<Control-n>", lambda e: self.new())
        root.bind("<Control-o>", lambda e: self.open())
        root.bind("<Control-s>", lambda e: self.save())

        # Header: name, the two tabs, and the global switches.
        header = ttk.Frame(root, style="Panel.TFrame", padding=(14, 8))
        header.pack(fill="x")
        tk.Label(header, text="◆", fg=T.ACCENT, bg=T.PANEL, font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Label(header, text="imgmacro", style="PanelTitle.TLabel").pack(side="left", padx=(6, 24))
        nav = ttk.Frame(header, style="Panel.TFrame")
        nav.pack(side="left")
        self.nav = {}
        for page, text in (("watch", "◉  Watch"), ("flows", "⟲  Flows"), ("macros", "●  Macros"),
                           ("autopilot", "◷  Autopilot")):
            b = ttk.Button(nav, text=text, style="Nav.TButton", command=lambda p=page: self.show_page(p))
            b.pack(side="left", padx=(0, 4))
            self.nav[page] = b
        self.settings_btn = ttk.Button(header, text="⚙  Settings", style="Tool.TButton",
                                       command=self.open_settings)
        self.settings_btn.pack(side="right")
        ttk.Checkbutton(header, text="Minimize while running", variable=self.minimize,
                        style="Panel.TCheckbutton").pack(side="right", padx=12)
        ttk.Checkbutton(header, text="Dry run (log only, no input)", variable=self.dry_run,
                        style="Panel.TCheckbutton").pack(side="right")
        tk.Frame(root, height=1, bg=T.BORDER).pack(fill="x")

        ttk.Label(root, textvariable=self.status, style="Status.TLabel").pack(fill="x", side="bottom")
        # Log, shared by both tabs.
        log_frame = ttk.Frame(root, style="Panel.TFrame", padding=(10, 6))
        log_frame.pack(fill="x", side="bottom")
        tk.Frame(root, height=1, bg=T.BORDER).pack(fill="x", side="bottom")
        self.log_text = tk.Text(log_frame, height=6, wrap="none", state="disabled",
                                font=("Consolas", 9), relief="flat", background=T.PANEL,
                                foreground=T.MUTED, insertbackground=T.TEXT,
                                selectbackground=T.ACCENT_DARK, highlightthickness=0)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.pages = ttk.Frame(root)
        self.pages.pack(fill="both", expand=True)
        self.watch_page = ttk.Frame(self.pages)
        self.build_watch(self.watch_page)
        # Here, not at the top: these use the dialogs defined in this file.
        import imgauto_gui
        import imgflow_gui
        import imgplay_gui
        self.flow_editor = imgflow_gui.FlowEditor(self.pages, self)
        self.macro_page = imgplay_gui.MacroPage(self.pages, self)
        self.autopilot = imgauto_gui.AutopilotPage(self.pages, self)
        self.show_page("watch")

    def build_watch(self, page):
        # Run controls
        bar = ttk.Frame(page, style="Panel.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="▶  Start", style="Accent.TButton", command=self.start)
        self.pause_btn = ttk.Button(bar, text="⏸  Pause", style="Tool.TButton",
                                    command=self.toggle_pause)
        self.stop_btn = ttk.Button(bar, text="■  Stop", style="Tool.TButton", command=self.stop_all)
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            b.pack(side="left", padx=(0, 6))
        self.record_btn = ttk.Button(bar, text="●  Record", style="Tool.TButton",
                                     command=self.toggle_recording)
        self.record_btn.pack(side="left", padx=(12, 0))
        self.test_btn = ttk.Button(bar, text="Test now", style="Tool.TButton", command=self.test_all)
        self.test_btn.pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="Rules are checked all the time, top to bottom; the first match acts.",
                  style="PanelMuted.TLabel").pack(side="right")

        body = ttk.Frame(page, padding=(10, 10, 10, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)  # the rule list takes the spare width
        body.columnconfigure(1, minsize=370)
        body.rowconfigure(0, weight=1)

        # Rule list
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=(0, 6))
        columns = ("on", "action", "where", "threshold", "score", "fired")
        self.tree = ttk.Treeview(tree_frame, columns=columns, selectmode="browse")
        headings = {"#0": ("Rule", 190), "on": ("On", 36), "action": ("Action", 125),
                    "where": ("Search area", 125), "threshold": ("Needs", 52),
                    "score": ("Score", 52), "fired": ("Fired", 46)}
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            anchor = "w" if col in ("#0", "action", "where") else "center"
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("#0", "where"))
        self.tree.tag_configure("off", foreground=T.FAINT)
        self.tree.tag_configure("hit", foreground=T.GOOD)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_preview())
        self.tree.bind("<Double-1>", lambda e: self.edit_rule())
        self.tree.bind("<space>", lambda e: self.toggle_rule())
        self.tree.bind("<Delete>", lambda e: self.remove_rule())

        buttons = ttk.Frame(left)
        buttons.pack(fill="x")
        self.rule_buttons = []
        for text, command in (("+  Add rule…", self.add_rule), ("Edit…", self.edit_rule),
                              ("Duplicate", self.duplicate_rule), ("Remove", self.remove_rule),
                              ("On / Off", self.toggle_rule), ("▲", lambda: self.move_rule(-1)),
                              ("▼", lambda: self.move_rule(1))):
            b = ttk.Button(buttons, text=text, command=command, style="Tool.TButton",
                           width=3 if len(text) == 1 else None)
            b.pack(side="left", padx=(0, 4))
            self.rule_buttons.append(b)

        # Preview
        right = ttk.Frame(body, style="Panel.TFrame", padding=14)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ttk.Label(right, text="Selected rule", style="PanelMuted.TLabel").pack(anchor="w")
        self.preview_image = tk.Label(right, text="No rule selected", bg=T.BG, fg=T.MUTED,
                                      height=8)
        self.preview_image.pack(fill="x", pady=(6, 8))
        self.preview_text = ttk.Label(right, justify="left", wraplength=335, style="Panel.TLabel")
        self.preview_text.pack(anchor="w")

    def show_page(self, page):
        self.page = page
        for name, b in self.nav.items():
            b.configure(style="NavOn.TButton" if name == page else "Nav.TButton")
        pages = {"watch": self.watch_page, "flows": self.flow_editor, "macros": self.macro_page,
                 "autopilot": self.autopilot}
        for frame in pages.values():
            frame.pack_forget()
        pages[page].pack(fill="both", expand=True)
        if page == "flows":
            self.flow_editor.after_idle(self.flow_editor.canvas.render)
        elif page == "autopilot":
            self.autopilot.refresh()
        self.update_status()

    def open_settings(self):
        """The settings (keys, check rate, recording) in a small window; they apply as you type."""
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(background=T.BG)
        self.setting_widgets = []
        general = ttk.LabelFrame(win, text="General", padding=10)
        general.pack(fill="x", padx=12, pady=(12, 0))
        self.setting_rows(general, (("Check the screen every (s)", self.interval, (0.05, 10, 0.05)),
                                    ("Start / run key", self.start_key, HOTKEYS),
                                    ("Pause / resume key", self.pause_key, HOTKEYS),
                                    ("Stop key", self.stop_key, HOTKEYS),
                                    ("Record key", self.record_key, [NO_KEY] + HOTKEYS)),
                          note="Hotkeys work while the game has focus. The start key runs the "
                               "rules on the Watch tab, the shown flow on the Flows tab, or the "
                               "selected macro on the Macros tab; the record key starts and stops "
                               "recording on the tab you're on.")
        recording = ttk.LabelFrame(win, text="Recording", padding=10)
        recording.pack(fill="x", padx=12, pady=(10, 0))
        self.setting_rows(recording, (("Spam gap (s)", self.record_gap, (0, 10, 0.25)),
                                      ("Auto-crop size (px)", self.record_crop, (16, 600, 8))),
                          note="Clicks in one spot less than the spam gap apart share a "
                               "screenshot, so spam-clicking makes one, not dozens. The stop "
                               "key also ends a recording.")
        human = ttk.LabelFrame(win, text="Humanize", padding=10)
        human.pack(fill="x", padx=12, pady=(10, 0))
        self.setting_rows(human, (("Vary each click by up to (px)", self.humanize_px, (0, 50, 1)),
                                  ("…and its timing by up to (ms)", self.humanize_ms, (0, 500, 5))),
                          note="Rules and flow steps click a random few pixels off and wait a "
                               "random bit longer, so they don't repeat the exact same pixel and "
                               "rhythm. 0 = exact. Recorded macros and drags always replay exactly.")
        screen = ttk.LabelFrame(win, text="On screen", padding=10)
        screen.pack(fill="x", padx=12, pady=(10, 0))
        check = ttk.Checkbutton(screen, text="Show matches and what's running on screen",
                                variable=self.show_matches)
        check.pack(anchor="w")
        self.setting_widgets.append(check)
        ttk.Label(screen, text="Draws a box around each match for a moment, and a badge at the top "
                               "saying what's running and which key stops it. It's see-through to "
                               "clicks and invisible to screenshots, so macros never see it.",
                  foreground=T.MUTED, wraplength=320, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Button(win, text="Close", command=win.destroy).pack(anchor="e", padx=12, pady=12)
        win.grab_set()

    def setting_rows(self, frame, rows, note):
        """Label + combobox (a list of choices) or spinbox ((from, to, step)) per row."""
        for row, (label, var, values) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            if isinstance(values, list):
                w = ttk.Combobox(frame, textvariable=var, values=values, width=8, state="readonly")
            else:
                low, high, step = values
                w = ttk.Spinbox(frame, textvariable=var, from_=low, to=high, increment=step, width=8)
            w.grid(row=row, column=1, sticky="w", padx=(10, 0), pady=2)
            self.setting_widgets.append(w)
        ttk.Label(frame, text=note, foreground=T.MUTED, wraplength=320,
                  justify="left").grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 0))

    # --- config <-> widgets ---------------------------------------------------

    def show_config(self):
        self.loading = True
        self.interval.set(f"{self.cfg.interval:g}")
        self.start_key.set(self.cfg.start_key)
        self.stop_key.set(self.cfg.stop_key)
        self.pause_key.set(self.cfg.pause_key)
        self.record_gap.set(f"{self.cfg.record_gap:g}")
        self.record_crop.set(str(self.cfg.record_crop))
        self.record_key.set(self.cfg.record_key or NO_KEY)
        self.humanize_px.set(str(self.cfg.humanize_px))
        self.humanize_ms.set(str(self.cfg.humanize_ms))
        self.show_matches.set(self.cfg.show_matches)
        self.loading = False
        self.refresh_tree()
        self.update_controls()

    def apply_settings(self):
        if getattr(self, "loading", False):
            return
        for attr, var, kind in (("interval", self.interval, float),
                                ("record_gap", self.record_gap, float),
                                ("record_crop", self.record_crop, int),
                                ("humanize_px", self.humanize_px, int),
                                ("humanize_ms", self.humanize_ms, int)):
            try:
                setattr(self.cfg, attr, kind(var.get()))
            except ValueError:
                pass  # mid-typing; validated again on Start, Record and Save
        self.cfg.start_key = self.start_key.get()
        self.cfg.stop_key = self.stop_key.get()
        self.cfg.pause_key = self.pause_key.get()
        self.cfg.record_key = "" if self.record_key.get() == NO_KEY else self.record_key.get()
        self.cfg.show_matches = self.show_matches.get()
        self.mark_dirty()
        self.update_status()

    def config_dir(self):
        return self.path.parent if self.path else APP_DIR

    def mark_dirty(self):
        self.dirty = True
        self.update_title()

    def update_title(self):
        name = self.path.name if self.path else "Untitled"
        # The title is what shows on the taskbar, so say when we're recording.
        recording = ""
        if self.recorder is not None:
            n = (self.recorder.clicks + self.recorder.keys if self.record_into == "macro"
                 else len(self.recorder.captures))
            recording = f"● Recording ({n}) — "
        self.root.title(f"{recording}{name}{' *' if self.dirty else ''} — imgmacro")

    # --- rule list --------------------------------------------------------------

    def thumb(self, rule):
        cached = self.thumbs.get(rule)
        if cached is None or cached[0] is not rule.preview:
            cached = (rule.preview, to_photo(self.root, rule.preview, 64, 32))
            self.thumbs[rule] = cached
        return cached[1]

    def refresh_tree(self, select=None):
        if select is None:
            select = self.selected_index()
        self.tree.delete(*self.tree.get_children())
        for i, rule in enumerate(self.cfg.rules):
            self.tree.insert("", "end", iid=str(i), text="  " + rule.name, image=self.thumb(rule),
                             values=("✓" if rule.enabled else "", describe_action(rule),
                                     describe_region(rule.region), describe_needed(rule),
                                     "", rule.fired or ""),
                             tags=() if rule.enabled else ("off",))
        if select is not None and self.cfg.rules:
            iid = str(min(select, len(self.cfg.rules) - 1))
            self.tree.selection_set(iid)
            self.tree.see(iid)
        self.show_preview()

    def selected_index(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def selected_rule(self):
        index = self.selected_index()
        return None if index is None else self.cfg.rules[index]

    def show_preview(self):
        rule = self.selected_rule()
        if rule is None:
            self.preview_photo = None
            self.preview_image.configure(image="", text="No rule selected", height=8)
            self.preview_text.configure(text="Add a rule to start: snip the image to look for, "
                                             "then choose what happens when it appears.")
            return
        self.preview_photo = to_photo(self.root, rule.preview, 330, 180)
        self.preview_image.configure(image=self.preview_photo, text="", height=0)
        when = "whenever it appears" if rule.cooldown == 0 else \
            f"at most every {rule.cooldown:g} s"
        restore = ", then put the mouse back" if rule.action == "click" and rule.restore_cursor else ""
        if rule.detect == "color":
            reach = (f"When a patch of at least {rule.min_area} px (each channel within "
                     f"±{rule.tolerance}) shows up")
        else:
            reach = f"When the match score reaches {rule.threshold:.2f}"
        extras = [what for on, what in ((rule.multiscale, "at other sizes"),
                                        (rule.edges, "by its outline")) if on]
        lines = [
            f"{rule.name}{'' if rule.enabled else '  (off)'}",
            f"Looks for {rule.describe_target()} in: {describe_region(rule.region).lower()}"
            + (f", also {' and '.join(extras)}" if extras else ""),
            f"{reach}: {describe_action(rule).lower()}{restore}, {when}.",
        ]
        if rule.detect == "image":
            lines.append(f"Image: {self.short_path(rule.image)}")
        if rule.action in ("click", "key") and rule.repeat > 1:
            verb = "Clicks" if rule.action == "click" else "Presses it"
            lines.insert(3, f"{verb} {rule.repeat} times, {rule.repeat_gap:g} s apart "
                            f"(takes {spam_seconds(rule):g} s).")
        if rule.action == "click" and rule.release_template is not None:
            wait = f" + {rule.release_delay * 1000:.0f} ms" if rule.release_delay else ""
            lines.insert(3, f"Lets go when the release image appears (score {rule.release_threshold:.2f})"
                            f"{wait}, or after {rule.hold:g} s at most.")
        self.preview_text.configure(text="\n".join(lines))

    def short_path(self, path):
        try:
            return Path(path).relative_to(self.config_dir())
        except ValueError:
            return path

    def add_rule(self):
        rule = RuleDialog(self).result
        if rule:
            self.cfg.rules.append(rule)
            self.mark_dirty()
            self.refresh_tree(select=len(self.cfg.rules) - 1)

    def edit_rule(self):
        index = self.selected_index()
        if index is None or self.busy():
            return
        rule = RuleDialog(self, self.cfg.rules[index]).result
        if rule:
            self.cfg.rules[index] = rule
            self.mark_dirty()
            self.refresh_tree(select=index)

    def duplicate_rule(self):
        index = self.selected_index()
        if index is None:
            return
        src = self.cfg.rules[index]
        self.cfg.rules.insert(index + 1, copy_rule(src, name=f"{src.name} (copy)"))
        self.mark_dirty()
        self.refresh_tree(select=index + 1)

    def remove_rule(self):
        index = self.selected_index()
        if index is None or self.busy():
            return
        rule = self.cfg.rules[index]
        if messagebox.askyesno("Remove rule", f"Remove “{rule.name}”?\n\n"
                               "Its image file stays on disk.", parent=self.root):
            del self.cfg.rules[index]
            self.mark_dirty()
            self.refresh_tree(select=index)

    def toggle_rule(self):
        rule = self.selected_rule()
        if rule is None or self.busy():
            return
        rule.enabled = not rule.enabled
        self.mark_dirty()
        self.refresh_tree()

    def add_rules(self, rules):
        self.cfg.rules.extend(rules)
        self.mark_dirty()
        self.refresh_tree(select=len(self.cfg.rules) - 1)

    def save_template(self, name, image):
        """Save a template image in the config's templates folder -> its path."""
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"
        folder = self.config_dir() / "templates"
        path, n = folder / f"{slug}.png", 2
        while path.exists():
            path, n = folder / f"{slug}-{n}.png", n + 1
        im.write_image(path, image)
        return path

    def move_rule(self, delta):
        index = self.selected_index()
        if index is None or not 0 <= index + delta < len(self.cfg.rules):
            return
        rules = self.cfg.rules
        rules[index], rules[index + delta] = rules[index + delta], rules[index]
        self.mark_dirty()
        self.refresh_tree(select=index + delta)

    # --- capturing --------------------------------------------------------------

    def while_hidden(self, fn, delay=0.0):
        """Run fn() with our windows hidden, so they don't cover what is being captured."""
        windows = [self.root] + [w for w in self.root.winfo_children()
                                 if isinstance(w, tk.Toplevel) and w.winfo_viewable()]
        for w in windows:
            w.withdraw()
        self.root.update()
        time.sleep(max(delay, 0.3))  # give the desktop time to repaint underneath
        try:
            return fn()
        finally:
            for w in windows:  # restore in stacking order, so the main window isn't left behind
                w.deiconify()
                w.lift()
            top = windows[-1]
            top.focus_force()
            if top is not self.root:
                top.grab_set()

    def test_all(self):
        if not self.cfg.rules:
            messagebox.showinfo("Test", "Add a rule first.", parent=self.root)
            return
        frame, origin = self.while_hidden(capture_desktop)
        self.log("Test:")
        for i, rule in enumerate(self.cfg.rules):
            try:
                hit = rule.find(frame, origin)
            except ValueError as exc:
                self.log(f"  {rule.name}: {exc}")
                self.tree.set(str(i), "score", "—")
                continue
            if hit is None:
                self.log(f"  {rule.name}: search area is smaller than the image")
                self.tree.set(str(i), "score", "—")
                continue
            verdict = "would act" if hit.score >= rule.needed else "no match"
            how = f" ({hit.how})" if hit.how else ""
            self.log(f"  {rule.name}: best {hit.score:.3f}{how} at ({hit.x}, {hit.y}) -> {verdict}")
            self.show_score(rule, hit.score)

    def show_score(self, rule, score):
        if rule not in self.cfg.rules:  # a late update for a rule that's since been removed
            return
        iid = str(self.cfg.rules.index(rule))
        tags = [] if rule.enabled else ["off"]
        if score >= rule.needed:
            tags.append("hit")
        self.tree.set(iid, "score", f"{score:.2f}")
        self.tree.set(iid, "fired", rule.fired or "")
        self.tree.item(iid, tags=tags)

    # --- running ----------------------------------------------------------------

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def flow_running(self):
        return self.flow_thread is not None and self.flow_thread.is_alive()

    def macro_running(self):
        return self.macro_thread is not None and self.macro_thread.is_alive()

    def any_running(self):
        return self.running() or self.flow_running() or self.macro_running()

    def busy(self):
        return self.any_running() or self.recorder is not None

    def current_runner(self):
        """The Watcher, FlowRunner or MacroRunner that's running, or None."""
        if self.running():
            return self.watcher
        if self.flow_running():
            return self.flow_runner
        if self.macro_running():
            return self.macro_runner
        return None

    def ready_to_run(self, title):
        """Check the settings before a run. -> True if it can go."""
        if self.busy():
            return False
        try:
            self.cfg.interval = float(self.interval.get())
            self.cfg.validate()
        except ValueError as exc:
            messagebox.showerror(title, str(exc), parent=self.root)
            return False
        return True

    def macro_map(self):
        return {m.name: m for m in self.macros}

    def launch(self, runner, attr, what, name, message):
        """Start a run on a worker thread, kept in self.<attr>."""
        self.run_info = (what, name, time.monotonic())
        mode = " in dry-run mode (no input is sent)" if self.dry_run.get() else ""
        self.log(f"{message}{mode}.")
        thread = threading.Thread(target=runner.run, daemon=True)
        setattr(self, attr, thread)  # before update_controls, which asks what's running
        thread.start()
        hide_from_capture(self.root, True)
        self.update_controls()
        if self.minimize.get():
            self.root.iconify()

    def callbacks(self):
        put = self.events.put
        return dict(on_log=lambda text: put(("log", text)),
                    on_pause=lambda paused: put(("pause", paused)),
                    on_hit=lambda rule, hit: put(("hit", (rule, hit))))

    def start_flow(self, flow):
        if not self.ready_to_run("Can't run"):
            return
        self.flow_runner = fl.FlowRunner(flow, self.cfg, dry_run=self.dry_run.get(),
                                         on_step=lambda step: self.events.put(("flowstep", step)),
                                         flows=self.flows, macros=self.macro_map(), **self.callbacks())
        self.launch(self.flow_runner, "flow_thread", "flow", flow.name, f"Running flow “{flow.name}”")

    def start_macro(self, macro, speed=None, loops=None):
        if not self.ready_to_run("Can't play"):
            return
        self.macro_runner = ip.MacroRunner(macro, self.cfg, dry_run=self.dry_run.get(), speed=speed,
                                           loops=loops, **self.callbacks())
        r = self.macro_runner
        times = "until stopped" if r.loops == 0 else f"{r.loops}×"
        self.launch(r, "macro_thread", "macro", macro.name,
                    f"Playing macro “{macro.name}” at {r.speed:g}×, {times}")

    def start(self):
        if not self.ready_to_run("Can't start"):
            return
        if not any(r.enabled for r in self.cfg.rules):
            messagebox.showinfo("Can't start", "There are no rules switched on.", parent=self.root)
            return
        for rule in self.cfg.rules:
            rule.last_fired, rule.fired = float("-inf"), 0
        self.refresh_tree()
        self.watcher = im.Watcher(self.cfg, dry_run=self.dry_run.get(), macros=self.macro_map(),
                                  on_tick=lambda scores: self.events.put(("tick", scores)),
                                  **self.callbacks())
        self.launch(self.watcher, "thread", "rules", "Watch rules",
                    f"Started watching {len(self.watcher.rules)} rule(s)")

    def start_shown(self):
        """The start key: run what the shown tab runs."""
        if self.page == "flows":
            self.flow_editor.run()
        elif self.page == "macros":
            self.macro_page.play()
        else:
            self.start()

    def stop_all(self):
        runner = self.current_runner()
        if runner is not None:
            runner.stop()

    def toggle_pause(self):
        runner = self.current_runner()
        if runner is not None:
            runner.set_paused(not runner.paused)

    def stop(self):
        self.stop_all()

    def run_ended(self, runner):
        """A run's thread has finished: note it in the history, and tidy up."""
        hide_from_capture(self.root, False)
        if self.run_info is not None:
            what, name, start = self.run_info
            self.run_info = None
            try:
                self.stats.record(what, name, time.monotonic() - start, runner.outcome or "stopped")
            except OSError as exc:
                self.log(f"Couldn't save the run history: {exc}")
        self.update_controls()
        self.autopilot.refresh()
        self.macro_page.refresh_list()

    def update_controls(self):
        running, recording = self.any_running(), self.recorder is not None
        editing = "disabled" if running or recording else "normal"
        for b in self.rule_buttons + [self.test_btn, self.start_btn, self.settings_btn]:
            b.configure(state=editing)
        for item in ("New", "Open…", "Import bundle…"):
            self.file_menu.entryconfigure(item, state=editing)
        self.pause_btn.configure(state="normal" if self.running() else "disabled", text="⏸  Pause")
        self.stop_btn.configure(state="normal" if self.running() else "disabled")
        rules_recording = recording and self.record_into == "rules"
        self.record_btn.configure(state="disabled" if running or (recording and not rules_recording)
                                  else "normal",
                                  text="■  Stop recording" if rules_recording else "●  Record")
        self.flow_editor.set_busy(running or recording, running=self.flow_running(),
                                  recording=recording and self.record_into == "flow")
        self.macro_page.set_busy(running or recording, running=self.macro_running(),
                                 recording=recording and self.record_into == "macro")
        self.update_title()
        self.update_status()

    def update_status(self):
        self.afk_hold = self.busy() or self.dry_run.get()
        keys = (f"{self.cfg.start_key} start  ·  {self.cfg.pause_key} pause  ·  "
                f"{self.cfg.stop_key} stop")
        if self.cfg.record_key:
            keys += f"  ·  {self.cfg.record_key} record"
        badge = ""
        if self.recorder is not None:
            if self.record_into == "macro":
                text = (f"Recording a macro: {self.recorder.clicks} click(s), {self.recorder.keys} key "
                        f"press(es)      {self.cfg.stop_key} or Stop recording to finish")
            else:
                shots = len(self.recorder.captures)
                text = (f"Recording: {self.recorder.clicks} click(s), {self.recorder.keys} key "
                        f"press(es), {shots} screenshot(s)      "
                        f"{self.cfg.stop_key} or Stop recording to finish")
            self.status.set(text)
            badge = f"●  Recording   ·   {self.cfg.stop_key} stops"
        else:
            if self.running():
                state = "Paused" if self.watcher.paused else \
                    ("Running rules (dry run)" if self.watcher.dry_run else "Running rules")
            elif self.flow_running():
                r = self.flow_runner
                state = f"Paused flow “{r.flow.name}”" if r.paused else \
                    f"Running flow “{r.flow.name}”" + (" (dry run)" if r.dry_run else "")
            elif self.macro_running():
                r = self.macro_runner
                state = f"Paused macro “{r.macro.name}”" if r.paused else \
                    f"Playing macro “{r.macro.name}”" + (" (dry run)" if r.dry_run else "")
            else:
                state = "Stopped"
            self.status.set(f"{state}      {keys}")
            if self.any_running():
                badge = (f"▶  {state}   ·   {self.cfg.pause_key} pause   ·   "
                         f"{self.cfg.stop_key} stop")
        if hasattr(self, "overlay"):
            self.overlay.badge(badge if self.cfg.show_matches else "")

    def poll(self):
        """Drain events from the worker threads, watch the start and record hotkeys,
        and run schedules that are due (every 50 ms)."""
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log(value)
                elif kind == "tick":
                    for rule, score in value.items():
                        self.show_score(rule, score)
                elif kind == "hit":
                    if self.cfg.show_matches:
                        self.overlay.show_hit(*value)
                elif kind == "pause":
                    text = "▶  Resume" if value else "⏸  Pause"
                    self.pause_btn.configure(text=text)
                    self.flow_editor.pause_btn.configure(text=text)
                    self.macro_page.pause_btn.configure(text=text)
                    self.update_status()
                elif kind == "flowstep":
                    self.flow_editor.show_running(value)
                elif kind == "captured":
                    captures = self.recorder.captures if self.recorder else []
                    n = captures.index(value) + 1 if value in captures else "?"
                    what = f"key {value.key}" if value.key else f"{value.button} {value.kind}"
                    if value.key and value.hold:
                        what = f"held key {value.key}"
                    if value.hold:
                        what += f" for {value.hold:g} s"
                    if value.moves:
                        _, dx, dy = value.moves[-1]
                        what += f", ending {dx:+d}, {dy:+d} px away"
                    where = "" if value.key else f" at ({value.x}, {value.y})"
                    self.log(f"Screenshot {n}: {what}{where}")
                    self.update_title()
                    self.update_status()
                elif kind in ("merged", "macro-input"):
                    self.update_title()
                    self.update_status()
                elif kind == "recorded":
                    self.root.after(0, self.finish_recording)  # not inside poll: it opens a dialog
        except queue.Empty:
            pass
        if self.thread is not None and not self.thread.is_alive():
            self.thread = None
            self.run_ended(self.watcher)
        if self.flow_thread is not None and not self.flow_thread.is_alive():
            self.flow_thread = None
            self.flow_editor.show_running(None)
            self.run_ended(self.flow_runner)
        if self.macro_thread is not None and not self.macro_thread.is_alive():
            self.macro_thread = None
            self.run_ended(self.macro_runner)
        if self.overlay.boxes:
            self.overlay.tidy()
        idle = not self.busy() and not self.root.grab_current()  # not while a dialog is open
        start_down = self.hotkey_down(self.cfg.start_key)
        if start_down and not self.start_was_down and idle:
            self.start_shown()
        self.start_was_down = start_down
        record_down = self.hotkey_down(self.cfg.record_key)
        if record_down and not self.record_was_down and idle:
            self.toggle_recording({"flows": "flow", "macros": "macro"}.get(self.page, "rules"))
        self.record_was_down = record_down
        if time.monotonic() >= self.next_schedule_check:
            self.next_schedule_check = time.monotonic() + 1.0
            self.run_schedules()
            self.autopilot.tick()
        self.root.after(50, self.poll)

    @staticmethod
    def hotkey_down(key):
        try:
            return bool(key) and im.key_down(im.parse_key(key))
        except ValueError:
            return False

    def run_schedules(self):
        for s in self.scheduler.take_due():
            what = f"{s.what} “{s.target}”"
            target = next((x for x in (self.flows if s.what == "flow" else self.macros)
                           if x.name == s.target), None)
            if target is None:
                self.log(f"{time.strftime('%H:%M:%S')}  Schedule: there's no {what} any more")
            elif self.busy() or self.root.grab_current():
                self.log(f"{time.strftime('%H:%M:%S')}  Schedule: skipped {what}, something else "
                         "is running (or a window is open)")
            else:
                self.log(f"{time.strftime('%H:%M:%S')}  Schedule: running {what}")
                if s.what == "flow":
                    self.start_flow(target)
                else:
                    self.start_macro(target)

    # --- recording --------------------------------------------------------------

    def toggle_recording(self, into="rules"):
        if self.recorder is not None:
            self.recorder.stop()
            return
        if self.any_running():
            return
        self.record_into = into
        try:
            self.cfg.validate()
        except ValueError as exc:
            messagebox.showerror("Can't record", str(exc), parent=self.root)
            return
        put = self.events.put
        ignore = (self.cfg.start_key, self.cfg.pause_key)
        also = (self.cfg.record_key,)
        if into == "macro":
            self.recorder = ip.MacroRecorder(
                stop_key=self.cfg.stop_key, ignore_keys=ignore, also_stop=also,
                on_event=lambda kind: put(("macro-input", kind)),
                on_stop=lambda: put(("recorded", None)), on_log=lambda text: put(("log", text)))
            self.recorder.start()
            self.log("Recording a macro: every mouse move, click, wheel turn and key, with its timing. "
                     f"It starts at your first click or key. Press {self.cfg.stop_key} or Stop "
                     "recording to finish.")
        else:
            # Full screenshots can show anything on screen, so they live in a temporary
            # folder that's deleted once the recording has been reviewed.
            folder = Path(tempfile.mkdtemp(prefix="imgmacro-recording-"))
            self.recorder = im.ClickRecorder(
                folder, gap=self.cfg.record_gap, crop_size=self.cfg.record_crop,
                stop_key=self.cfg.stop_key, ignore_keys=ignore, also_stop=also,
                on_capture=lambda c: put(("captured", c)), on_merge=lambda c: put(("merged", c)),
                on_stop=lambda: put(("recorded", None)), on_log=lambda text: put(("log", text)))
            hide_from_capture(self.root, True)  # keep our window out of the screenshots
            self.recorder.start()
            self.log(f"Recording clicks and keys. Clicks less than {self.cfg.record_gap:g} s apart in "
                     "the same spot, or the same key pressed again that quickly, share a screenshot. "
                     f"Press {self.cfg.stop_key} or Stop recording to finish.")
        self.update_controls()
        if self.minimize.get():
            self.root.iconify()

    def finish_recording(self):
        recorder, self.recorder = self.recorder, None
        if recorder is None:  # already dealt with (the window is closing)
            return
        hide_from_capture(self.root, False)
        self.update_controls()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.record_into == "macro":
            self.finish_macro_recording(recorder)
            return
        self.log(f"Recording stopped: {recorder.clicks} click(s), {recorder.keys} key press(es), "
                 f"{len(recorder.captures)} screenshot(s).")
        try:
            if recorder.captures:
                RecordingReview(self, recorder.captures, as_steps=self.record_into == "flow")
        finally:
            shutil.rmtree(recorder.folder, ignore_errors=True)

    def finish_macro_recording(self, recorder):
        if not recorder.events:
            self.log("Recording stopped: nothing was recorded.")
            return
        macro = ip.Macro("", Path(), recorder.events, recorder.screen)
        self.log(f"Recording stopped: {macro.summary()}.")
        taken = {m.name for m in self.macros}
        name, n = f"Macro {len(self.macros) + 1}", len(self.macros) + 2
        while name in taken:
            name, n = f"Macro {n}", n + 1
        while True:
            name = simpledialog.askstring("Name the macro", f"Recorded {macro.summary()}.\n\nName:",
                                          initialvalue=name, parent=self.root)
            if name is None:
                if messagebox.askyesno("Discard recording", "Discard this recording?", parent=self.root):
                    return
                continue
            name = name.strip()
            if name and name not in taken:
                break
            messagebox.showerror("Name the macro", "Give it a name no other macro has.", parent=self.root)
        macro.name, macro.file = name, ip.recording_path(self.config_dir(), name)
        try:
            ip.write_recording(macro)
        except OSError as exc:
            messagebox.showerror("Couldn't save the recording", str(exc), parent=self.root)
            return
        self.macros.append(macro)
        self.mark_dirty()
        self.macro_page.refresh_list(select=macro)
        self.log(f"Saved the recording as {self.short_path(macro.file)}.")

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 1000:
            self.log_text.delete("1.0", f"{lines - 1000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # --- files ------------------------------------------------------------------

    def confirm_discard(self):
        """True if it's fine to throw away the current config (saving it if asked)."""
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("Unsaved changes", "Save changes first?",
                                           parent=self.root)
        if answer is None:
            return False
        return self.save() if answer else True

    def new(self):
        if self.busy() or not self.confirm_discard():
            return
        self.cfg, self.path, self.dirty = im.Config(rules=[]), None, False
        self.flows, self.macros, self.schedules = [], [], []
        self.unloaded_macros = []
        self.after_load()

    def open(self):
        if self.busy() or not self.confirm_discard():
            return
        path = filedialog.askopenfilename(parent=self.root, initialdir=self.config_dir(),
                                          filetypes=[("imgmacro config", "*.json")])
        if path:
            self.load(path)

    def load(self, path):
        try:
            cfg = im.load_config(path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Couldn't open config", str(exc), parent=self.root)
            return
        loaded, problems = {}, []
        for what, load in (("flows", fl.load_flows),
                           ("macros", lambda c, p: ip.load_macros(c, p, problems)),
                           ("schedules", lambda c, p: au.load_schedules(c))):
            try:
                loaded[what] = load(cfg, path)
            except (ValueError, OSError, KeyError) as exc:
                messagebox.showerror(f"Couldn't read the {what}", f"{exc}\n\nThe rest loaded; the "
                                     f"{what} were left out (and are dropped if you save).",
                                     parent=self.root)
                loaded[what] = []
        if problems:
            lines = "\n".join(f"• {d.get('name', '?')}: {exc}" for d, exc in problems)
            messagebox.showwarning("Some macros didn't load", f"{lines}\n\nThey're left out for now, "
                                   "and kept in the config as they are when you save.", parent=self.root)
        self.unloaded_macros = [d for d, _ in problems]
        self.cfg, self.path, self.dirty = cfg, Path(path).resolve(), False
        self.flows, self.macros, self.schedules = loaded["flows"], loaded["macros"], loaded["schedules"]
        self.after_load()
        self.log(f"Opened {self.path}")

    def after_load(self):
        """Show a config that's just been loaded (or a new one) on every tab."""
        self.stats = au.Stats(au.stats_path(self.path) if self.path else None)
        self.scheduler = au.Scheduler(self.schedules)
        settings = self.cfg.anti_afk
        try:
            self.anti_afk.configure(every=float(settings.get("every", 15)),
                                    key=str(settings.get("key", "space")), enabled=False)
        except ValueError:
            pass
        self.show_config()
        self.flow_editor.refresh(keep=False)
        self.macro_page.refresh_list()
        self.autopilot.load()

    def store(self):
        """Put the flows, macros and schedules into the config, ready to save or export."""
        path = self.path or APP_DIR / "config.json"
        fl.store_flows(self.cfg, self.flows, path)
        ip.store_macros(self.cfg, self.macros, path, self.unloaded_macros)
        au.store_schedules(self.cfg, self.schedules)

    def save(self):
        if self.path is None:
            return self.save_as()
        try:
            self.cfg.interval = float(self.interval.get())
            self.cfg.validate()
            self.store()
            im.save_config(self.cfg, self.path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Couldn't save", str(exc), parent=self.root)
            return False
        self.dirty = False
        self.update_title()
        self.log(f"Saved {self.path}")
        return True

    def save_as(self):
        path = filedialog.asksaveasfilename(parent=self.root, initialdir=self.config_dir(),
                                            initialfile=self.path.name if self.path else "config.json",
                                            defaultextension=".json",
                                            filetypes=[("imgmacro config", "*.json")])
        if not path:
            return False
        self.path = Path(path).resolve()
        self.stats = au.Stats(au.stats_path(self.path))
        return self.save()

    # --- bundles ----------------------------------------------------------------

    def export_bundle(self):
        import imgbundle
        if not (self.cfg.rules or self.flows or self.macros):
            messagebox.showinfo("Export bundle", "There's nothing to export yet.", parent=self.root)
            return
        choice = ExportDialog(self).result
        if not choice:
            return
        stem = self.path.stem if self.path else "imgmacro"
        path = filedialog.asksaveasfilename(parent=self.root, initialdir=self.config_dir(),
                                            initialfile=f"{stem}.imgbundle", defaultextension=".imgbundle",
                                            filetypes=[("imgmacro bundle", "*.imgbundle")])
        if not path:
            return
        rules, flows, macros = choice
        try:
            self.store()
            summary = imgbundle.export_bundle(self.cfg, self.path or APP_DIR / "config.json", path,
                                              rules, flows, macros)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Couldn't export", str(exc), parent=self.root)
            return
        self.log(f"Exported {summary} to {path}")

    def import_bundle(self):
        import imgbundle
        if self.busy():
            return
        path = filedialog.askopenfilename(parent=self.root, initialdir=self.config_dir(),
                                          filetypes=[("imgmacro bundle", "*.imgbundle")])
        if not path:
            return
        base = self.config_dir()
        try:
            rules, flows, macros, summary = imgbundle.import_bundle(
                path, base / "config.json", [f.name for f in self.flows], [m.name for m in self.macros])
            new_rules = [im.rule_from_dict(r, base, i) for i, r in enumerate(rules, 1)]
            new_macros = [ip.macro_from_dict(d, base) for d in macros]
            new_flows = [fl.flow_from_dict(d, base) for d in flows]
        except (ValueError, OSError, KeyError) as exc:
            messagebox.showerror("Couldn't import", str(exc), parent=self.root)
            return
        self.cfg.rules.extend(new_rules)
        self.macros.extend(new_macros)
        self.flows.extend(new_flows)
        self.mark_dirty()
        self.refresh_tree()
        self.flow_editor.refresh()
        self.macro_page.refresh_list()
        self.log(f"Imported {summary}. Save to keep them.")

    def on_close(self):
        if not self.confirm_discard():  # first: cancelling leaves everything running
            return
        runner = self.current_runner()
        if runner is not None:
            runner.stop()
            for thread in (self.thread, self.flow_thread, self.macro_thread):
                if thread is not None:
                    thread.join(timeout=2)
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder.thread.join(timeout=5)
            if self.record_into != "macro":
                shutil.rmtree(self.recorder.folder, ignore_errors=True)
            self.recorder = None
        self.anti_afk.close()
        self.root.destroy()


class ExportDialog(tk.Toplevel):
    """Pick what goes in a bundle. `.result` is (rules, flow names, macro names), or None."""

    def __init__(self, app):
        super().__init__(app.root)
        self.result = None
        self.title("Export bundle")
        self.transient(app.root)
        self.resizable(False, True)
        ttk.Label(self, text="What goes in the bundle? Flows bring along the macros and flows they "
                             "run, and every image and recording they need comes too.",
                  wraplength=420, justify="left", padding=(12, 12, 12, 4)).pack(anchor="w")
        self.items = []  # (BooleanVar, kind, value)
        body = ttk.Frame(self, padding=(12, 0))
        body.pack(fill="both", expand=True)
        for title, kind, entries in (("Rules", "rule", [(r.name, r) for r in app.cfg.rules]),
                                     ("Flows", "flow", [(f.name, f.name) for f in app.flows]),
                                     ("Macros", "macro", [(m.name, m.name) for m in app.macros])):
            if not entries:
                continue
            box = ttk.LabelFrame(body, text=title, padding=8)
            box.pack(fill="x", pady=(8, 0))
            for label, value in entries:
                var = tk.BooleanVar(value=True)
                ttk.Checkbutton(box, text=label, variable=var).pack(anchor="w")
                self.items.append((var, kind, value))
        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Export…", command=self.ok).pack(side="right", padx=6)
        self.grab_set()
        app.root.wait_window(self)

    def ok(self):
        picked = {"rule": [], "flow": [], "macro": []}
        for var, kind, value in self.items:
            if var.get():
                picked[kind].append(value)
        if not any(picked.values()):
            messagebox.showinfo("Export bundle", "Tick at least one thing to export.", parent=self)
            return
        self.result = (picked["rule"], picked["flow"], picked["macro"])
        self.destroy()


class RuleDialog(tk.Toplevel):
    """Create or edit one rule. `.result` is the new Rule, or None if cancelled.

    `allow_macro` offers "Play a macro" as an action (not for a macro's own
    safeguards and checkpoints); `kind` names what's being edited in the title.
    """

    def __init__(self, app, rule=None, allow_macro=True, kind="rule", default_action="click"):
        super().__init__(app.root)
        self.app = app
        self.result = None
        self.allow_macro = allow_macro
        self.template = rule.template if rule else None
        self.image_path = rule.image if rule else None  # None until a new snip is saved
        # The image the template is cropped from (a whole-screen snip, a loaded file, or
        # the existing template), where it sits on screen if known, and the crop box.
        self.source = self.template
        self.source_origin = None
        self.crop_rect = ((0, 0, self.template.shape[1], self.template.shape[0])
                          if self.template is not None else None)
        self.snip_region = None
        self.region = rule.region if rule else None
        self.photo = None
        self.recording = False

        r = rule or im.Rule(name="", image=Path(), template=None, action=default_action)
        self.name = tk.StringVar(value=r.name or (f"Rule {len(app.cfg.rules) + 1}" if kind == "rule"
                                                  else kind.capitalize()))
        self.enabled = tk.BooleanVar(value=r.enabled)
        self.action = tk.StringVar(value=ACTION_LABELS[r.action])
        self.button = tk.StringVar(value=r.button)
        self.key = tk.StringVar(value=r.key)
        self.offset_x = tk.StringVar(value=str(r.offset[0]))
        self.offset_y = tk.StringVar(value=str(r.offset[1]))
        self.restore = tk.BooleanVar(value=r.restore_cursor)
        self.hold = tk.StringVar(value=f"{r.hold:g}")
        self.spam = tk.BooleanVar(value=r.repeat > 1)
        self.repeat = tk.StringVar(value=str(r.repeat if r.repeat > 1 else 10))
        self.repeat_gap = tk.StringVar(value=f"{r.repeat_gap:g}")
        self.path = list(r.path)  # a recorded drag; only editable by removing it
        # Let go of a hold when this appears; the image path is None until a new snip is saved.
        self.release_template = r.release_template
        self.release_path = r.release_image
        self.release_region = r.release_region
        self.release_threshold = tk.StringVar(value=f"{r.release_threshold:g}")
        self.release_delay = tk.StringVar(value=f"{r.release_delay * 1000:.0f}")
        self.release_photo = None
        self.threshold = tk.DoubleVar(value=r.threshold)
        self.cooldown = tk.StringVar(value=f"{r.cooldown:g}")
        self.pause_after = tk.StringVar(value=f"{r.pause_after:g}")
        self.delay = tk.StringVar(value="0")
        # What to look for, besides an image: a colour (or one pixel of it), or text.
        self.detect = tk.StringVar(value=DETECT_LABELS[r.detect])
        self.color = tuple(r.color)
        self.color_point = None  # where the colour was picked on screen, for "Only that pixel"
        self.tolerance = tk.StringVar(value=str(r.tolerance))
        self.min_area = tk.StringVar(value=str(r.min_area))
        self.text = tk.StringVar(value=r.text)
        self.multiscale = tk.BooleanVar(value=r.multiscale)
        self.edges = tk.BooleanVar(value=r.edges)
        self.macro = tk.StringVar(value=r.macro)
        if r.action == "macro" and not allow_macro:
            self.action.set(ACTION_LABELS["none"])

        self.title(f"Edit {kind}" if rule else f"New {kind}")
        self.transient(app.root)
        self.resizable(False, False)
        self.build()
        self.update_template()
        self.update_region()
        self.update_detect()
        self.update_action()
        self.bind("<Escape>", lambda e: self.cancel_recording() if self.recording else self.destroy())
        self.bind("<Return>", lambda e: self.on_record(e) if self.recording else self.ok())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.name_entry.focus_set()
        self.name_entry.select_range(0, "end")
        app.root.wait_window(self)

    def build(self):
        pad = {"padx": 10, "pady": (8, 0)}
        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Name").pack(side="left")
        self.name_entry = ttk.Entry(top, textvariable=self.name, width=40)
        self.name_entry.pack(side="left", padx=(8, 12), fill="x", expand=True)
        ttk.Checkbutton(top, text="On", variable=self.enabled).pack(side="left")

        # 1. What to look for
        what = ttk.LabelFrame(self, text="1. What to look for", padding=8)
        what.pack(fill="x", **pad)
        kinds = ttk.Frame(what)
        kinds.pack(fill="x")
        for label in DETECT_LABELS.values():
            ttk.Radiobutton(kinds, text=label, value=label, variable=self.detect,
                            command=self.update_detect).pack(side="left", padx=(0, 14))
        self.detect_note = ttk.Label(kinds, foreground=T.MUTED)
        self.detect_note.pack(side="left")
        self.what_frame = what

        look = self.image_frame = ttk.Frame(what)
        self.template_label = ttk.Label(look, anchor="center", relief="solid", padding=4,
                                        cursor="hand2")
        self.template_label.grid(row=0, column=0, rowspan=5, sticky="nsew", padx=(0, 10))
        self.template_label.bind("<Button-1>", lambda e: self.adjust_crop())
        look.columnconfigure(0, minsize=300)
        ttk.Button(look, text="Snip from screen…", command=self.snip).grid(row=0, column=1, sticky="ew")
        ttk.Button(look, text="Load image file…", command=self.load_image).grid(
            row=1, column=1, sticky="ew", pady=(4, 0))
        self.crop_btn = ttk.Button(look, text="Adjust crop…", command=self.adjust_crop)
        self.crop_btn.grid(row=2, column=1, sticky="ew", pady=(4, 0))
        delay = ttk.Frame(look)
        delay.grid(row=3, column=1, sticky="w", pady=(6, 0))
        ttk.Label(delay, text="Wait before snip (s)").pack(side="left")
        ttk.Spinbox(delay, textvariable=self.delay, from_=0, to=10, width=4).pack(side="left", padx=4)
        self.template_info = ttk.Label(look, foreground=T.MUTED, wraplength=200, justify="left")
        self.template_info.grid(row=4, column=1, sticky="nw", pady=(6, 0))
        extra = ttk.Frame(look)
        extra.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(extra, text="Also find it at other sizes (70–130%)",
                        variable=self.multiscale).pack(side="left")
        ttk.Checkbutton(extra, text="Also by its outline, if its colours change",
                        variable=self.edges).pack(side="left", padx=(14, 0))
        ttk.Label(look, text="Both only kick in when the plain match misses, and take longer: "
                             "give the rule a search area.",
                  foreground=T.MUTED).grid(row=6, column=0, columnspan=2, sticky="w")

        # A colour: pick it from the screen; a patch of it at least min_area big counts.
        self.color_frame = cf = ttk.Frame(what)
        self.swatch = tk.Label(cf, width=6, height=2, relief="solid", bd=1)
        self.swatch.grid(row=0, column=0, rowspan=3, sticky="nsw", padx=(0, 10))
        self.color_label = ttk.Label(cf, width=10)
        self.color_label.grid(row=0, column=1, sticky="w")
        ttk.Button(cf, text="Pick colour from screen…", command=self.pick_color).grid(
            row=0, column=2, sticky="w", padx=(6, 0))
        self.pixel_btn = ttk.Button(cf, text="Only that pixel", command=self.region_one_pixel)
        self.pixel_btn.grid(row=0, column=3, sticky="w", padx=(6, 0))
        ttk.Label(cf, text="Each of red, green, blue within ±").grid(row=1, column=1, columnspan=2,
                                                                   sticky="w", pady=(6, 0))
        ttk.Spinbox(cf, textvariable=self.tolerance, from_=0, to=255, width=5).grid(
            row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Label(cf, text="A patch of at least (px)").grid(row=2, column=1, columnspan=2,
                                                            sticky="w", pady=(6, 0))
        ttk.Spinbox(cf, textvariable=self.min_area, from_=1, to=1000000, width=7).grid(
            row=2, column=3, sticky="w", pady=(6, 0))
        ttk.Label(cf, text="Click a pixel to pick its colour, or drag a box to take the most common "
                           "one. For pixel detection, use “Only that pixel” and a patch of 1. "
                           "The rule acts on the middle of the biggest patch.",
                  foreground=T.MUTED, wraplength=560, justify="left").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        # Text: read by Windows' OCR.
        self.text_frame = tf = ttk.Frame(what)
        ttk.Label(tf, text="Text to find").grid(row=0, column=0, sticky="w")
        ttk.Entry(tf, textvariable=self.text, width=40).grid(row=0, column=1, sticky="w", padx=8)
        self.ocr_note = ttk.Label(tf, foreground=T.MUTED, wraplength=560, justify="left")
        self.ocr_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 2. Where to look
        where = ttk.LabelFrame(self, text="2. Where to look", padding=8)
        where.pack(fill="x", **pad)
        self.region_label = ttk.Label(where, width=34)
        self.region_label.pack(side="left")
        ttk.Button(where, text="Whole screen", command=self.clear_region).pack(side="right")
        self.near_btn = ttk.Button(where, text="Near the snip", command=self.region_near_snip)
        self.near_btn.pack(side="right", padx=4)
        ttk.Button(where, text="Pick area…", command=self.pick_region).pack(side="right")

        # 3. What to do
        do = ttk.LabelFrame(self, text="3. When it appears", padding=8)
        do.pack(fill="x", **pad)
        actions = [label for label, a in LABEL_ACTIONS.items() if a != "macro" or self.allow_macro]
        action = ttk.Combobox(do, textvariable=self.action, values=actions,
                              state="readonly", width=22)
        action.grid(row=0, column=0, sticky="w")
        action.bind("<<ComboboxSelected>>", lambda e: self.update_action())

        self.click_frame = ttk.Frame(do)
        ttk.Label(self.click_frame, text="Button").pack(side="left")
        ttk.Combobox(self.click_frame, textvariable=self.button, values=list(im.MOUSE_BUTTONS),
                     state="readonly", width=7).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(self.click_frame, text="Put the mouse back afterwards",
                        variable=self.restore).pack(side="left")

        self.spam_frame = ttk.Frame(do)
        self.spam_check = ttk.Checkbutton(self.spam_frame, text="Spam: click", variable=self.spam,
                                          command=self.update_spam)
        self.spam_check.pack(side="left")
        self.repeat_spin = ttk.Spinbox(self.spam_frame, textvariable=self.repeat, from_=2, to=10000,
                                       increment=1, width=6, command=self.update_spam)
        self.repeat_spin.pack(side="left", padx=4)
        ttk.Label(self.spam_frame, text="times,").pack(side="left")
        self.gap_spin = ttk.Spinbox(self.spam_frame, textvariable=self.repeat_gap, from_=0, to=60,
                                    increment=0.05, width=6, command=self.update_spam)
        self.gap_spin.pack(side="left", padx=4)
        self.gap_label = ttk.Label(self.spam_frame, text="s between clicks")
        self.gap_label.pack(side="left")
        self.spam_info = ttk.Label(self.spam_frame, foreground=T.MUTED)
        self.spam_info.pack(side="left", padx=8)
        for var in (self.repeat, self.repeat_gap, self.hold):
            var.trace_add("write", lambda *a: self.update_spam())

        self.hold_frame = ttk.Frame(do)
        self.hold_label = ttk.Label(self.hold_frame, text="Hold the button for (s)")
        self.hold_label.pack(side="left")
        ttk.Spinbox(self.hold_frame, textvariable=self.hold, from_=0, to=600, increment=0.1,
                    width=6).pack(side="left", padx=4)
        self.path_label = ttk.Label(self.hold_frame, foreground=T.MUTED)
        self.path_label.pack(side="left", padx=8)
        self.clear_path_btn = ttk.Button(self.hold_frame, text="Remove drag",
                                         command=self.clear_path)

        self.release_frame = ttk.Frame(do)
        ttk.Label(self.release_frame, text="Let go when this appears").pack(side="left")
        self.release_label = ttk.Label(self.release_frame, relief="solid", padding=2,
                                       foreground=T.MUTED)
        self.release_label.pack(side="left", padx=6)
        ttk.Button(self.release_frame, text="Snip release image…",
                   command=self.snip_release).pack(side="left")
        self.release_remove = ttk.Button(self.release_frame, text="Remove",
                                         command=self.clear_release)
        self.release_remove.pack(side="left", padx=4)
        self.release_sim = ttk.Frame(self.release_frame)
        self.release_sim.pack(side="left", padx=(8, 0))
        ttk.Label(self.release_sim, text="similarity").pack(side="left")
        ttk.Spinbox(self.release_sim, textvariable=self.release_threshold, from_=0.5, to=1.0,
                    increment=0.01, width=5).pack(side="left", padx=4)
        ttk.Label(self.release_sim, text="then wait (ms)").pack(side="left", padx=(8, 0))
        ttk.Spinbox(self.release_sim, textvariable=self.release_delay, from_=0, to=5000,
                    increment=10, width=6).pack(side="left", padx=4)

        self.key_frame = ttk.Frame(do)
        ttk.Label(self.key_frame, text="Key").pack(side="left")
        ttk.Entry(self.key_frame, textvariable=self.key, width=16).pack(side="left", padx=4)
        self.record_btn = ttk.Button(self.key_frame, text="Record…", command=self.record_key)
        self.record_btn.pack(side="left")
        ttk.Label(self.key_frame, text="e.g. space, e, ctrl+s, F5, grave, num5",
                  foreground=T.MUTED).pack(side="left", padx=8)

        self.offset_frame = ttk.Frame(do)
        ttk.Label(self.offset_frame, text="Aim offset from the image centre:  x").pack(side="left")
        ttk.Spinbox(self.offset_frame, textvariable=self.offset_x, from_=-5000, to=5000,
                    width=6).pack(side="left", padx=4)
        ttk.Label(self.offset_frame, text="y").pack(side="left")
        ttk.Spinbox(self.offset_frame, textvariable=self.offset_y, from_=-5000, to=5000,
                    width=6).pack(side="left", padx=4)

        self.macro_frame = ttk.Frame(do)
        ttk.Label(self.macro_frame, text="Macro").pack(side="left")
        ttk.Combobox(self.macro_frame, textvariable=self.macro, state="readonly", width=28,
                     values=[m.name for m in self.app.macros]).pack(side="left", padx=6)
        ttk.Label(self.macro_frame, text="plays it with its own speed, repeats and safeguards, "
                                         "then carries on", foreground=T.MUTED).pack(side="left")

        # 4. Matching
        match = ttk.LabelFrame(self, text="4. Matching", padding=8)
        match.pack(fill="x", **pad)
        self.threshold_widgets = [
            ttk.Label(match, text="Similarity needed"),
            ttk.Scale(match, variable=self.threshold, from_=0.5, to=1.0, length=220,
                      command=lambda v: self.threshold_label.configure(text=f"{float(v):.2f}")),
            ttk.Label(match, text=f"{self.threshold.get():.2f}", width=5),
            ttk.Label(match, text="lower = looser, higher = stricter", foreground=T.MUTED)]
        self.threshold_label = self.threshold_widgets[2]
        for column, w in enumerate(self.threshold_widgets):
            w.grid(row=0, column=column, sticky="w", padx=6 if column == 1 else 0)
        ttk.Label(match, text="Wait after acting (s)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(match, textvariable=self.cooldown, from_=0, to=3600, increment=0.5,
                    width=6).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(match, text="Pause after acting (s)").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(match, textvariable=self.pause_after, from_=0, to=3600, increment=0.5,
                    width=6).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(match, text="check no rule for this long afterwards (Watch only)",
                  foreground=T.MUTED).grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))
        test = ttk.Frame(match)
        test.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(test, text="Test on screen now", command=self.test).pack(side="left")
        self.test_label = ttk.Label(test, text="")
        self.test_label.pack(side="left", padx=10)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="OK", command=self.ok).pack(side="right", padx=6)

    # --- template and region ----------------------------------------------------

    def update_template(self):
        if self.template is None:
            self.photo = None
            self.template_label.configure(image="", text="No image yet.\n\nSnip the thing to "
                                                         "look for, e.g. a button or icon.")
            self.template_info.configure(text="")
            self.crop_btn.configure(state="disabled")
            return
        self.crop_btn.configure(state="normal")
        self.photo = to_photo(self, self.template, 290, 150)
        self.template_label.configure(image=self.photo, text="")
        h, w = self.template.shape[:2]
        source = "new snip (saved on OK)" if self.image_path is None else self.image_path.name
        self.template_info.configure(text=f"{w}×{h} px\n{source}\nClick the image to adjust the crop.")

    def update_region(self):
        self.region_label.configure(text=describe_region(self.region))
        self.near_btn.configure(state="normal" if self.snip_region else "disabled")

    def delay_seconds(self):
        try:
            return max(float(self.delay.get()), 0)
        except ValueError:
            return 0

    def set_crop(self, rect, new_file=True):
        """Make the template the `rect` part of the source image."""
        x0, y0, x1, y1 = rect
        self.crop_rect = rect
        self.template = self.source[y0:y1, x0:x1].copy()
        if self.source_origin:
            ox, oy = self.source_origin
            self.snip_region = (ox + x0, oy + y0, x1 - x0, y1 - y0)
        if new_file:
            self.image_path = None  # saved as a new template file on OK
        self.update_template()
        self.update_region()

    def adjust_crop(self):
        if self.source is None:
            return
        rect = CropDialog(self, self.source, self.crop_rect).result
        self.grab_set()  # the crop dialog took the grab; take it back
        if rect and rect != self.crop_rect:
            self.set_crop(rect)

    def snip(self):
        picked = self.app.while_hidden(lambda: im.select_region(self.app.root), self.delay_seconds())
        if picked:
            # Keep the whole screenshot so the crop can be adjusted afterwards.
            self.source, self.source_origin = picked.frame, picked.origin
            left, top, width, height = picked.region
            x0, y0 = left - picked.origin[0], top - picked.origin[1]
            self.set_crop((x0, y0, x0 + width, y0 + height))

    def load_image(self):
        path = filedialog.askopenfilename(parent=self, initialdir=self.app.config_dir(),
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp")])
        if path:
            try:
                image = im.read_image(path)
            except ValueError as exc:
                messagebox.showerror("Couldn't load image", str(exc), parent=self)
                return
            self.source, self.source_origin, self.snip_region = image, None, None
            self.image_path = Path(path).resolve()
            self.set_crop((0, 0, image.shape[1], image.shape[0]), new_file=False)

    def pick_region(self):
        picked = self.app.while_hidden(lambda: im.select_region(self.app.root), self.delay_seconds())
        if picked:
            self.region = tuple(picked.region)
            self.update_region()

    def region_near_snip(self):
        left, top, width, height = self.snip_region
        pad = 100
        self.region = (left - pad, top - pad, width + 2 * pad, height + 2 * pad)
        self.update_region()

    def clear_region(self):
        self.region = None
        self.update_region()

    # --- action fields ----------------------------------------------------------

    def detect_kind(self):
        return next(k for k, label in DETECT_LABELS.items() if label == self.detect.get())

    def update_detect(self):
        """Show the settings for what's being looked for: an image, a colour or text."""
        kind = self.detect_kind()
        for frame in (self.image_frame, self.color_frame, self.text_frame):
            frame.pack_forget()
        {"image": self.image_frame, "color": self.color_frame,
         "text": self.text_frame}[kind].pack(fill="x", pady=(8, 0))
        for w in self.threshold_widgets:
            if kind == "color":
                w.grid_remove()
            else:
                w.grid()
        self.detect_note.configure(text={
            "image": "a button, an icon, a warning…",
            "color": "a colour, or a single pixel of it",
            "text": "words on screen, read by Windows"}[kind])
        if kind == "color":
            self.update_color()
        elif kind == "text":
            import imgocr
            problem = imgocr.available()
            self.ocr_note.configure(
                foreground=T.BAD if problem else T.MUTED,
                text=(f"Text detection can't run yet: {problem}." if problem else
                      f"Read by Windows' own OCR ({imgocr.language()}), on this computer. Any case; "
                      "spaces and punctuation are ignored. The similarity below allows a misread "
                      "letter or two. Give it a search area: reading the whole screen is slow."))
        self.update_region()

    def update_color(self):
        self.swatch.configure(background=im.color_hex(self.color))
        self.color_label.configure(text=im.color_hex(self.color))
        self.pixel_btn.configure(state="normal" if self.color_point else "disabled")

    def pick_color(self):
        picked = self.app.while_hidden(lambda: im.select_region(self.app.root, point=True),
                                       self.delay_seconds())
        if not picked:
            return
        pixels = picked.crop.reshape(-1, 3)
        # The most common colour in the box (for one pixel, just that one).
        values, counts = np.unique(pixels, axis=0, return_counts=True)
        b, g, r = (int(v) for v in values[counts.argmax()])
        self.color = (r, g, b)
        left, top, width, height = picked.region
        self.color_point = (left + width // 2, top + height // 2)
        self.snip_region = tuple(picked.region)
        self.update_color()
        self.update_region()

    def region_one_pixel(self):
        x, y = self.color_point
        self.region = (x, y, 1, 1)
        self.min_area.set("1")
        self.update_region()

    def update_action(self):
        action = LABEL_ACTIONS[self.action.get()]
        for frame in (self.click_frame, self.spam_frame, self.hold_frame, self.release_frame,
                      self.key_frame, self.offset_frame, self.macro_frame):
            frame.grid_forget()
        if action == "macro":
            self.macro_frame.grid(row=1, column=0, sticky="w", pady=(6, 0))
        if action in ("click", "key"):
            first = self.click_frame if action == "click" else self.key_frame
            first.grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.spam_frame.grid(row=2, column=0, sticky="w", pady=(6, 0))
            self.hold_frame.grid(row=3, column=0, sticky="w", pady=(6, 0))
            self.release_frame.grid(row=4, column=0, sticky="w", pady=(6, 0))
            self.update_path()
            self.update_release()
            self.update_spam()
        if action in ("click", "move"):
            self.offset_frame.grid(row=5, column=0, sticky="w", pady=(6, 0))

    def holding(self):
        try:
            return float(self.hold.get() or 0) > 0
        except ValueError:
            return False

    def update_spam(self):
        """Spam needs a quick click/press: it's switched off while there's a hold time."""
        if not hasattr(self, "spam_info"):
            return
        action = LABEL_ACTIONS[self.action.get()]
        self.spam_check.configure(text="Spam: click" if action == "click" else "Spam: press it")
        self.gap_label.configure(text="s between clicks" if action == "click" else "s between presses")
        if self.holding():
            self.spam_check.configure(state="disabled")
            for w in (self.repeat_spin, self.gap_spin):
                w.configure(state="disabled")
            self.spam_info.configure(text="(not with a hold: set the hold time to 0)")
            return
        self.spam_check.configure(state="normal")
        on = self.spam.get()
        for w in (self.repeat_spin, self.gap_spin):
            w.configure(state="normal" if on else "disabled")
        try:
            n, gap = int(self.repeat.get()), float(self.repeat_gap.get())
        except ValueError:
            self.spam_info.configure(text="")
            return
        if on and n > 1:
            rate = f"{1 / gap:.0f} per second, " if gap > 0 else ""
            self.spam_info.configure(text=f"({rate}takes {(n - 1) * gap:.2f} s; {self.app.cfg.stop_key} stops it)")
        else:
            self.spam_info.configure(text="(off: one click per match)" if action == "click"
                                     else "(off: one press per match)")

    def update_release(self):
        if self.release_template is None:
            self.release_photo = None
            self.release_label.configure(image="", text="nothing: holds for the time above")
            self.hold_label.configure(text="Hold the button for (s)")
            self.update_path()
            self.release_remove.configure(state="disabled")
            self.release_sim.pack_forget()
            return
        self.release_photo = to_photo(self, self.release_template, 160, 48)
        self.release_label.configure(image=self.release_photo, text="")
        self.hold_label.configure(text="Hold for at most (s)")
        if not self.path:
            self.path_label.configure(text="")
        self.release_remove.configure(state="normal")
        self.release_sim.pack(side="left", padx=(8, 0))

    def snip_release(self):
        picked = self.app.while_hidden(lambda: im.select_region(self.app.root), self.delay_seconds())
        if not picked:
            return
        pad = RecordingReview.RELEASE_PAD
        left, top, width, height = picked.region
        self.release_template = picked.crop
        self.release_path = None  # saved on OK
        # Only look where it was snipped, so a moving bar can't match early somewhere else.
        self.release_region = (left - pad, top - pad, width + 2 * pad, height + 2 * pad)
        try:
            hold = float(self.hold.get() or 0)
        except ValueError:
            hold = 0
        if hold <= 0:
            self.hold.set("10")  # the longest to wait for it
        self.update_release()

    def clear_release(self):
        self.release_template = self.release_path = self.release_region = None
        self.update_release()

    def update_path(self):
        if LABEL_ACTIONS[self.action.get()] == "key":
            self.path_label.configure(text="0 = a quick press")
            self.clear_path_btn.pack_forget()
        elif self.path:
            t, dx, dy = self.path[-1]
            self.path_label.configure(text=f"and drag along the recorded path ({len(self.path)} "
                                           f"points, {t:g} s, ends {dx:+d}, {dy:+d} px away)")
            self.clear_path_btn.pack(side="left")
        else:
            self.path_label.configure(text="0 = a normal click")
            self.clear_path_btn.pack_forget()

    def clear_path(self):
        try:
            hold = float(self.hold.get() or 0)
        except ValueError:
            hold = 0
        if self.path and not hold:
            self.hold.set(f"{self.path[-1][0]:g}")  # keep holding as long as the drag took
        self.path = []
        self.update_path()

    def record_key(self):
        self.recording = True
        self.record_btn.configure(text="Press a key…")
        self.focus_set()  # the dialog itself, so the keypress doesn't type into an entry
        self.bind("<KeyPress>", self.on_record)

    def cancel_recording(self):
        self.recording = False
        self.record_btn.configure(text="Record…")
        self.unbind("<KeyPress>")

    def on_record(self, event):
        if event.keysym == "Escape":
            return "break"  # handled by the <Escape> binding
        if event.keycode in MODIFIER_VKS:
            return "break"  # wait for the main key
        name = VK_TO_NAME.get(event.keycode)
        if name is None:
            self.test_label.configure(text=f"“{event.keysym}” isn't supported; type a key name.")
        else:
            mods = [m for m, bit in (("ctrl", 0x4), ("shift", 0x1), ("alt", 0x20000))
                    if event.state & bit]
            self.key.set("+".join(mods + [name]))
        self.cancel_recording()
        return "break"

    # --- result -----------------------------------------------------------------

    def build_rule(self):
        """The rule described by the form -> Rule, raising ValueError with a readable message."""
        detect = self.detect_kind()
        if detect == "image" and self.template is None:
            raise ValueError("Snip or load the image to look for first.")
        if detect == "text" and not self.text.get().strip():
            raise ValueError("Type the text to look for first.")
        try:
            tolerance, min_area = int(self.tolerance.get() or 0), int(self.min_area.get() or 1)
        except ValueError:
            raise ValueError("The colour tolerance and patch size must be whole numbers.") from None
        name = self.name.get().strip() or "Unnamed rule"
        try:
            offset = (int(self.offset_x.get() or 0), int(self.offset_y.get() or 0))
        except ValueError:
            raise ValueError("The aim offset must be whole numbers.") from None
        try:
            cooldown = float(self.cooldown.get() or 0)
        except ValueError:
            raise ValueError("The wait after acting must be a number of seconds.") from None
        try:
            pause_after = max(float(self.pause_after.get() or 0), 0.0)
        except ValueError:
            raise ValueError("The pause after acting must be a number of seconds.") from None
        try:
            hold = float(self.hold.get() or 0)
        except ValueError:
            raise ValueError("The hold time must be a number of seconds.") from None
        repeat, repeat_gap = 1, 0.1
        if self.spam.get() and hold <= 0:
            try:
                repeat, repeat_gap = int(self.repeat.get()), float(self.repeat_gap.get())
            except ValueError:
                raise ValueError("Spam needs a whole number of clicks and a number of seconds "
                                 "between them.") from None
        try:
            release_threshold = float(self.release_threshold.get() or 0.9)
        except ValueError:
            raise ValueError("The release similarity must be a number from 0.5 to 1.") from None
        try:
            release_delay = float(self.release_delay.get() or 0) / 1000
        except ValueError:
            raise ValueError("The wait before letting go must be a number of milliseconds.") from None
        action = LABEL_ACTIONS[self.action.get()]
        if action == "macro" and not self.macro.get():
            raise ValueError("Pick the macro to play" + ("." if self.app.macros else
                             ": record one on the Macros tab first."))
        image = detect == "image"
        rule = im.Rule(name=name, image=(self.image_path or Path()) if image else Path(),
                       template=self.template if image else None,
                       detect=detect, color=self.color, tolerance=tolerance, min_area=min_area,
                       text=self.text.get().strip(), multiscale=image and self.multiscale.get(),
                       edges=image and self.edges.get(), macro=self.macro.get(),
                       enabled=self.enabled.get(), threshold=round(self.threshold.get(), 3),
                       region=self.region, action=action,
                       button=self.button.get(), offset=offset, key=self.key.get().strip(),
                       cooldown=cooldown, pause_after=pause_after, restore_cursor=self.restore.get(),
                       hold=hold, path=list(self.path) if action == "click" else [],
                       repeat=repeat, repeat_gap=repeat_gap)
        if action in ("click", "key") and self.release_template is not None:
            if rule.motion in ("click", "press"):
                raise ValueError("A release image needs a hold time: the longest to hold "
                                 "while waiting for it.")
            rule.release_image = self.release_path or Path()
            rule.release_template = self.release_template
            rule.release_threshold = release_threshold
            rule.release_delay = release_delay
            rule.release_region = self.release_region
        rule.validate()
        return rule

    def test(self):
        try:
            rule = self.build_rule()
        except ValueError as exc:
            messagebox.showerror("Can't test yet", str(exc), parent=self)
            return
        frame, origin = self.app.while_hidden(capture_desktop)
        try:
            hit = rule.find(frame, origin)
        except ValueError as exc:
            self.test_label.configure(text=str(exc), foreground=T.BAD)
            return
        if hit is None:
            self.test_label.configure(text="The search area is smaller than the image.",
                                      foreground=T.BAD)
            return
        ok = hit.score >= rule.needed
        how = f" ({hit.how})" if hit.how else ""
        text = (f"Best match {hit.score:.3f}{how} at ({hit.x}, {hit.y}): "
                + ("would act ✓" if ok else "no match"))
        if rule.release_template is not None:
            release = rule.find_release(frame, origin)
            rscore = release[0] if release else 0.0
            text += (f"   ·   release image {rscore:.3f}: "
                     + ("would let go" if rscore >= rule.release_threshold else "keeps holding"))
        self.test_label.configure(text=text, foreground=T.GOOD if ok else T.BAD)

    def ok(self):
        try:
            rule = self.build_rule()
        except ValueError as exc:
            messagebox.showerror("Rule isn't finished", str(exc), parent=self)
            return
        if rule.detect == "image" and self.image_path is None:  # a new snip or crop: save it
            try:
                rule.image = self.app.save_template(rule.name, self.template)
            except OSError as exc:
                messagebox.showerror("Couldn't save the image", str(exc), parent=self)
                return
        if rule.release_template is not None and self.release_path is None:
            try:
                rule.release_image = self.app.save_template(f"{rule.name} release",
                                                            rule.release_template)
            except OSError as exc:
                messagebox.showerror("Couldn't save the image", str(exc), parent=self)
                return
        self.result = rule
        self.destroy()


class CropCanvas(ttk.Frame):
    """An image with a crop box you can draw, move and resize, built for picking a
    small button out of a whole-desktop screenshot.

    Drag on the image to draw a new box; drag inside it to move it, or on an edge
    or corner to resize it. The wheel zooms around the pointer, right- or
    middle-drag pans, arrow keys nudge the box a pixel (Shift+arrows resize it).
    Only the visible part of the image is scaled and redrawn, so large
    screenshots stay responsive.
    """

    GRIP = 7  # how close (screen px) the pointer must be to an edge to grab it
    CURSORS = {"t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
               "l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
               "tl": "size_nw_se", "br": "size_nw_se", "tr": "size_ne_sw", "bl": "size_ne_sw",
               "move": "fleur", "new": "crosshair"}

    def __init__(self, master, on_change=None, width=760, height=460):
        super().__init__(master)
        self.on_change = on_change
        self.image = None
        self.rect = (0, 0, 1, 1)  # x0, y0, x1, y1 in image pixels (x1/y1 exclusive)
        self.marker = None        # an image point to mark, e.g. where a click happened
        self.trail = []           # image points to draw as a line, e.g. a recorded drag
        self.scale, self.ox, self.oy = 1.0, 0.0, 0.0  # zoom, and the image point at top-left
        self.photo = None
        self.drag = None
        self.pan = None

        self.canvas = tk.Canvas(self, width=width, height=height, background="#1e1e1e",
                                highlightthickness=0, cursor="crosshair", takefocus=1)
        self.canvas.pack(fill="both", expand=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="Fit", command=self.fit).pack(side="left")
        ttk.Button(bar, text="Zoom to crop", command=self.zoom_to_rect).pack(side="left", padx=4)
        ttk.Button(bar, text="100%", command=self.actual_size).pack(side="left")
        self.info = ttk.Label(bar, foreground=T.MUTED)
        self.info.pack(side="left", padx=10)

        c = self.canvas
        c.bind("<Configure>", lambda e: self.render())
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_hover)
        for button in ("2", "3"):
            c.bind(f"<ButtonPress-{button}>", self.on_pan_start)
            c.bind(f"<B{button}-Motion>", self.on_pan)
        c.bind("<MouseWheel>", self.on_wheel)
        for key, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
            c.bind(f"<{key}>", lambda e, dx=dx, dy=dy: self.nudge(dx, dy, resize=False))
            c.bind(f"<Shift-{key}>", lambda e, dx=dx, dy=dy: self.nudge(dx, dy, resize=True))

    # --- public -----------------------------------------------------------------

    def set_image(self, image, rect=None, marker=None, trail=()):
        self.image = image
        self.marker = marker
        self.trail = list(trail)
        self.rect = self.clamp(rect or (0, 0, image.shape[1], image.shape[0]))
        self.after_idle(self.zoom_to_rect)

    def set_rect(self, rect):
        self.rect = self.clamp(rect)
        self.zoom_to_rect()
        self.changed()

    def clear(self):
        self.image = None
        self.canvas.delete("all")
        self.info.configure(text="")

    # --- geometry ---------------------------------------------------------------

    def view_size(self):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 2 or h < 2:  # not laid out yet
            w, h = int(self.canvas["width"]), int(self.canvas["height"])
        return w, h

    def to_canvas(self, ix, iy):
        return (ix - self.ox) * self.scale, (iy - self.oy) * self.scale

    def to_image(self, cx, cy):
        return cx / self.scale + self.ox, cy / self.scale + self.oy

    def clamp(self, rect):
        """Round to whole pixels, order the corners, keep inside the image, at least 1 px."""
        h, w = self.image.shape[:2]
        x0, y0, x1, y1 = (int(round(v)) for v in rect)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0, y0 = min(max(x0, 0), w - 1), min(max(y0, 0), h - 1)
        return x0, y0, min(max(x1, x0 + 1), w), min(max(y1, y0 + 1), h)

    def show(self, scale, cx, cy):
        """Zoom to `scale`, centred on image point (cx, cy)."""
        if self.image is None:
            return
        w, h = self.view_size()
        self.scale = min(max(scale, 0.02), 32.0)
        self.ox, self.oy = cx - w / 2 / self.scale, cy - h / 2 / self.scale
        self.render()

    def fit(self):
        if self.image is None:
            return
        h, w = self.image.shape[:2]
        vw, vh = self.view_size()
        self.show(min(vw / w, vh / h) * 0.98, w / 2, h / 2)

    def zoom_to_rect(self):
        """Show the crop box large, with some of its surroundings for context."""
        if self.image is None:
            return
        x0, y0, x1, y1 = self.rect
        if self.trail:  # keep a recorded drag in view too
            xs, ys = [p[0] for p in self.trail], [p[1] for p in self.trail]
            x0, y0, x1, y1 = min(x0, *xs), min(y0, *ys), max(x1, *xs), max(y1, *ys)
        vw, vh = self.view_size()
        pad = max(x1 - x0, y1 - y0, 40)
        self.show(min(vw / (x1 - x0 + 2 * pad), vh / (y1 - y0 + 2 * pad), 8.0),
                  (x0 + x1) / 2, (y0 + y1) / 2)

    def actual_size(self):
        x0, y0, x1, y1 = self.rect
        self.show(1.0, (x0 + x1) / 2, (y0 + y1) / 2)

    # --- drawing ----------------------------------------------------------------

    def render(self):
        if self.image is None:
            return
        c = self.canvas
        vw, vh = self.view_size()
        h, w = self.image.shape[:2]
        s = self.scale
        x0, y0 = max(int(self.ox), 0), max(int(self.oy), 0)
        x1, y1 = min(int(self.ox + vw / s) + 2, w), min(int(self.oy + vh / s) + 2, h)
        c.delete("image")
        if x1 > x0 and y1 > y0:
            part = self.image[y0:y1, x0:x1]
            size = (max(1, round((x1 - x0) * s)), max(1, round((y1 - y0) * s)))
            # Nearest-neighbour when zoomed in, so individual pixels stay crisp for exact crops.
            part = cv2.resize(part, size, interpolation=cv2.INTER_NEAREST if s >= 1 else cv2.INTER_AREA)
            self.photo = tk.PhotoImage(master=c, data=im.ppm_data(part), format="PPM")
            c.create_image(*self.to_canvas(x0, y0), anchor="nw", image=self.photo, tags="image")
            c.tag_lower("image")
        self.draw_overlay()

    def draw_overlay(self):
        c = self.canvas
        c.delete("overlay")
        vw, vh = self.view_size()
        x0, y0 = self.to_canvas(*self.rect[:2])
        x1, y1 = self.to_canvas(*self.rect[2:])
        # Dim everything outside the crop.
        cx0, cy0 = min(max(x0, 0), vw), min(max(y0, 0), vh)
        cx1, cy1 = min(max(x1, 0), vw), min(max(y1, 0), vh)
        for box in ((0, 0, vw, cy0), (0, cy1, vw, vh), (0, cy0, cx0, cy1), (cx1, cy0, vw, cy1)):
            if box[2] > box[0] and box[3] > box[1]:
                c.create_rectangle(*box, fill="black", stipple="gray50", outline="", tags="overlay")
        c.create_rectangle(x0, y0, x1, y1, outline="#ffd400", width=2, tags="overlay")
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1), (mx, y0), (mx, y1), (x0, my), (x1, my)):
            c.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill="#ffd400", outline="black",
                               tags="overlay")
        if len(self.trail) > 1:
            points = [v for x, y in self.trail for v in self.to_canvas(x + 0.5, y + 0.5)]
            c.create_line(*points, fill="black", width=5, tags="overlay")
            c.create_line(*points, fill="#00d0ff", width=2, arrow="last", arrowshape=(12, 14, 5),
                          tags="overlay")
        if self.marker:
            px, py = self.to_canvas(self.marker[0] + 0.5, self.marker[1] + 0.5)
            for dx, dy in ((1, 0), (0, 1)):
                c.create_line(px - 12 * dx, py - 12 * dy, px + 12 * dx, py + 12 * dy,
                              fill="black", width=4, tags="overlay")
                c.create_line(px - 12 * dx, py - 12 * dy, px + 12 * dx, py + 12 * dy,
                              fill="#ff3030", width=2, tags="overlay")
        rx0, ry0, rx1, ry1 = self.rect
        self.info.configure(text=f"Crop {rx1 - rx0}×{ry1 - ry0} px at ({rx0}, {ry0})"
                                 f"   ·   zoom {self.scale * 100:.0f}%")

    # --- mouse and keys ---------------------------------------------------------

    def grab_mode(self, cx, cy):
        """What a press at canvas (cx, cy) grabs: an edge/corner, the box, or nothing ("new")."""
        x0, y0 = self.to_canvas(*self.rect[:2])
        x1, y1 = self.to_canvas(*self.rect[2:])
        g = self.GRIP
        if not (x0 - g <= cx <= x1 + g and y0 - g <= cy <= y1 + g):
            return "new"
        edges = ""
        if abs(cy - y0) <= g:
            edges += "t"
        elif abs(cy - y1) <= g:
            edges += "b"
        if abs(cx - x0) <= g:
            edges += "l"
        elif abs(cx - x1) <= g:
            edges += "r"
        if edges:
            return edges
        return "move" if x0 < cx < x1 and y0 < cy < y1 else "new"

    def on_hover(self, e):
        if self.image is not None and self.drag is None:
            self.canvas.configure(cursor=self.CURSORS[self.grab_mode(e.x, e.y)])

    def on_press(self, e):
        self.canvas.focus_set()
        if self.image is not None:
            self.drag = (self.grab_mode(e.x, e.y), self.to_image(e.x, e.y), self.rect)

    def on_drag(self, e):
        if self.drag is None:
            return
        mode, (sx, sy), (x0, y0, x1, y1) = self.drag
        ix, iy = self.to_image(e.x, e.y)
        if mode == "new":
            self.rect = self.clamp((sx, sy, ix, iy))
        elif mode == "move":
            h, w = self.image.shape[:2]
            dx = min(max(round(ix - sx), -x0), w - x1)
            dy = min(max(round(iy - sy), -y0), h - y1)
            self.rect = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        else:
            dx, dy = ix - sx, iy - sy
            self.rect = self.clamp((x0 + dx if "l" in mode else x0, y0 + dy if "t" in mode else y0,
                                    x1 + dx if "r" in mode else x1, y1 + dy if "b" in mode else y1))
        self.draw_overlay()

    def on_release(self, e):
        if self.drag is None:
            return
        mode, _, before = self.drag
        self.drag = None
        x0, y0, x1, y1 = self.rect
        if mode == "new" and (x1 - x0 < 3 or y1 - y0 < 3):
            self.rect = before  # a plain click, not a drag: keep the old box
        self.draw_overlay()
        self.changed()

    def on_pan_start(self, e):
        self.pan = (e.x, e.y, self.ox, self.oy)

    def on_pan(self, e):
        if self.pan and self.image is not None:
            x, y, ox, oy = self.pan
            self.ox, self.oy = ox - (e.x - x) / self.scale, oy - (e.y - y) / self.scale
            self.render()

    def on_wheel(self, e):
        if self.image is None:
            return
        ix, iy = self.to_image(e.x, e.y)  # keep the pixel under the pointer where it is
        self.scale = min(max(self.scale * (1.25 if e.delta > 0 else 0.8), 0.02), 32.0)
        self.ox, self.oy = ix - e.x / self.scale, iy - e.y / self.scale
        self.render()

    def nudge(self, dx, dy, resize):
        if self.image is None:
            return "break"
        x0, y0, x1, y1 = self.rect
        if resize:
            self.rect = self.clamp((x0, y0, x1 + dx, y1 + dy))
        else:
            h, w = self.image.shape[:2]
            dx = min(max(dx, -x0), w - x1)
            dy = min(max(dy, -y0), h - y1)
            self.rect = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        self.draw_overlay()
        self.changed()
        return "break"

    def changed(self):
        if self.on_change:
            self.on_change(self.rect)


CROP_HELP = ("Drag to draw a new box  ·  drag the box or its edges to adjust  ·  wheel zooms  ·  "
             "right-drag pans  ·  arrow keys nudge (Shift resizes)")


class CropDialog(tk.Toplevel):
    """Adjust which part of an image becomes the template.
    `.result` is the new (x0, y0, x1, y1) crop, or None if cancelled."""

    def __init__(self, parent, image, rect):
        super().__init__(parent)
        self.result = None
        self.title("Adjust crop")
        self.geometry("1000x700")
        self.minsize(700, 500)
        self.transient(parent)
        ttk.Label(self, text=CROP_HELP, foreground=T.MUTED,
                  padding=(10, 10, 10, 0)).pack(anchor="w")
        self.cropper = CropCanvas(self, width=960, height=560)
        self.cropper.pack(fill="both", expand=True, padx=10, pady=6)
        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Use this crop", command=self.ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.cropper.set_image(image, rect)
        self.grab_set()
        self.cropper.canvas.focus_set()
        parent.wait_window(self)

    def ok(self):
        self.result = self.cropper.rect
        self.destroy()


@dataclass(eq=False)
class ReviewItem:
    capture: im.Capture
    name: str
    rect: tuple       # the crop, in screenshot pixels
    cooldown: float
    include: bool = True
    thumb: object = None
    release_rect: tuple | None = None  # the release-image crop, in release-screenshot pixels
    use_release: bool = False          # let go when the release image appears
    release_frame: int = 0             # which of the release screenshots to crop from


class RecordingReview(tk.Toplevel):
    """Turn recorded clicks into rules: adjust each screenshot's crop, name it, and
    choose which to keep. Each rule clicks exactly where you clicked, relative to
    its image, even if the crop isn't centred on that spot.

    A hold or drag also has a release screenshot, taken just before you let go.
    Cropping something from it (e.g. a charge bar lined up with its mark) makes
    the rule let go when that appears, instead of after a fixed time."""

    NEAR_PAD = 150
    RELEASE_PAD = 4          # px of slack around the release crop when searching for it
    RELEASE_THRESHOLD = 0.9

    def __init__(self, app, captures, as_steps=False):
        super().__init__(app.root)
        self.app = app
        self.as_steps = as_steps  # add "If image appears" steps to the shown flow, not rules
        self.items = [ReviewItem(c, f"Recorded {c.kind} {c.key} {i}" if c.key
                                 else f"Recorded {c.kind} {i}", c.rect,
                                 # a spam-click burst should keep clicking, so no wait
                                 cooldown=0.0 if c.clicks > 1 else 1.0,
                                 release_frame=c.release_choice)
                      for i, c in enumerate(captures, 1)]
        self.current = None
        self.shots = {}  # (item, "press" or a release frame number) -> screenshot, loaded on demand
        self.loading = False
        self.name = tk.StringVar()
        self.cooldown = tk.StringVar()
        self.near = tk.BooleanVar(value=False)
        self.waits = tk.BooleanVar(value=True)  # flows: a Wait step for each pause between captures
        self.view = tk.StringVar(value="press")
        self.use_release = tk.BooleanVar(value=False)
        self.name.trace_add("write", lambda *a: self.on_name())

        self.title("Review recording")
        self.geometry("1260x840")
        self.minsize(980, 680)
        self.transient(app.root)
        self.build()
        self.refresh_list(select=0)
        self.protocol("WM_DELETE_WINDOW", self.discard)
        self.grab_set()
        app.root.wait_window(self)

    def build(self):
        clicks = sum(item.capture.clicks for item in self.items if not item.capture.key)
        keys = sum(item.capture.clicks for item in self.items if item.capture.key)
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)
        inputs = " and ".join(part for n, part in ((clicks, f"{clicks} click(s)"),
                                                    (keys, f"{keys} key press(es)")) if n)
        ttk.Label(body, text=f"{len(self.items)} screenshot(s) from {inputs}. Trim each crop to just "
                             "the thing that tells you to click or press, then add them as rules.",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w",
                                                     pady=(0, 8))

        left = ttk.Frame(body)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        self.tree = ttk.Treeview(left, columns=("add", "clicks"), selectmode="browse")
        self.tree.heading("#0", text="Screenshot")
        self.tree.column("#0", width=200)
        self.tree.heading("add", text="Add")
        self.tree.column("add", width=40, anchor="center")
        self.tree.heading("clicks", text="Input")
        self.tree.column("clicks", width=96, anchor="center")
        self.tree.tag_configure("skip", foreground=T.FAINT)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<space>", lambda e: self.toggle_include())
        self.tree.bind("<Delete>", lambda e: self.delete_item())
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Add / skip", command=self.toggle_include).pack(side="left")
        ttk.Button(buttons, text="Delete", command=self.delete_item).pack(side="left", padx=4)

        right = ttk.Frame(body)
        right.grid(row=1, column=1, sticky="nsew")
        ttk.Label(right, text=CROP_HELP + ".  The red cross is where you pressed.",
                  foreground=T.MUTED).pack(anchor="w")
        # Only shown for holds and drags.
        self.release_bar = ttk.Frame(right)
        ttk.Radiobutton(self.release_bar, text="Press image: hold when this appears",
                        variable=self.view, value="press",
                        command=self.show_view).pack(side="left")
        ttk.Radiobutton(self.release_bar, text="Release image: let go when this appears",
                        variable=self.view, value="release",
                        command=self.show_view).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(self.release_bar, text="Let go when the release image appears",
                        variable=self.use_release,
                        command=self.on_use_release).pack(side="right")
        # Only shown on the release image: step through the screenshots around the release.
        self.frame_bar = ttk.Frame(right)
        ttk.Button(self.frame_bar, text="◀  Earlier", width=11,
                   command=lambda: self.step_frame(-1)).pack(side="left")
        self.frame_label = ttk.Label(self.frame_bar, width=44, anchor="center")
        self.frame_label.pack(side="left", padx=6)
        ttk.Button(self.frame_bar, text="Later  ▶", width=11,
                   command=lambda: self.step_frame(1)).pack(side="left")
        ttk.Label(self.frame_bar, foreground=T.MUTED,
                  text="Pick the one closest to lined up. Earlier ones make the rule wait the difference."
                  ).pack(side="left", padx=10)
        self.cropper = CropCanvas(right, on_change=self.on_crop_change, width=820, height=470)
        self.cropper.pack(fill="both", expand=True, pady=(4, 8))
        form = ttk.Frame(right)
        form.pack(fill="x")
        ttk.Label(form, text="Rule name").pack(side="left")
        ttk.Entry(form, textvariable=self.name, width=30).pack(side="left", padx=(6, 16))
        ttk.Label(form, text="Wait after acting (s)").pack(side="left")
        ttk.Spinbox(form, textvariable=self.cooldown, from_=0, to=3600, increment=0.5,
                    width=6).pack(side="left", padx=6)
        ttk.Button(form, text="Reset crop", command=self.reset_crop).pack(side="right")
        self.info = ttk.Label(right, foreground=T.MUTED, justify="left", wraplength=800)
        self.info.pack(anchor="w", pady=(6, 0))

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Checkbutton(bottom, variable=self.near,
                        text=f"Only search near where I clicked (±{self.NEAR_PAD} px): faster, "
                             "but misses things that move around").pack(side="left")
        ttk.Checkbutton(bottom, variable=self.waits,
                        text="Add the pauses between them as Wait steps" if self.as_steps else
                             "Keep the pauses between them (Pause after acting)"
                        ).pack(side="left", padx=(12, 0))
        self.add_btn = ttk.Button(bottom, text="Add rules", style="Big.TButton",
                                  command=self.add_rules)
        self.add_btn.pack(side="right")
        ttk.Button(bottom, text="Discard recording", command=self.discard).pack(side="right", padx=8)

    # --- list -------------------------------------------------------------------

    def kind_label(self, item):
        c = item.capture
        if c.key:
            if c.hold:
                return f"hold {c.key} → image" if item.use_release else f"hold {c.key} {c.hold:g}s"
            return f"{c.key} ×{c.clicks}" if c.clicks > 1 else c.key
        if c.hold:
            return f"{c.kind} → image" if item.use_release else f"{c.kind} {c.hold:g}s"
        return f"×{c.clicks}" if c.clicks > 1 else "1"

    def refresh_list(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.items):
            if item.thumb is None:
                item.thumb = to_photo(self, item.capture.crop, 64, 32)
            self.tree.insert("", "end", iid=str(i), text="  " + item.name, image=item.thumb,
                             values=("✓" if item.include else "", self.kind_label(item)),
                             tags=() if item.include else ("skip",))
        count = sum(item.include for item in self.items)
        noun = "step" if self.as_steps else "rule"
        self.add_btn.configure(text=f"Add {count} {noun}{'s' if count != 1 else ''}",
                               state="normal" if count else "disabled")
        if self.items and select is not None:
            iid = str(min(select, len(self.items) - 1))
            self.tree.selection_set(iid)
            self.tree.see(iid)
        elif not self.items:
            self.current = None
            self.cropper.clear()
            self.release_bar.pack_forget()
            self.info.configure(text="Nothing left to add.")

    def iid(self, item):
        return str(self.items.index(item))

    def screenshot(self, item, which="press"):
        """The press screenshot, or `which` = "release" for the chosen release screenshot."""
        if which == "release":
            which = item.release_frame
        key = (item, which)
        if key not in self.shots:
            c = item.capture
            self.shots = {k: v for k, v in self.shots.items() if k[0] is item}  # keep memory low
            self.shots[key] = im.read_image(c.path if which == "press" else c.release_shots[which][0])
        return self.shots[key]

    def step_frame(self, delta):
        item = self.current
        if item is None or self.view.get() != "release":
            return
        frame = min(max(item.release_frame + delta, 0), len(item.capture.release_shots) - 1)
        if frame != item.release_frame:
            self.commit()  # keep the crop: the release image is usually in the same place
            if item.release_rect is None:
                item.release_rect = self.cropper.rect
            item.release_frame = frame
            self.show_view(commit=False)

    def release_delay(self, item):
        """A screenshot from before you let go: keep holding that much longer once it appears."""
        return max(-item.capture.release_shots[item.release_frame][1], 0.0)

    def describe_frame(self, item):
        shots = item.capture.release_shots
        t = shots[item.release_frame][1]
        when = f"{abs(t) * 1000:.0f} ms {'before' if t < 0 else 'after'} you let go"
        return f"Screenshot {item.release_frame + 1} of {len(shots)}: {when}"

    def default_release_rect(self, item):
        image = self.screenshot(item, "release")
        x, y = item.capture.release_point
        size = self.app.cfg.record_crop
        return im.crop_around(x, y, size, image.shape[1], image.shape[0])

    def commit(self):
        """Store the form's values on the item being shown."""
        item = self.current
        if item is None:
            return
        try:
            item.cooldown = max(float(self.cooldown.get()), 0.0)
        except ValueError:
            pass
        if self.view.get() == "press":
            item.rect = self.cropper.rect
        elif item.use_release:
            item.release_rect = self.cropper.rect

    def on_select(self):
        selection = self.tree.selection()
        if not selection:
            return
        item = self.items[int(selection[0])]
        if item is self.current:
            return
        self.commit()
        self.current = item
        self.loading = True
        self.name.set(item.name)
        self.cooldown.set(f"{item.cooldown:g}")
        self.use_release.set(item.use_release)
        self.view.set("press")
        self.loading = False
        if item.capture.release_shots:
            self.release_bar.pack(fill="x", pady=(4, 0), before=self.cropper)
        else:
            self.release_bar.pack_forget()
        self.show_view(commit=False)

    def show_view(self, commit=True):
        """Show the press or the release screenshot of the current item."""
        item = self.current
        if item is None:
            return
        if commit:
            self.commit_other_view()
        c = item.capture
        try:
            if self.view.get() == "release":
                image = self.screenshot(item, "release")
                rect = item.release_rect or self.default_release_rect(item)
                self.cropper.set_image(image, rect, marker=c.release_point)
                self.frame_label.configure(text=self.describe_frame(item))
                self.frame_bar.pack(fill="x", pady=(4, 0), before=self.cropper)
            else:
                self.frame_bar.pack_forget()
                image = self.screenshot(item, "press")
                if c.key:  # where the mouse was doesn't matter for a key
                    self.cropper.set_image(image, item.rect)
                else:
                    px, py = c.point
                    trail = [(px, py)] + [(px + dx, py + dy) for _, dx, dy in c.moves]
                    self.cropper.set_image(image, item.rect, marker=(px, py), trail=trail)
        except ValueError as exc:
            messagebox.showerror("Couldn't open the screenshot", str(exc), parent=self)
            return
        self.update_info()

    def commit_other_view(self):
        """Switching views: the cropper still holds the crop of the view we're leaving."""
        item = self.current
        if self.view.get() == "release":  # leaving the press view
            item.rect = self.cropper.rect
        elif item.use_release:
            item.release_rect = self.cropper.rect

    def on_name(self):
        if self.loading or self.current is None:
            return
        self.current.name = self.name.get().strip() or "Recorded click"
        self.tree.item(self.iid(self.current), text="  " + self.current.name)

    def on_use_release(self):
        item = self.current
        if item is None:
            return
        item.use_release = self.use_release.get()
        if item.use_release and self.view.get() != "release":
            self.view.set("release")
            self.show_view()
        elif item.use_release:
            item.release_rect = self.cropper.rect
        self.tree.set(self.iid(item), "clicks", self.kind_label(item))
        self.update_info()

    def toggle_include(self):
        selection = self.tree.selection()
        if selection:
            item = self.items[int(selection[0])]
            item.include = not item.include
            self.refresh_list(select=int(selection[0]))

    def delete_item(self):
        selection = self.tree.selection()
        if selection:
            index = int(selection[0])
            if self.items[index] is self.current:
                self.current = None
            del self.items[index]
            self.refresh_list(select=index)

    # --- crop -------------------------------------------------------------------

    def on_crop_change(self, rect):
        item = self.current
        if item is None:
            return
        if self.view.get() == "release":
            # Cropping the release screenshot means you want to use it.
            item.release_rect = rect
            if not item.use_release:
                item.use_release = True
                self.use_release.set(True)
                self.tree.set(self.iid(item), "clicks", self.kind_label(item))
        else:
            item.rect = rect
            x0, y0, x1, y1 = rect
            item.thumb = to_photo(self, self.screenshot(item)[y0:y1, x0:x1], 64, 32)
            self.tree.item(self.iid(item), image=item.thumb)
        self.update_info()

    def reset_crop(self):
        item = self.current
        if item is None:
            return
        if self.view.get() == "release":
            self.cropper.set_rect(self.default_release_rect(item))
        else:
            self.cropper.set_rect(item.capture.rect)

    def offset_for(self, item):
        """Offset from the template's centre (as the matcher computes it) to the click."""
        x0, y0, x1, y1 = item.rect
        px, py = item.capture.point
        return px - (x0 + (x1 - x0) // 2), py - (y0 + (y1 - y0) // 2)

    def give_up_after(self, item):
        """With a release image, the longest to hold while waiting for it."""
        return max(10.0, round(item.capture.hold * 3, 1))

    def update_info(self):
        item = self.current
        c = item.capture
        if self.view.get() == "release":
            lines = ["These are the screens around the moment you let go. Pick the one where it's "
                     "lined up, then draw a box around what told you to let go, e.g. the bar at its "
                     "mark. Keep it tight around the part that changes, so it only matches at the "
                     "right moment."]
            if item.use_release:
                x0, y0, x1, y1 = self.cropper.rect
                delay = self.release_delay(item)
                then = f", waits {delay * 1000:.0f} ms (as you did after this screenshot)," if delay else ""
                lines.append(f"While holding, the rule watches this {x1 - x0}×{y1 - y0} px box "
                             f"(±{self.RELEASE_PAD} px) and when it looks like this{then} lets go "
                             f"(similarity {self.RELEASE_THRESHOLD:.2f}; if it only gets close, just "
                             "after the closest match). If it never appears, it lets go "
                             f"after {self.give_up_after(item):g} s.")
            else:
                lines.append("Not used yet: crop it, or tick “Let go when the release image "
                             f"appears”. Until then the rule holds for {c.hold:g} s.")
            self.info.configure(text="\n".join(lines))
            return
        x0, y0, x1, y1 = item.rect
        dx, dy = self.offset_for(item)
        release = " until the release image appears" if item.use_release else f" for {c.hold:g} s"
        if c.key:
            does = f"holds {c.key} down{release}" if c.hold else f"presses {c.key}"
            lines = [f"This rule looks for the {x1 - x0}×{y1 - y0} px image in the box and {does}.",
                     "The box starts in the middle of the window you pressed the key in. Move it "
                     "to whatever tells you to press it."]
            if c.release_shots and not item.use_release:
                lines.append("To let go at the right moment instead of after a fixed time, switch to "
                             "“Release image” and crop what tells you to let go.")
            if c.clicks > 1:
                lines.append(f"You pressed {c.key} {c.clicks} times in a burst, so the wait after "
                             "pressing starts at 0 s: the rule keeps pressing it for as long as the "
                             "image is on screen.")
            self.info.configure(text="\n".join(lines))
            return
        does = {"click": f"{c.button}-clicks where you clicked",
                "hold": f"holds the {c.button} button down{release} where you pressed it",
                "drag": f"drags with the {c.button} button the way you did, holding{release}, "
                        "starting where you pressed it"}[c.kind]
        lines = [f"This rule looks for the {x1 - x0}×{y1 - y0} px image in the box and {does} "
                 f"({dx:+d}, {dy:+d} px from the image centre)."]
        if c.moves:
            lines.append("The blue line is the path the mouse took while the button was down. "
                         "It's replayed relative to wherever the image is found.")
        if c.release_shots and not item.use_release:
            lines.append("To let go at the right moment instead of after a fixed time, switch to "
                         "“Release image” and crop what tells you to let go.")
        if c.clicks > 1:
            lines.append(f"You clicked here {c.clicks} times in a burst, so the wait after acting "
                         "starts at 0 s: the rule keeps clicking for as long as the image is on screen.")
        px, py = c.point
        if not (x0 <= px < x1 and y0 <= py < y1):
            lines.append("The box doesn't include the spot you clicked. That's fine: the rule "
                         "still clicks there, relative to wherever the image is found.")
        self.info.configure(text="\n".join(lines))

    # --- finish -----------------------------------------------------------------

    def add_rules(self):
        self.commit()
        rules, releases = [], []
        for item in (i for i in self.items if i.include):
            c = item.capture
            try:
                image = self.screenshot(item)
                release = None
                if item.use_release:
                    rx0, ry0, rx1, ry1 = item.release_rect or self.default_release_rect(item)
                    release = self.screenshot(item, "release")[ry0:ry1, rx0:rx1].copy()
            except ValueError as exc:
                messagebox.showerror("Couldn't open the screenshot", str(exc), parent=self)
                return
            x0, y0, x1, y1 = item.rect
            region = None
            if self.near.get():
                pad = self.NEAR_PAD
                region = (c.origin[0] + x0 - pad, c.origin[1] + y0 - pad,
                          x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
            if c.key:
                rule = im.Rule(name=item.name, image=Path(), template=image[y0:y1, x0:x1].copy(),
                               action="key", key=c.key, cooldown=item.cooldown, region=region,
                               hold=c.hold)
            else:
                rule = im.Rule(name=item.name, image=Path(), template=image[y0:y1, x0:x1].copy(),
                               action="click", button=c.button, offset=self.offset_for(item),
                               cooldown=item.cooldown, region=region,
                               hold=c.hold, path=list(c.moves))
            if release is not None:
                pad = self.RELEASE_PAD
                rule.hold = self.give_up_after(item)
                rule.release_template = release
                rule.release_threshold = self.RELEASE_THRESHOLD
                rule.release_delay = round(self.release_delay(item), 3)
                ro = c.release_origin
                rule.release_region = (ro[0] + rx0 - pad, ro[1] + ry0 - pad,
                                       rx1 - rx0 + 2 * pad, ry1 - ry0 + 2 * pad)
            rules.append(rule)
        try:
            for rule in rules:
                rule.image = self.app.save_template(rule.name, rule.template)
                if rule.release_template is not None:
                    rule.release_image = self.app.save_template(f"{rule.name} release",
                                                                rule.release_template)
        except OSError as exc:
            messagebox.showerror("Couldn't save the images", str(exc), parent=self)
            return
        if self.as_steps:
            steps, previous = [], None
            for rule, item in zip(rules, [i for i in self.items if i.include]):
                if previous is not None and self.waits.get():
                    wait = self.pause_step(previous.capture, item.capture)
                    if wait is not None:
                        steps.append(wait)
                steps.append(self.as_step(rule, item))
                previous = item
            self.app.flow_editor.add_steps(steps)
            self.app.log(f"Added {len(steps)} step(s) from the recording to the flow.")
        else:
            items = [i for i in self.items if i.include]
            if self.waits.get():
                for rule, item, after in zip(rules, items, items[1:]):
                    rule.pause_after = self.pause_between(item.capture, after.capture) or 0.0
            self.app.add_rules(rules)
            self.app.log(f"Added {len(rules)} rule(s) from the recording.")
        self.destroy()

    def as_step(self, rule, item):
        """A recorded capture as a flow step: wait for its image (up to 10 s), then act.
        A spam-click burst becomes that many clicks, at the speed you clicked."""
        c = item.capture
        if c.clicks > 1 and rule.motion in ("click", "press"):
            rule.repeat = c.clicks
            rule.repeat_gap = round(max((c.time - c.start) / (c.clicks - 1), 0.03), 3)
        return fl.Step("image", name=rule.name, rule=rule, timeout=10.0)

    MIN_PAUSE = 0.05  # s; shorter pauses than this aren't worth a Wait step

    def pause_between(self, before, after):
        """Seconds from letting go of `before` to pressing `after`, or None if too short
        to matter. Skipped captures in between still count."""
        pause = after.start - before.time
        return round(pause, 2) if pause >= self.MIN_PAUSE else None

    def pause_step(self, before, after):
        """A Wait step for the pause between two captures, or None."""
        pause = self.pause_between(before, after)
        return None if pause is None else fl.Step("wait", seconds=pause)

    def discard(self):
        if not self.items or messagebox.askyesno(
                "Discard recording", f"Discard {len(self.items)} screenshot(s) without adding "
                                     "any rules?", parent=self):
            self.destroy()


def main():
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
