"""The Autopilot tab: Anti-AFK, schedules, and the run history.

All three only work while the window is open. The engine is imgauto.py.
"""

import time
import tkinter as tk
from tkinter import messagebox, ttk

import imgauto as au
import imgmacro as im
import imgtheme as T


class AutopilotPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.windows = []
        self.build()

    def build(self):
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="Running unattended: these work while this window is open, even "
                            "minimized.", style="PanelMuted.TLabel").pack(side="left")

        body = ttk.Frame(self, padding=(10, 10, 10, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="half")
        body.columnconfigure(1, weight=1, uniform="half")
        body.rowconfigure(1, weight=1)

        # Anti-AFK
        afk = ttk.LabelFrame(body, text="Anti-AFK", padding=12)
        afk.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        afk.columnconfigure(1, weight=1)
        ttk.Label(afk, text="Every few minutes, switch to the game, press a key so it doesn't kick you "
                            "for being idle, and switch straight back to what you were using.",
                  foreground=T.MUTED, wraplength=520, justify="left").grid(row=0, column=0, columnspan=3,
                                                                          sticky="w")
        ttk.Label(afk, text="Game window").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.window_var = tk.StringVar()
        self.window_box = ttk.Combobox(afk, textvariable=self.window_var, state="readonly")
        self.window_box.grid(row=1, column=1, sticky="ew", padx=8, pady=(10, 0))
        self.window_box.bind("<<ComboboxSelected>>", lambda e: self.pick_window())
        ttk.Button(afk, text="↻", width=3, style="Tool.TButton", command=self.list_windows).grid(
            row=1, column=2, pady=(10, 0))
        ttk.Label(afk, text="Every (minutes)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        row = ttk.Frame(afk)
        row.grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))
        self.every_var = tk.StringVar(value="15")
        ttk.Spinbox(row, textvariable=self.every_var, from_=1, to=120, increment=1, width=6).pack(side="left")
        ttk.Label(row, text="Key").pack(side="left", padx=(16, 6))
        self.key_var = tk.StringVar(value="space")
        ttk.Entry(row, textvariable=self.key_var, width=12).pack(side="left")
        self.on_var = tk.BooleanVar(value=False)
        self.on_check = ttk.Checkbutton(afk, text="On", variable=self.on_var, command=self.toggle_afk)
        self.on_check.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.afk_status = ttk.Label(afk, foreground=T.MUTED)
        self.afk_status.grid(row=3, column=1, columnspan=2, sticky="w", padx=8, pady=(10, 0))
        for var in (self.every_var, self.key_var):
            var.trace_add("write", lambda *a: self.apply_afk())

        # Schedules
        sched = ttk.LabelFrame(body, text="Schedules", padding=12)
        sched.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(sched, text="Run a flow or a macro every so often, or at a time of day. If "
                              "something else is running then, that run is skipped.",
                  foreground=T.MUTED, wraplength=520, justify="left").pack(anchor="w")
        frame = ttk.Frame(sched)
        frame.pack(fill="both", expand=True, pady=(8, 6))
        self.sched_tree = ttk.Treeview(frame, columns=("on", "next"), height=5, selectmode="browse")
        self.sched_tree.heading("#0", text="Runs")
        self.sched_tree.heading("on", text="On")
        self.sched_tree.heading("next", text="Next run")
        self.sched_tree.column("#0", width=300)
        self.sched_tree.column("on", width=36, anchor="center")
        self.sched_tree.column("next", width=110)
        self.sched_tree.tag_configure("off", foreground=T.FAINT)
        self.sched_tree.pack(fill="both", expand=True)
        self.sched_tree.bind("<Double-1>", lambda e: self.edit_schedule())
        buttons = ttk.Frame(sched)
        buttons.pack(fill="x")
        for text, cmd in (("+  Add…", self.add_schedule), ("Edit…", self.edit_schedule),
                          ("On / Off", self.toggle_schedule), ("Remove", self.remove_schedule)):
            ttk.Button(buttons, text=text, style="Tool.TButton", command=cmd).pack(side="left", padx=(0, 4))

        # History
        hist = ttk.LabelFrame(body, text="Run history", padding=12)
        hist.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        hist.columnconfigure(0, weight=3)
        hist.columnconfigure(1, weight=2)
        hist.rowconfigure(0, weight=1)
        self.history = self.make_tree(hist, (("#0", "When", 130), ("what", "What", 260),
                                             ("took", "Took", 80), ("result", "Result", 90)))
        self.history.master.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.totals = self.make_tree(hist, (("#0", "Flow, macro or rules", 230), ("runs", "Runs", 60),
                                            ("time", "Total time", 90), ("last", "Last", 120)))
        self.totals.master.grid(row=0, column=1, sticky="nsew")
        bottom = ttk.Frame(hist)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.history_note = ttk.Label(bottom, foreground=T.MUTED)
        self.history_note.pack(side="left")
        ttk.Button(bottom, text="Clear history", style="Tool.TButton",
                   command=self.clear_history).pack(side="right")

    @staticmethod
    def make_tree(parent, columns):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c for c, _, _ in columns[1:]], selectmode="none")
        for col, text, width in columns:
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        for tag, colour in (("finished", T.GOOD), ("failed", T.BAD), ("error", T.BAD)):
            tree.tag_configure(tag, foreground=colour)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    # --- after a config is opened -------------------------------------------------

    def load(self):
        settings = self.app.cfg.anti_afk
        self.loading = True
        self.every_var.set(f"{float(settings.get('every', 15)):g}")
        self.key_var.set(str(settings.get("key", "space")))
        self.on_var.set(False)
        self.loading = False
        self.list_windows(prefer=settings.get("window", ""))
        self.refresh()

    def refresh(self):
        self.show_schedules()
        self.show_history()
        self.tick()

    # --- Anti-AFK -----------------------------------------------------------------

    def list_windows(self, prefer=None):
        current = self.app.anti_afk.window
        self.windows = au.list_windows()
        self.window_box.configure(values=[w.label() for w in self.windows])
        match = next((w for w in self.windows if current and w.hwnd == current.hwnd), None)
        if match is None and prefer:
            match = next((w for w in self.windows if w.title == prefer), None) or \
                next((w for w in self.windows if prefer.lower() in w.title.lower()), None)
        if match is not None:
            self.window_var.set(match.label())
            self.app.anti_afk.configure(window=match)
        elif current is None:
            self.window_var.set("")

    def pick_window(self):
        i = self.window_box.current()
        if 0 <= i < len(self.windows):
            window = self.windows[i]
            self.app.anti_afk.configure(window=window)
            self.app.cfg.anti_afk["window"] = window.title
            self.app.mark_dirty()
        self.tick()

    def apply_afk(self):
        if getattr(self, "loading", False):
            return
        try:
            every = float(self.every_var.get())
            key = self.key_var.get().strip()
            im.parse_combo(key)
        except ValueError:
            return  # mid-typing
        if not 1 <= every <= 120:
            return
        self.app.anti_afk.configure(every=every, key=key)
        if (self.app.cfg.anti_afk.get("every"), self.app.cfg.anti_afk.get("key")) != (every, key):
            self.app.cfg.anti_afk.update(every=every, key=key)
            self.app.mark_dirty()
        self.tick()

    def toggle_afk(self):
        try:
            self.app.anti_afk.configure(enabled=self.on_var.get())
        except ValueError as exc:
            self.on_var.set(False)
            messagebox.showinfo("Anti-AFK", f"First {exc}.", parent=self)
        self.tick()

    def tick(self):
        """Every second: the Anti-AFK countdown and the schedules' next runs."""
        afk = self.app.anti_afk
        left = afk.seconds_left()
        if not afk.enabled:
            text = "Off" if afk.window else "Off: pick the game's window"
        elif left is None or left < 1:
            text = afk.status
        else:
            m, s = divmod(int(left), 60)
            text = f"Next {afk.key} in {m}:{s:02d}" + (f"   ·   {afk.status}" if afk.presses else "")
        self.afk_status.configure(text=text)
        for i, s in enumerate(self.app.schedules):
            if self.sched_tree.exists(str(i)):
                self.sched_tree.set(str(i), "next", self.next_text(s))

    # --- schedules ------------------------------------------------------------------

    def next_text(self, s):
        when = self.app.scheduler.due.get(s)
        if not s.enabled or when is None:
            return ""
        return f"{when:%a %H:%M}"

    def show_schedules(self, select=None):
        tree = self.sched_tree
        tree.delete(*tree.get_children())
        for i, s in enumerate(self.app.schedules):
            tree.insert("", "end", iid=str(i), text=s.describe(),
                        values=("✓" if s.enabled else "", self.next_text(s)),
                        tags=() if s.enabled else ("off",))
        if select is not None and self.app.schedules:
            tree.selection_set(str(min(select, len(self.app.schedules) - 1)))

    def selected_schedule(self):
        selection = self.sched_tree.selection()
        return int(selection[0]) if selection else None

    def schedules_changed(self, select=None):
        self.app.scheduler.update(self.app.schedules)
        self.app.mark_dirty()
        self.show_schedules(select)

    def add_schedule(self):
        s = ScheduleDialog(self.app, None).result
        if s is not None:
            self.app.schedules.append(s)
            self.schedules_changed(len(self.app.schedules) - 1)

    def edit_schedule(self):
        i = self.selected_schedule()
        if i is None:
            return
        s = ScheduleDialog(self.app, self.app.schedules[i]).result
        if s is not None:
            self.app.schedules[i] = s
            self.schedules_changed(i)

    def toggle_schedule(self):
        i = self.selected_schedule()
        if i is not None:
            s = self.app.schedules[i]
            s.enabled = not s.enabled
            if s.enabled:
                self.app.scheduler.reset(s)  # count from now, not from when it was switched off
            self.schedules_changed(i)

    def remove_schedule(self):
        i = self.selected_schedule()
        if i is not None:
            del self.app.schedules[i]
            self.schedules_changed(i)

    # --- history --------------------------------------------------------------------

    def show_history(self):
        stats = self.app.stats
        self.history.delete(*self.history.get_children())
        for h in reversed(stats.history):
            when = time.strftime("%d %b %H:%M:%S", time.localtime(h["at"]))
            what = f"{h['what']}: {h['name']}" if h["what"] != "rules" else "Watch rules"
            self.history.insert("", "end", text=when, values=(what, clock(h["seconds"]), h["outcome"]),
                                tags=(h["outcome"],))
        self.totals.delete(*self.totals.get_children())
        for key, t in sorted(stats.totals.items(), key=lambda kv: -kv[1].get("last", 0)):
            what, _, name = key.partition(":")
            label = "Watch rules" if what == "rules" else f"{what}: {name}"
            last = time.strftime("%d %b %H:%M", time.localtime(t.get("last", 0)))
            self.totals.insert("", "end", text=label, values=(t.get("runs", 0), clock(t.get("seconds", 0)), last))
        where = stats.path.name if stats.path else "nowhere yet: save the config first"
        self.history_note.configure(text=f"Kept in {where}, next to the config, so running things "
                                         "doesn't change the config itself.")

    def clear_history(self):
        if messagebox.askyesno("Clear history", "Forget every run so far?", parent=self):
            try:
                self.app.stats.clear()
            except OSError as exc:
                messagebox.showerror("Couldn't clear the history", str(exc), parent=self)
            self.show_history()


def clock(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ScheduleDialog(tk.Toplevel):
    """Make or change a schedule. `.result` is the Schedule, or None if cancelled."""

    def __init__(self, app, schedule=None):
        super().__init__(app.root)
        self.app, self.result = app, None
        s = schedule or au.Schedule(target=(app.flows[0].name if app.flows else
                                            app.macros[0].name if app.macros else ""),
                                    what="flow" if app.flows or not app.macros else "macro")
        self.title("Schedule")
        self.transient(app.root)
        self.resizable(False, False)
        self.what = tk.StringVar(value=s.what)
        self.target = tk.StringVar(value=s.target)
        self.kind = tk.StringVar(value=s.kind)
        self.every = tk.StringVar(value=f"{s.every:g}")
        self.at = tk.StringVar(value=s.at)
        self.days = [tk.BooleanVar(value=not s.days or d in s.days) for d in range(7)]
        self.enabled = tk.BooleanVar(value=s.enabled)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="x")
        ttk.Label(body, text="Run").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(body)
        row.grid(row=0, column=1, sticky="w", padx=8)
        for value, label in (("flow", "a flow"), ("macro", "a macro")):
            ttk.Radiobutton(row, text=label, value=value, variable=self.what,
                            command=self.update_targets).pack(side="left", padx=(0, 10))
        self.target_box = ttk.Combobox(body, textvariable=self.target, state="readonly", width=30)
        self.target_box.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))

        ttk.Label(body, text="When").grid(row=2, column=0, sticky="nw", pady=(12, 0))
        when = ttk.Frame(body)
        when.grid(row=2, column=1, sticky="w", padx=8, pady=(12, 0))
        ttk.Radiobutton(when, text="Every", value="every", variable=self.kind).grid(row=0, column=0, sticky="w")
        ttk.Spinbox(when, textvariable=self.every, from_=1, to=10080, increment=5, width=7).grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(when, text="minutes").grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(when, text="Daily at", value="daily", variable=self.kind).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(when, textvariable=self.at, width=7).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(when, text="(24-hour, e.g. 09:30) on").grid(row=1, column=2, sticky="w", pady=(6, 0))
        days = ttk.Frame(when)
        days.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        for d, var in enumerate(self.days):
            ttk.Checkbutton(days, text=au.DAYS[d], variable=var).pack(side="left", padx=(0, 4))
        ttk.Checkbutton(body, text="On", variable=self.enabled).grid(row=3, column=1, sticky="w",
                                                                    padx=8, pady=(12, 0))
        ttk.Label(body, text="It counts from when the window opens (or the schedule is saved), and "
                             "a run missed while the window was closed isn't made up later.",
                  foreground=T.MUTED, wraplength=380, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        bottom = ttk.Frame(self, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="OK", command=self.ok).pack(side="right", padx=6)
        self.update_targets(keep=True)
        self.grab_set()
        app.root.wait_window(self)

    def update_targets(self, keep=False):
        names = [x.name for x in (self.app.flows if self.what.get() == "flow" else self.app.macros)]
        self.target_box.configure(values=names)
        if not (keep and self.target.get() in names):
            self.target.set(names[0] if names else "")

    def ok(self):
        days = tuple(d for d, var in enumerate(self.days) if var.get())
        try:
            every = float(self.every.get())
        except ValueError:
            messagebox.showerror("Schedule", "Every how many minutes? That must be a number.", parent=self)
            return
        s = au.Schedule(what=self.what.get(), target=self.target.get(), kind=self.kind.get(),
                        every=every, at=self.at.get().strip(), days=() if len(days) == 7 else days,
                        enabled=self.enabled.get())
        if s.kind == "daily" and not days:
            messagebox.showerror("Schedule", "Tick at least one day.", parent=self)
            return
        try:
            s.validate()
        except ValueError as exc:
            messagebox.showerror("Schedule", str(exc).capitalize() + ".", parent=self)
            return
        self.result = s
        self.destroy()
