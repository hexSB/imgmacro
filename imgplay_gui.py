"""The Macros tab: record the mouse and keyboard, and play the recordings back.

The list on the left holds the recordings; the panel on the right sets how the
selected one plays (speed, how many times), its notes, its safeguards (rules
checked while it plays, e.g. to close a disconnect dialog) and its checkpoints
(moments where it waits for something to appear). The engine is imgplay.py.
"""

import copy
import time
import tkinter as tk
from tkinter import messagebox, ttk

import imgplay as ip
import imgtheme as T

SPEEDS = ["0.25", "0.5", "0.75", "1", "1.5", "2", "3", "4"]


class MacroPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.selected = None
        self.loading = False
        self.build()

    # --- layout -------------------------------------------------------------------

    def build(self):
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        self.play_btn = ttk.Button(bar, text="▶  Play", style="Accent.TButton", command=self.play)
        self.pause_btn = ttk.Button(bar, text="⏸  Pause", style="Tool.TButton",
                                    command=self.app.toggle_pause)
        self.stop_btn = ttk.Button(bar, text="■  Stop", style="Tool.TButton", command=self.app.stop_all)
        for b in (self.play_btn, self.pause_btn, self.stop_btn):
            b.pack(side="left", padx=(0, 6))
        self.record_btn = ttk.Button(bar, text="●  Record macro", style="Tool.TButton",
                                     command=lambda: self.app.toggle_recording(into="macro"))
        self.record_btn.pack(side="left", padx=(12, 0))
        ttk.Label(bar, text="A macro replays your mouse and keys exactly, with their timing.",
                  style="PanelMuted.TLabel").pack(side="right")

        body = ttk.Frame(self, padding=(10, 10, 10, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=420)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        frame = ttk.Frame(left)
        frame.pack(fill="both", expand=True, pady=(0, 6))
        columns = ("length", "plays", "guards", "runs", "last")
        self.tree = ttk.Treeview(frame, columns=columns, selectmode="browse")
        for col, (text, width, anchor) in {"#0": ("Macro", 200, "w"), "length": ("Recording", 190, "w"),
                                           "plays": ("Plays", 110, "w"), "guards": ("Guards", 90, "center"),
                                           "runs": ("Runs", 50, "center"),
                                           "last": ("Last run", 110, "w")}.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("#0", "length"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<Double-1>", lambda e: self.play())
        self.tree.bind("<Delete>", lambda e: self.delete())
        buttons = ttk.Frame(left)
        buttons.pack(fill="x")
        self.list_buttons = []
        for text, command in (("Duplicate", self.duplicate), ("Delete", self.delete),
                              ("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1))):
            b = ttk.Button(buttons, text=text, command=command, style="Tool.TButton",
                           width=3 if len(text) == 1 else None)
            b.pack(side="left", padx=(0, 4))
            self.list_buttons.append(b)

        self.side = ttk.Frame(body, style="Panel.TFrame", padding=14)
        self.side.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

    # --- the list -------------------------------------------------------------------

    def refresh_list(self, select=None):
        if select is None:
            select = self.selected
        self.tree.delete(*self.tree.get_children())
        for i, m in enumerate(self.app.macros):
            total = self.app.stats.total("macro", m.name)
            plays = ("until stopped" if m.loops == 0 else f"{m.loops}×") + f" at {m.speed:g}×"
            guards = f"{len(m.guards)} / {len(m.checkpoints)}" if m.guards or m.checkpoints else ""
            last = ""
            if total.get("last"):
                last = time.strftime("%d %b %H:%M", time.localtime(total["last"]))
            self.tree.insert("", "end", iid=str(i), text="  " + m.name,
                             values=(m.summary(), plays, guards, total.get("runs", "") or "", last))
        if select in self.app.macros:
            iid = str(self.app.macros.index(select))
            self.tree.selection_set(iid)
            self.tree.see(iid)
        elif self.app.macros:
            self.tree.selection_set("0")
        else:
            self.selected = None
            self.show_side()

    def on_select(self):
        selection = self.tree.selection()
        macro = self.app.macros[int(selection[0])] if selection else None
        if macro is not self.selected:
            self.selected = macro
            self.show_side()

    def changed(self, relist=True):
        self.app.mark_dirty()
        if relist:
            self.refresh_list()

    def duplicate(self):
        m = self.selected
        if m is None or self.app.busy():
            return
        taken = {x.name for x in self.app.macros}
        name, n = f"{m.name} (copy)", 2
        while name in taken:
            name, n = f"{m.name} (copy {n})", n + 1
        twin = copy.deepcopy(m)
        twin.name = name
        twin.file = ip.recording_path(self.app.config_dir(), name)
        try:
            ip.write_recording(twin)
        except OSError as exc:
            messagebox.showerror("Couldn't copy the recording", str(exc), parent=self)
            return
        self.app.macros.insert(self.app.macros.index(m) + 1, twin)
        self.changed(relist=False)
        self.refresh_list(select=twin)

    def delete(self):
        m = self.selected
        if m is None or self.app.busy():
            return
        users = [f.name for f in self.app.flows for s in f.all_steps()
                 if s.kind == "macro" and s.macro == m.name]
        users += [r.name for r in self.app.cfg.rules if r.action == "macro" and r.macro == m.name]
        warn = f"\n\nIt's played by: {', '.join(sorted(set(users)))}." if users else ""
        if messagebox.askyesno("Delete macro", f"Delete “{m.name}”?{warn}\n\nIts recording file "
                               "stays on disk.", parent=self):
            index = self.app.macros.index(m)
            self.app.macros.remove(m)
            self.selected = None
            self.changed(relist=False)
            self.refresh_list(select=self.app.macros[min(index, len(self.app.macros) - 1)]
                              if self.app.macros else None)

    def move(self, delta):
        m = self.selected
        macros = self.app.macros
        if m is None:
            return
        i = macros.index(m)
        if 0 <= i + delta < len(macros):
            macros[i], macros[i + delta] = macros[i + delta], macros[i]
            self.changed()

    def play(self):
        if self.selected is None:
            messagebox.showinfo("Play", "Record a macro first: click “● Record macro”.", parent=self)
            return
        self.app.start_macro(self.selected)

    def set_busy(self, busy, running=False, recording=False):
        state = "disabled" if busy else "normal"
        for b in self.list_buttons + [self.play_btn]:
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.pause_btn.configure(state="normal" if running else "disabled")
        self.record_btn.configure(state="disabled" if busy and not recording else "normal",
                                  text="■  Stop recording" if recording else "●  Record macro")
        self.show_side()

    # --- the side panel ---------------------------------------------------------------

    def show_side(self):
        for w in self.side.winfo_children():
            w.destroy()
        m = self.selected
        if m is None:
            ttk.Label(self.side, text="Macros", style="PanelTitle.TLabel").pack(anchor="w")
            self.note("Click “● Record macro”, play the game as you want it repeated, and press "
                      f"{self.app.cfg.stop_key} to stop. Every mouse move, click, wheel turn and key is "
                      "recorded with its timing, starting at your first click or key.\n\n"
                      "Then play it back here, from a flow (“Play a macro” step), or from a rule on "
                      "the Watch tab (action “Play a macro”).")
            return
        busy = self.app.busy()
        ttk.Label(self.side, text=m.name, style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(self.side, text=m.summary(), style="PanelMuted.TLabel").pack(anchor="w")
        if m.screen[2:] != (0, 0):
            self.note(f"Recorded on a {m.screen[2]}×{m.screen[3]} desktop. Keep the game where it was "
                      f"and at the same size: clicks replay at the same screen positions.")

        form = ttk.Frame(self.side, style="Panel.TFrame")
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        name = tk.StringVar(value=m.name)
        ttk.Label(form, text="Name", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(form, textvariable=name)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        name_entry.bind("<FocusOut>", lambda e: self.rename(m, name))
        name_entry.bind("<Return>", lambda e: self.rename(m, name))
        speed = tk.StringVar(value=f"{m.speed:g}")
        ttk.Label(form, text="Speed (×)", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(form, textvariable=speed, values=SPEEDS, width=6).grid(row=1, column=1, sticky="w",
                                                                           padx=(8, 0), pady=(6, 0))
        loops = tk.StringVar(value=str(m.loops))
        ttk.Label(form, text="Play it (times)", style="Panel.TLabel").grid(row=2, column=0, sticky="w",
                                                                           pady=(6, 0))
        loop_row = ttk.Frame(form, style="Panel.TFrame")
        loop_row.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Spinbox(loop_row, textvariable=loops, from_=0, to=100000, width=7).pack(side="left")
        ttk.Label(loop_row, text="0 = until stopped", style="PanelMuted.TLabel").pack(side="left", padx=8)

        def on_speed(*a):
            try:
                v = float(speed.get())
            except ValueError:
                return
            if 0.05 <= v <= 20 and v != m.speed:
                m.speed = v
                self.changed()

        def on_loops(*a):
            try:
                v = int(loops.get())
            except ValueError:
                return
            if v >= 0 and v != m.loops:
                m.loops = v
                self.changed()
        speed.trace_add("write", on_speed)
        loops.trace_add("write", on_loops)
        self.keep = [name, speed, loops]

        ttk.Label(self.side, text="Notes", style="Panel.TLabel").pack(anchor="w", pady=(10, 2))
        notes = tk.Text(self.side, height=3, wrap="word", relief="flat", background=T.CARD,
                        foreground=T.TEXT, insertbackground=T.TEXT, font=T.FONT, highlightthickness=0)
        notes.insert("1.0", m.notes)
        notes.pack(fill="x")

        def on_notes(e=None):
            text = notes.get("1.0", "end-1c").strip()
            if text != m.notes:
                m.notes = text
                self.app.mark_dirty()
        notes.bind("<FocusOut>", on_notes)
        notes.bind("<KeyRelease>", on_notes)

        self.rule_list("Safeguards", m.guards, "safeguard",
                       "Checked all the time while it plays. When one appears, the macro pauses "
                       "(letting go of anything held), the safeguard acts, and after the wait below "
                       "the macro carries on where it stopped.", busy)
        delay = tk.StringVar(value=f"{m.resume_delay:g}")
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Wait after a safeguard acts (s)", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(row, textvariable=delay, from_=0, to=600, increment=0.5, width=6).pack(side="left", padx=8)

        def on_delay(*a):
            try:
                v = float(delay.get())
            except ValueError:
                return
            if 0 <= v <= 600:
                m.resume_delay = v
                self.app.mark_dirty()
        delay.trace_add("write", on_delay)
        self.keep.append(delay)

        self.checkpoint_list(m, busy)
        if busy:
            self.disable(self.side)

    def rename(self, m, var):
        name = var.get().strip()
        if not name or name == m.name:
            var.set(m.name)
            return
        if any(x.name == name for x in self.app.macros if x is not m):
            messagebox.showerror("Rename", f"There's already a macro called “{name}”.", parent=self)
            var.set(m.name)
            return
        old = m.name
        m.name = name
        # Keep flows and rules that play it pointing at it.
        for f in self.app.flows:
            for s in f.all_steps():
                if s.kind == "macro" and s.macro == old:
                    s.macro = name
                if s.rule is not None and s.rule.action == "macro" and s.rule.macro == old:
                    s.rule.macro = name
        for r in self.app.cfg.rules:
            if r.action == "macro" and r.macro == old:
                r.macro = name
        for s in self.app.schedules:
            if s.what == "macro" and s.target == old:
                s.target = name
        self.changed()
        self.app.flow_editor.refresh()

    def rule_list(self, title, rules, kind, note, busy):
        ttk.Label(self.side, text=title, style="Panel.TLabel").pack(anchor="w", pady=(12, 2))
        frame = ttk.Frame(self.side, style="Panel.TFrame")
        frame.pack(fill="x")
        tree = ttk.Treeview(frame, columns=("does",), height=3, selectmode="browse", show="tree")
        tree.column("#0", width=150)
        tree.column("does", width=200)
        for i, r in enumerate(rules):
            tree.insert("", "end", iid=str(i), text=("" if r.enabled else "(off) ") + r.name,
                        values=(f"{r.describe_target()} → {self.app.gui.describe_action(r).lower()}",))
        tree.pack(fill="x")
        tree.bind("<Double-1>", lambda e: self.edit_rule(rules, tree, kind))
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 0))
        for text, cmd in (("+ Add…", lambda: self.add_rule(rules, kind)),
                          ("Edit…", lambda: self.edit_rule(rules, tree, kind)),
                          ("Remove", lambda: self.remove(rules, tree))):
            ttk.Button(row, text=text, style="Tool.TButton", command=cmd).pack(side="left", padx=(0, 4))
        self.note(note)

    def add_rule(self, rules, kind):
        rule = self.app.gui.RuleDialog(self.app, None, allow_macro=False, kind=kind).result
        if rule is not None:
            rules.append(rule)
            self.changed()

    def edit_rule(self, rules, tree, kind):
        selection = tree.selection()
        if not selection or self.app.busy():
            return
        i = int(selection[0])
        rule = self.app.gui.RuleDialog(self.app, rules[i], allow_macro=False, kind=kind).result
        if rule is not None:
            rules[i] = rule
            self.changed()

    def remove(self, items, tree):
        selection = tree.selection()
        if selection and not self.app.busy():
            del items[int(selection[0])]
            self.changed()

    def checkpoint_list(self, m, busy):
        ttk.Label(self.side, text="Checkpoints", style="Panel.TLabel").pack(anchor="w", pady=(12, 2))
        frame = ttk.Frame(self.side, style="Panel.TFrame")
        frame.pack(fill="x")
        tree = ttk.Treeview(frame, columns=("wait",), height=3, selectmode="browse", show="tree")
        tree.column("#0", width=110)
        tree.column("wait", width=240)
        for i, c in enumerate(sorted(m.checkpoints, key=lambda c: c.at)):
            fail = "else stop" if c.on_fail == "stop" else "else carry on"
            tree.insert("", "end", iid=str(m.checkpoints.index(c)), text=f"at {ip.clock(c.at)}",
                        values=(f"wait ≤ {c.timeout:g} s for {c.rule.describe_target()}, {fail}",))
        tree.pack(fill="x")
        tree.bind("<Double-1>", lambda e: self.edit_checkpoint(m, tree))
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 0))
        for text, cmd in (("+ Add…", lambda: self.add_checkpoint(m)),
                          ("Edit…", lambda: self.edit_checkpoint(m, tree)),
                          ("Remove", lambda: self.remove(m.checkpoints, tree))):
            ttk.Button(row, text=text, style="Tool.TButton", command=cmd).pack(side="left", padx=(0, 4))
        self.note("At that moment in the recording, playback waits until the image appears, so a "
                  "slow loading screen can't throw the rest of the timing off. If it never does, "
                  "the macro stops (in a flow: its Failed lane runs) or carries on.")

    def add_checkpoint(self, m):
        rule = self.app.gui.RuleDialog(self.app, None, allow_macro=False, kind="checkpoint",
                                       default_action="none").result
        if rule is None:
            return
        cp = ip.Checkpoint(at=round(m.duration / 2, 1), rule=rule)
        if CheckpointDialog(self, m, cp).ok:
            m.checkpoints.append(cp)
            self.changed()

    def edit_checkpoint(self, m, tree):
        selection = tree.selection()
        if not selection or self.app.busy():
            return
        cp = m.checkpoints[int(selection[0])]
        if CheckpointDialog(self, m, cp).ok:
            self.changed()

    def note(self, text):
        ttk.Label(self.side, text=text, style="PanelMuted.TLabel", wraplength=390,
                  justify="left").pack(anchor="w", pady=(6, 0))

    def disable(self, widget):
        try:
            if isinstance(widget, (ttk.Entry, ttk.Spinbox, ttk.Button, ttk.Combobox, tk.Text)):
                widget.configure(state="disabled")
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self.disable(child)


class CheckpointDialog(tk.Toplevel):
    """When a checkpoint waits, for how long, and what happens if its image doesn't
    appear. `.ok` is True if saved (the checkpoint is changed in place)."""

    def __init__(self, page, macro, cp):
        super().__init__(page.app.root)
        self.page, self.cp, self.ok = page, cp, False
        self.title("Checkpoint")
        self.transient(page.app.root)
        self.resizable(False, False)
        self.at = tk.StringVar(value=f"{cp.at:g}")
        self.timeout = tk.StringVar(value=f"{cp.timeout:g}")
        self.on_fail = tk.StringVar(value=cp.on_fail)
        self.rule = cp.rule
        body = ttk.Frame(self, padding=12)
        body.pack(fill="x")
        ttk.Label(body, text=f"Wait at (s into the recording, 0 to {macro.duration:g})").grid(
            row=0, column=0, sticky="w")
        ttk.Spinbox(body, textvariable=self.at, from_=0, to=max(macro.duration, 0), increment=0.5,
                    width=8).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(body, text="for").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.what = ttk.Label(body, foreground=T.MUTED)
        self.what.grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Button(body, text="Image & action…", command=self.edit_rule).grid(row=1, column=2, pady=(8, 0))
        ttk.Label(body, text="Wait up to (s)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(body, textvariable=self.timeout, from_=0, to=3600, increment=1, width=8).grid(
            row=2, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(body, text="If it doesn't appear").grid(row=3, column=0, sticky="w", pady=(8, 0))
        fail = ttk.Frame(body)
        fail.grid(row=3, column=1, columnspan=2, sticky="w", padx=8, pady=(8, 0))
        ttk.Radiobutton(fail, text="Stop the macro (it failed)", value="stop",
                        variable=self.on_fail).pack(anchor="w")
        ttk.Radiobutton(fail, text="Carry on anyway", value="continue", variable=self.on_fail).pack(anchor="w")
        ttk.Label(body, text="What it's waiting for can also be acted on, e.g. clicked: set that "
                             "under Image & action (Only log it = just wait).", foreground=T.MUTED,
                  wraplength=420, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        bottom = ttk.Frame(self, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="OK", command=self.save).pack(side="right", padx=6)
        self.show_rule()
        self.grab_set()
        page.app.root.wait_window(self)

    def show_rule(self):
        self.what.configure(text=f"{self.rule.describe_target()}, then "
                                 f"{self.page.app.gui.describe_action(self.rule).lower()}")

    def edit_rule(self):
        rule = self.page.app.gui.RuleDialog(self.page.app, self.rule, allow_macro=False,
                                            kind="checkpoint").result
        self.grab_set()
        if rule is not None:
            self.rule = rule
            self.show_rule()

    def save(self):
        try:
            at, timeout = float(self.at.get()), float(self.timeout.get())
        except ValueError:
            messagebox.showerror("Checkpoint", "The times must be numbers of seconds.", parent=self)
            return
        if at < 0 or timeout < 0:
            messagebox.showerror("Checkpoint", "The times can't be negative.", parent=self)
            return
        self.cp.at, self.cp.timeout, self.cp.on_fail, self.cp.rule = at, timeout, self.on_fail.get(), self.rule
        self.ok = True
        self.destroy()
