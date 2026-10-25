"""The Flows tab: a flow's steps drawn as a graph that lays itself out.

The main line runs top to bottom. An "If image appears" step splits into a
Found lane (straight on down) and a Not found lane (in its own column to the
right), and a "Play a macro" step into Finished and Failed lanes the same way;
both rejoin at a dot under the step, so lines never cross. A Repeat step's
body sits under it with a loop-back line on its right. Click a step to edit it
on the right, click a + on any line to insert a step there.
"""

import copy
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, simpledialog, ttk

import imgflow as fl
import imgmacro as im
import imgtheme as T

# kind -> (icon, accent colour). The icons are plain Unicode so Tk can draw them.
KINDS = {
    "start": ("▶", T.GOOD), "end": ("◆", T.FAINT),
    "image": ("◉", T.BLUE), "macro": ("●", T.GOOD), "key": ("⌨", T.ACCENT),
    "click": ("⊕", T.ORANGE), "type": ("✎", T.ACCENT), "scroll": ("↕", T.ORANGE),
    "mouse": ("↖", T.ORANGE),
    "wait": ("◔", T.MUTED), "repeat": ("↻", T.PURPLE), "flow": ("⇥", T.PURPLE),
    "restart": ("⟲", T.PURPLE), "stop": ("■", T.BAD),
}

# Layout, in unzoomed pixels. The main line runs top to bottom.
NODE_W, NODE_H = 236, 60
GAP = 64           # line length between steps: room for a + and a label
COL_GAP = 46       # between a Found column and the Not found column beside it
LOOP_W = 44        # right of a Repeat body, for the loop-back line
EMPTY_W = 150      # an empty Not found lane: just a line with a +
EMPTY_H = 86       # an empty lane, still long enough for its + and label
MARGIN = 40


class Layout:
    """World coordinates for everything in a flow's graph.

    Each lane is a column whose spine is the centre line of its steps. An image
    step's Found lane carries on down its own column; its Not found lane gets a
    column to the right; both meet at a dot under the step and the main line
    carries on from there, so lines never cross.
    """

    def __init__(self, flow):
        self.nodes = []   # (key, x, y, w, h); key is a Step, "start" or "end"
        self.edges = []   # (points, colour, label, dashed, arrow)
        self.pluses = []  # (x, y, lane list, index to insert at)
        self.dots = []    # (x, y) where lanes rejoin
        x, y = MARGIN, MARGIN
        self.nodes.append(("start", x, y, NODE_W, NODE_H))
        cx = x + NODE_W / 2
        w, h = self.seq_size(flow.steps)
        end_y = y + NODE_H + h
        self.seq(flow.steps, x, y + NODE_H, (cx, y + NODE_H), (cx, end_y), None, None)
        self.nodes.append(("end", x, end_y, NODE_W, NODE_H))
        self.width, self.height = x + max(w, NODE_W) + MARGIN, end_y + NODE_H + MARGIN

    # --- sizes ------------------------------------------------------------------

    def seq_size(self, steps, empty_w=NODE_W):
        if not steps:
            return empty_w, EMPTY_H
        sizes = [self.elem_size(s) for s in steps]
        return max(w for w, _ in sizes), sum(GAP + h for _, h in sizes) + GAP

    def elem_size(self, step):
        if step.kind in fl.LANE_NAMES:
            fw, fh = self.seq_size(step.found)
            mw, mh = self.seq_size(step.missing, EMPTY_W)
            return fw + COL_GAP + mw, NODE_H + max(fh, mh)
        if step.kind == "repeat":
            bw, bh = self.seq_size(step.body)
            return bw + LOOP_W, NODE_H + bh
        return NODE_W, NODE_H

    # --- placing ----------------------------------------------------------------

    def seq(self, steps, x, y, entry, exit, label, colour, empty_w=NODE_W):
        """Lay out a lane in the column at x (steps start below y): from `entry`
        through each step to `exit`."""
        spine = x + NODE_W / 2 if steps else x + min(empty_w, NODE_W) / 2
        cur, cy, ended = entry, y, False
        for i, step in enumerate(steps):
            _, h = self.elem_size(step)
            ey = cy + GAP
            self.edge(cur, (spine, ey), spine, label if i == 0 else None,
                      colour if i == 0 else None, ended, steps, i)
            cur = self.elem(step, x, ey)
            cy = ey + h
            ended = ended or (step.enabled and step.kind in ("stop", "restart"))
        self.edge(cur, exit, spine, label if not steps else None,
                  colour if not steps else None, ended, steps, len(steps), arrow=False)

    def elem(self, step, x, y):
        """Place one step (and its lanes) with its top-left at x, y. -> where the line leaves it."""
        self.nodes.append((step, x, y, NODE_W, NODE_H))
        cx, bottom = x + NODE_W / 2, y + NODE_H
        if step.kind in fl.LANE_NAMES:
            yes, no = fl.LANE_NAMES[step.kind]
            fw, fh = self.seq_size(step.found)
            _, mh = self.seq_size(step.missing, EMPTY_W)
            jy = bottom + max(fh, mh)
            self.seq(step.found, x, bottom, (cx, bottom), (cx, jy), yes, T.GOOD)
            self.seq(step.missing, x + fw + COL_GAP, bottom, (x + NODE_W, y + NODE_H / 2), (cx, jy),
                     no, T.BAD, EMPTY_W)
            self.dots.append((cx, jy))
            return cx, jy
        if step.kind == "repeat":
            bw, bh = self.seq_size(step.body)
            jy = bottom + bh
            self.seq(step.body, x, bottom, (cx, bottom), (cx, jy), None, None)
            lx = x + bw + LOOP_W / 2
            label = "forever" if step.times == 0 else f"×{step.times}"
            self.edges.append(([(cx, jy), (lx, jy), (lx, y + NODE_H / 2), (x + NODE_W, y + NODE_H / 2)],
                               T.PURPLE, label, True, True))
            self.dots.append((cx, jy))
            return cx, jy
        return cx, bottom

    def edge(self, a, b, spine, label, colour, dashed, lane, index, arrow=True):
        self.edges.append((self.route(a, b, spine), colour, label, dashed, arrow))
        self.pluses.append((spine, (a[1] + b[1]) / 2, lane, index))

    @staticmethod
    def route(a, b, spine):
        """Corner points from a to b, travelling down the column at x = `spine`."""
        points = [a]
        if a[0] != spine:
            points.append((spine, a[1]))
        if b[0] != spine:
            points.append((spine, b[1]))
        points.append(b)
        return points


def rounded_line(points, r):
    """Points for a smooth=True line that runs straight with rounded corners of radius r."""
    out = [points[0], points[0]]
    for i in range(1, len(points) - 1):
        (ax, ay), (cx, cy), (bx, by) = points[i - 1], points[i], points[i + 1]

        def towards(px, py, d):
            dx, dy = px - cx, py - cy
            length = max(abs(dx) + abs(dy), 1e-9)
            d = min(d, length / 2)
            return cx + dx / length * d, cy + dy / length * d
        p1, p2 = towards(ax, ay, r), towards(bx, by, r)
        out += [p1, p1, (cx, cy), p2, p2]
    out += [points[-1], points[-1]]
    return [v for p in out for v in p]


def rounded_rect(c, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


class FlowCanvas(tk.Canvas):
    """Draws a flow, and turns clicks into calls on the editor."""

    def __init__(self, master, editor):
        super().__init__(master, background=T.BG, highlightthickness=0)
        self.editor = editor
        self.flow = None
        self.zoom, self.pan = 1.0, (0.0, 0.0)
        self.selected = None   # a Step, or "start"
        self.running = None    # the step being run
        self.photos = {}
        self.targets = {}      # canvas tag -> ("node", key) or ("plus", lane, index)
        self.drag = None
        self.layout = None
        self.fonts = {}
        self.needs_fit = False
        self.bind("<Configure>", self.on_resize)
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_drag)
        self.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Double-Button-1>", self.on_double)
        self.bind("<Button-3>", self.on_right)
        for b in ("2",):
            self.bind(f"<ButtonPress-{b}>", self.on_press)
            self.bind(f"<B{b}-Motion>", self.on_drag)
        self.bind("<MouseWheel>", self.on_wheel)
        self.bind("<Shift-MouseWheel>", lambda e: self.scroll(e.delta / 2, 0))
        self.bind("<Control-MouseWheel>", lambda e: self.zoom_by(1.12 if e.delta > 0 else 1 / 1.12, e.x, e.y))

    # --- coordinates ----------------------------------------------------------------

    def on_resize(self, event):
        if self.needs_fit and event.width >= 50:
            self.fit()
            self.editor.update_zoom()
        else:
            self.render()

    def font(self, size, bold=False):
        size = max(5, round(size * self.zoom))
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(family="Segoe UI", size=size,
                                          weight="bold" if bold else "normal")
        return self.fonts[key]

    def sx(self, x):
        return x * self.zoom + self.pan[0]

    def sy(self, y):
        return y * self.zoom + self.pan[1]

    def fit(self):
        """Fit the width (and the height, if that doesn't make it too small to read);
        a long flow then scrolls with the wheel."""
        if self.flow is None:
            return
        if self.winfo_width() < 50:  # not shown yet: fit when it is
            self.needs_fit = True
            return
        self.needs_fit = False
        layout = Layout(self.flow)
        w, h = self.winfo_width(), self.winfo_height()
        self.zoom = min(w / layout.width, 1.1)
        if layout.height * self.zoom > h:
            self.zoom = max(min(self.zoom, h / layout.height), min(0.7, self.zoom))
        self.pan = ((w - layout.width * self.zoom) / 2, max((h - layout.height * self.zoom) / 2, 0))
        self.render()

    def scroll(self, dx, dy):
        self.pan = (self.pan[0] + dx, self.pan[1] + dy)
        self.render()

    def zoom_by(self, factor, cx=None, cy=None):
        if cx is None:
            cx, cy = self.winfo_width() / 2, self.winfo_height() / 2
        new = min(max(self.zoom * factor, 0.3), 2.0)
        wx, wy = (cx - self.pan[0]) / self.zoom, (cy - self.pan[1]) / self.zoom
        self.zoom = new
        self.pan = (cx - wx * new, cy - wy * new)
        self.render()
        self.editor.update_zoom()

    # --- drawing ----------------------------------------------------------------------

    def render(self):
        self.delete("all")
        self.targets = {}
        if self.flow is None:
            self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, fill=T.MUTED,
                             font=("Segoe UI", 12), justify="center",
                             text="No flows yet.\nClick “+ New flow” to make one.")
            return
        self.layout = layout = Layout(self.flow)
        z = self.zoom
        for points, colour, label, dashed, arrow in layout.edges:
            pts = [(self.sx(x), self.sy(y)) for x, y in points]
            self.create_line(*rounded_line(pts, 14 * z), smooth=True, width=max(1.5, 2 * z),
                             fill=T.FAINT if dashed and colour is None else (colour or "#8a8378"),
                             dash=(4, 4) if dashed else None,
                             arrow="last" if arrow else None, arrowshape=(8 * z, 9 * z, 3.5 * z))
            if label:
                (x0, y0), (x1, y1) = pts[0], pts[1]
                if y0 == y1:  # leaves sideways: label above that run
                    self.create_text(min(x0, x1) + 10 * z, y0 - 9 * z, anchor="w", text=label,
                                     fill=colour or T.MUTED, font=self.font(8, True))
                else:  # leaves downwards: label beside the line, near its top
                    self.create_text(x0 + 9 * z, y0 + 13 * z, anchor="w", text=label,
                                     fill=colour or T.MUTED, font=self.font(8, True))
        for x, y in layout.dots:
            r = 4 * z
            self.create_oval(self.sx(x) - r, self.sy(y) - r, self.sx(x) + r, self.sy(y) + r,
                             fill="#8a8378", outline="")
        for i, (key, x, y, w, h) in enumerate(layout.nodes):
            self.draw_node(f"n{i}", key, self.sx(x), self.sy(y), w * z, h * z)
        if not self.editor.app.busy():
            for i, (x, y, lane, index) in enumerate(layout.pluses):
                self.draw_plus(f"p{i}", self.sx(x), self.sy(y), lane, index)

    def draw_node(self, tag, key, x, y, w, h):
        z = self.zoom
        self.targets[tag] = ("node", key)
        if key in ("start", "end"):
            kind = key
            title = "Start" if key == "start" else "Done"
            loops = self.flow.loops
            sub = ("" if loops == 1 else ("repeats until stopped" if loops == 0 else f"runs {loops}×")) \
                if key == "start" else ("" if loops == 1 else "then from the top")
            enabled, thumb = True, None
        else:
            kind, title, sub = key.kind, key.title(), key.summary()
            enabled = key.enabled
            thumb = key.rule.preview if key.kind == "image" and key.rule is not None else None
        icon, colour = KINDS[kind]
        selected = key is self.selected or (key == "start" and self.selected == "start")
        running = key is self.running
        outline = T.GOOD if running else T.ACCENT if selected else T.BORDER
        if kind in ("start", "stop", "restart") and not (selected or running):
            outline = colour if kind == "start" else "#5b3a33" if kind == "stop" else "#4a3f66"
        rounded_rect(self, x, y, x + w, y + h, 12 * z, fill=T.CARD if enabled else T.PANEL,
                     outline=outline, width=(3 if running else 2 if selected else 1.2), tags=(tag,))
        # Icon tile.
        s = 34 * z
        ix, iy = x + 12 * z, y + (h - s) / 2
        rounded_rect(self, ix, iy, ix + s, iy + s, 8 * z, fill=T.BG, outline=T.BORDER, tags=(tag,))
        self.create_text(ix + s / 2, iy + s / 2, text=icon, fill=colour if enabled else T.FAINT,
                         font=self.font(13, True), tags=(tag,))
        tx = ix + s + 10 * z
        right = x + w - 10 * z
        if thumb is not None:
            tw, th = 46 * z, 34 * z
            photo = self.thumb(key, thumb, tw, th)
            self.create_rectangle(right - tw - 2 * z, y + (h - th) / 2 - 2 * z, right, y + (h + th) / 2 + 2 * z,
                                  fill=T.BG, outline=T.BORDER, tags=(tag,))
            self.create_image(right - tw / 2 - z, y + h / 2, image=photo, tags=(tag,))
            right -= tw + 8 * z
        width = right - tx
        title_font, sub_font = self.font(10, True), self.font(8)
        if not enabled:
            title += "  (off)"
        self.create_text(tx, y + h / 2 - (8 * z if sub else 0), anchor="w",
                         text=fit_text(title, title_font, width),
                         fill=T.TEXT if enabled else T.FAINT, font=title_font, tags=(tag,))
        if sub:
            self.create_text(tx, y + h / 2 + 10 * z, anchor="w", text=fit_text(sub, sub_font, width),
                             fill=T.MUTED if enabled else T.FAINT, font=sub_font, tags=(tag,))

    def thumb(self, step, image, w, h):
        key = (id(image), round(w), round(h))
        if key not in self.photos:
            self.photos[key] = self.editor.app.gui.to_photo(self, image, w, h)
        return self.photos[key]

    def draw_plus(self, tag, x, y, lane, index):
        z = max(self.zoom, 0.6)
        r = 9 * z
        self.targets[tag] = ("plus", lane, index)
        self.create_oval(x - r, y - r, x + r, y + r, fill=T.PANEL, outline="#8a8378",
                         width=1.2, tags=(tag, "plus"))
        self.create_text(x, y - 0.5, text="+", fill=T.TEXT, font=self.font(10, True),
                         tags=(tag, "plus"))
        self.tag_bind(tag, "<Enter>", lambda e, t=tag: self.hover_plus(t, True))
        self.tag_bind(tag, "<Leave>", lambda e, t=tag: self.hover_plus(t, False))

    def hover_plus(self, tag, on):
        for item in self.find_withtag(tag):
            if self.type(item) == "oval":
                self.itemconfigure(item, fill=T.ACCENT_DARK if on else T.PANEL,
                                   outline=T.ACCENT if on else "#8a8378")
        self.configure(cursor="hand2" if on else "")

    # --- mouse ------------------------------------------------------------------------

    def target_at(self, event):
        for item in reversed(self.find_overlapping(event.x - 1, event.y - 1, event.x + 1, event.y + 1)):
            for tag in self.gettags(item):
                if tag in self.targets:
                    return self.targets[tag]
        return None

    def on_press(self, e):
        self.focus_set()
        target = self.target_at(e) if e.num == 1 else None
        self.drag = None
        if target and target[0] == "node":
            self.editor.select(target[1])
        elif target and target[0] == "plus":
            self.editor.insert_menu(target[1], target[2], e.x_root, e.y_root)
        else:
            self.drag = (e.x, e.y, self.pan)
            self.configure(cursor="fleur")

    def on_drag(self, e):
        if self.drag:
            x0, y0, (px, py) = self.drag
            self.pan = (px + e.x - x0, py + e.y - y0)
            self.render()

    def on_release(self, e):
        if self.drag:
            self.drag = None
            self.configure(cursor="")

    def on_double(self, e):
        target = self.target_at(e)
        if target and target[0] == "node" and isinstance(target[1], fl.Step):
            self.editor.edit_image(target[1])

    def on_right(self, e):
        target = self.target_at(e)
        if target and target[0] == "node" and isinstance(target[1], fl.Step):
            self.editor.select(target[1])
            self.editor.step_menu(target[1], e.x_root, e.y_root)

    def on_wheel(self, e):
        self.scroll(0, e.delta / 2)


def fit_text(text, font, width):
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


class FlowEditor(ttk.Frame):
    """The Flows tab: toolbar, graph and the selected step's settings."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.flow = None
        self.selected = None
        self.loading = False
        self.vars = []
        self.build()

    # --- layout -------------------------------------------------------------------------

    def build(self):
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        self.flow_var = tk.StringVar()
        self.flow_box = ttk.Combobox(bar, textvariable=self.flow_var, state="readonly", width=28)
        self.flow_box.pack(side="left")
        self.flow_box.bind("<<ComboboxSelected>>", lambda e: self.pick_flow(self.flow_box.current()))
        self.edit_buttons = []
        for text, cmd in (("✎", self.rename_flow), ("+  New flow", self.new_flow),
                          ("Delete flow", self.delete_flow)):
            b = ttk.Button(bar, text=text, style="Tool.TButton", command=cmd,
                           width=3 if text == "✎" else None)
            b.pack(side="left", padx=(6, 0))
            self.edit_buttons.append(b)
        self.counts = ttk.Label(bar, style="PanelMuted.TLabel")
        self.counts.pack(side="left", padx=14)
        self.stop_btn = ttk.Button(bar, text="■  Stop", style="Tool.TButton", command=self.app.stop_all)
        self.stop_btn.pack(side="right")
        self.pause_btn = ttk.Button(bar, text="⏸  Pause", style="Tool.TButton", command=self.app.toggle_pause)
        self.pause_btn.pack(side="right", padx=(0, 6))
        self.run_btn = ttk.Button(bar, text="▶  Run", style="Accent.TButton", command=self.run)
        self.run_btn.pack(side="right", padx=(0, 6))
        self.record_btn = ttk.Button(bar, text="●  Record steps", style="Tool.TButton",
                                     command=lambda: self.app.toggle_recording(into="flow"))
        self.record_btn.pack(side="right", padx=(0, 12))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        holder = ttk.Frame(body)
        holder.grid(row=0, column=0, sticky="nsew")
        self.canvas = FlowCanvas(holder, self)
        self.canvas.pack(fill="both", expand=True)
        # Zoom controls, floating bottom-left like the original.
        zoom = ttk.Frame(holder, style="Panel.TFrame", padding=4)
        zoom.place(x=12, rely=1.0, y=-12, anchor="sw")
        ttk.Button(zoom, text="+", width=3, style="Tool.TButton",
                   command=lambda: self.canvas.zoom_by(1.2)).pack()
        self.zoom_label = ttk.Label(zoom, text="100%", style="PanelMuted.TLabel", anchor="center",
                                    font=T.FONT_SMALL)
        self.zoom_label.pack(pady=2)
        ttk.Button(zoom, text="−", width=3, style="Tool.TButton",
                   command=lambda: self.canvas.zoom_by(1 / 1.2)).pack()
        ttk.Button(zoom, text="⤢", width=3, style="Tool.TButton",
                   command=self.fit).pack(pady=(6, 0))
        ttk.Label(holder, text="Drag or wheel to scroll · Ctrl+wheel zooms · click a step to edit it · "
                               "+ inserts a step · right-click for more", style="Muted.TLabel",
                  font=T.FONT_SMALL).place(relx=1.0, rely=1.0, x=-12, y=-8, anchor="se")

        self.side = ttk.Frame(body, style="Panel.TFrame", padding=14, width=330)
        self.side.grid(row=0, column=1, sticky="ns")
        self.side.grid_propagate(False)
        self.side.pack_propagate(False)

    def fit(self):
        self.canvas.fit()
        self.update_zoom()

    def update_zoom(self):
        self.zoom_label.configure(text=f"{self.canvas.zoom * 100:.0f}%")

    # --- flows --------------------------------------------------------------------------

    def refresh(self, keep=True):
        """Show the app's flows (after a load, a new config, ...)."""
        flows = self.app.flows
        names = [f.name for f in flows]
        self.flow_box.configure(values=names)
        index = flows.index(self.flow) if keep and self.flow in flows else (0 if flows else -1)
        self.pick_flow(index, fit=not keep)

    def pick_flow(self, index, fit=True):
        flows = self.app.flows
        self.flow = flows[index] if 0 <= index < len(flows) else None
        self.flow_var.set(self.flow.name if self.flow else "")
        self.canvas.flow = self.flow
        self.selected = None
        self.canvas.selected = None
        self.after_idle(self.fit if fit else self.canvas.render)
        self.show_side()
        self.update_counts()

    def new_flow(self):
        name = self.ask_name("New flow", f"Flow {len(self.app.flows) + 1}")
        if name:
            self.app.flows.append(fl.Flow(name))
            self.changed()
            self.refresh(keep=False)
            self.pick_flow(len(self.app.flows) - 1)
            self.select("start")

    def rename_flow(self):
        if self.flow:
            name = self.ask_name("Rename flow", self.flow.name)
            if name:
                self.rename_refs(self.flow.name, name)
                self.flow.name = name
                self.changed()
                self.refresh()

    def rename_refs(self, old, new):
        """Keep "Run another flow" steps and schedules pointing at a renamed flow."""
        for f in self.app.flows:
            for s in f.all_steps():
                if s.kind == "flow" and s.flow == old:
                    s.flow = new
        for s in self.app.schedules:
            if s.what == "flow" and s.target == old:
                s.target = new

    def users(self, name):
        """Flows and schedules that run the flow `name`."""
        found = {f"flow “{f.name}”" for f in self.app.flows for s in f.all_steps()
                 if s.kind == "flow" and s.flow == name}
        found |= {f"the schedule “{s.describe()}”" for s in self.app.schedules
                  if s.what == "flow" and s.target == name}
        return sorted(found)

    def delete_flow(self):
        users = self.users(self.flow.name) if self.flow else []
        warn = f"\n\nIt's run by: {', '.join(users)}." if users else ""
        if self.flow and messagebox.askyesno("Delete flow", f"Delete “{self.flow.name}” and all its "
                                             f"steps?{warn}", parent=self):
            self.app.flows.remove(self.flow)
            self.changed()
            self.refresh(keep=False)

    def ask_name(self, title, initial):
        name = simpledialog.askstring(title, "Name:", initialvalue=initial, parent=self)
        name = (name or "").strip()
        if name and any(f.name == name and f is not self.flow for f in self.app.flows):
            messagebox.showerror(title, f"There's already a flow called “{name}”.", parent=self)
            return None
        return name or None

    def changed(self, redraw=True):
        self.app.mark_dirty()
        self.update_counts()
        if redraw:
            self.canvas.render()

    def update_counts(self):
        if not self.flow:
            self.counts.configure(text="")
            return
        steps = self.flow.all_steps()
        branches = sum(s.kind in fl.LANE_NAMES and bool(s.found or s.missing) for s in steps)
        self.counts.configure(text=f"→  {len(steps)} step{'s' if len(steps) != 1 else ''}   ·   "
                                   f"{branches} branch{'es' if branches != 1 else ''}")

    # --- steps ----------------------------------------------------------------------------

    def find(self, step, steps=None):
        """The lane holding `step` and its index there, or (None, None)."""
        steps = self.flow.steps if steps is None else steps
        for i, s in enumerate(steps):
            if s is step:
                return steps, i
            for _, lane in s.lanes():
                found = self.find(step, lane)
                if found[0] is not None:
                    return found
        return None, None

    def select(self, key):
        self.selected = key
        self.canvas.selected = key
        self.canvas.render()
        self.show_side()

    def insert_menu(self, lane, index, x, y):
        if self.app.busy():
            return
        menu = tk.Menu(self, tearoff=False)
        for kind, label in fl.STEP_TYPES.items():
            menu.add_command(label=f"  {KINDS[kind][0]}   {label}",
                             command=lambda k=kind: self.insert(lane, index, k))
        menu.tk_popup(x, y)

    def insert(self, lane, index, kind):
        step = fl.new_step(kind)
        if kind == "image":
            rule = self.app.gui.RuleDialog(self.app, None).result
            if rule is None:
                return
            step.rule = rule
            step.name = rule.name
        elif kind == "macro" and self.app.macros:
            step.macro = self.app.macros[0].name
        elif kind == "flow":
            step.flow = next((f.name for f in self.app.flows if f is not self.flow), "")
        lane.insert(index, step)
        self.changed()
        self.select(step)

    def add_steps(self, steps):
        """Append steps (e.g. from a recording) to the flow's main line."""
        if self.flow is None:
            self.app.flows.append(fl.Flow(f"Flow {len(self.app.flows) + 1}"))
            self.refresh(keep=False)
        self.flow.steps.extend(steps)
        self.changed()
        self.fit()

    def step_menu(self, step, x, y):
        if self.app.busy():
            return
        menu = tk.Menu(self, tearoff=False)
        if step.kind == "image":
            menu.add_command(label="Edit image & action…", command=lambda: self.edit_image(step))
        menu.add_command(label="Switch off" if step.enabled else "Switch on",
                         command=lambda: self.toggle(step))
        menu.add_command(label="Duplicate", command=lambda: self.duplicate(step))
        menu.add_command(label="Move earlier", command=lambda: self.move(step, -1))
        menu.add_command(label="Move later", command=lambda: self.move(step, 1))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.delete(step))
        menu.tk_popup(x, y)

    def edit_image(self, step):
        if step.kind != "image" or self.app.busy():
            return
        rule = self.app.gui.RuleDialog(self.app, step.rule).result
        if rule is not None:
            if step.name in ("", step.rule.name):
                step.name = rule.name
            step.rule = rule
            self.changed()
            self.show_side()

    def toggle(self, step):
        step.enabled = not step.enabled
        self.changed()
        self.show_side()

    def duplicate(self, step):
        lane, i = self.find(step)
        if lane is not None:
            twin = copy.deepcopy(step)
            lane.insert(i + 1, twin)
            self.changed()
            self.select(twin)

    def move(self, step, delta):
        lane, i = self.find(step)
        if lane is not None and 0 <= i + delta < len(lane):
            lane[i], lane[i + delta] = lane[i + delta], lane[i]
            self.changed()

    def delete(self, step):
        lane, i = self.find(step)
        if lane is None:
            return
        inner = sum(len(l) for _, l in step.lanes())
        if inner and not messagebox.askyesno(
                "Delete step", f"Delete “{step.title()}” and the {inner} step(s) in its "
                               "lanes?", parent=self):
            return
        del lane[i]
        self.changed()
        self.select(None)

    # --- running --------------------------------------------------------------------------

    def run(self):
        if self.flow is None:
            return
        if not self.flow.steps:
            messagebox.showinfo("Run flow", "Add a step first: click the + on the line.", parent=self)
            return
        self.app.start_flow(self.flow)

    def show_running(self, step):
        self.canvas.running = step
        self.canvas.render()

    def set_busy(self, busy, running=False, recording=False):
        state = "disabled" if busy else "normal"
        for b in self.edit_buttons + [self.run_btn]:
            b.configure(state=state)
        self.flow_box.configure(state="disabled" if busy else "readonly")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.pause_btn.configure(state="normal" if running else "disabled")
        self.record_btn.configure(state="disabled" if running else "normal",
                                  text="■  Stop recording" if recording else "●  Record steps")
        self.canvas.render()
        self.show_side()

    # --- the side panel -------------------------------------------------------------------

    def show_side(self):
        for w in self.side.winfo_children():
            w.destroy()
        self.vars = []
        key = self.selected
        if self.flow is None:
            ttk.Label(self.side, text="Flows", style="PanelTitle.TLabel").pack(anchor="w")
            self.note("A flow runs steps in order. An “If image appears” step branches: only its "
                      "Found or Not found lane runs, then the flow carries on along the main line.")
            ttk.Button(self.side, text="+  New flow", style="Accent.TButton",
                       command=self.new_flow).pack(anchor="w", pady=(12, 0))
            return
        if key is None:
            ttk.Label(self.side, text=self.flow.name, style="PanelTitle.TLabel").pack(anchor="w")
            self.note("Click a step to edit it, or the + on a line to add one there.\n\n"
                      "An “If image appears” step splits in two: the Found lane runs when the image "
                      "shows up in time, the Not found lane when it doesn't. Either way the flow then "
                      "carries on along the main line after it.\n\n"
                      f"{self.app.cfg.start_key} runs, {self.app.cfg.pause_key} pauses, "
                      f"{self.app.cfg.stop_key} stops, even while the game has focus.")
            return
        busy = self.app.busy()
        if key == "start":
            self.header("start", "Start", "Flow settings")
            name = self.field_entry("Name", self.flow.name, self.set_flow_name)
            if busy:
                name.configure(state="disabled")
            self.field_spin("Run the whole flow (times)", self.flow.loops, 0, 100000, 1, int,
                            lambda v: setattr(self.flow, "loops", max(v, 0)), "0 = until you stop it")
            return
        if key == "end":
            self.header("end", "Done", "The end of the main line")
            self.note("When the main line gets here, the flow ends, or starts again from the top if "
                      "it's set to run more than once (click Start).")
            return
        step = key
        self.header(step.kind, step.title(), fl.STEP_TYPES[step.kind])
        self.field_entry("Name", step.name, lambda v: setattr(step, "name", v),
                         placeholder=fl.STEP_TYPES[step.kind])
        on = tk.BooleanVar(value=step.enabled)
        ttk.Checkbutton(self.side, text="On (a switched-off step is skipped)", variable=on,
                        style="Panel.TCheckbutton",
                        command=lambda: self.toggle(step)).pack(anchor="w", pady=(6, 0))
        getattr(self, f"side_{step.kind}")(step)
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x", side="bottom")
        for text, cmd in (("◀", lambda: self.move(step, -1)), ("▶", lambda: self.move(step, 1)),
                          ("Duplicate", lambda: self.duplicate(step)), ("Delete", lambda: self.delete(step))):
            b = ttk.Button(row, text=text, style="Tool.TButton", command=cmd,
                           width=3 if len(text) == 1 else None)
            b.pack(side="left", padx=(0, 4))
            if busy:
                b.configure(state="disabled")
        if busy:
            for w in self.side.winfo_children():
                self.disable(w)

    def disable(self, widget):
        try:
            if isinstance(widget, (ttk.Entry, ttk.Spinbox, ttk.Button, ttk.Checkbutton, ttk.Combobox)):
                widget.configure(state="disabled")
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self.disable(child)

    def header(self, kind, title, subtitle):
        icon, colour = KINDS[kind]
        top = ttk.Frame(self.side, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 10))
        tk.Label(top, text=icon, fg=colour, bg=T.CARD, font=("Segoe UI", 15, "bold"),
                 width=2).pack(side="left", ipady=4)
        text = ttk.Frame(top, style="Panel.TFrame")
        text.pack(side="left", padx=10)
        ttk.Label(text, text=title, style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(text, text=subtitle, style="PanelMuted.TLabel").pack(anchor="w")

    def note(self, text):
        ttk.Label(self.side, text=text, style="PanelMuted.TLabel", wraplength=295,
                  justify="left").pack(anchor="w", pady=(8, 0))

    def label(self, text):
        ttk.Label(self.side, text=text, style="Panel.TLabel").pack(anchor="w", pady=(10, 2))

    def field_entry(self, label, value, apply, placeholder=""):
        self.label(label)
        var = tk.StringVar(value=value)
        entry = ttk.Entry(self.side, textvariable=var)
        entry.pack(fill="x")

        def on_change(*a):
            apply(var.get().strip())
            self.changed()
        var.trace_add("write", on_change)
        self.vars.append(var)
        return entry

    def field_spin(self, label, value, low, high, step, kind, apply, note=None):
        self.label(label)
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x")
        var = tk.StringVar(value=f"{value:g}" if isinstance(value, float) else str(value))
        spin = ttk.Spinbox(row, textvariable=var, from_=low, to=high, increment=step, width=8)
        spin.pack(side="left")
        if note:
            ttk.Label(row, text=note, style="PanelMuted.TLabel").pack(side="left", padx=8)

        def on_change(*a):
            try:
                v = kind(var.get())
            except ValueError:
                return  # mid-typing
            if low <= v <= high:
                apply(v)
                self.changed()
        var.trace_add("write", on_change)
        self.vars.append(var)
        return spin

    def set_flow_name(self, name):
        if name and not any(f.name == name and f is not self.flow for f in self.app.flows):
            self.rename_refs(self.flow.name, name)
            self.flow.name = name
            self.flow_box.configure(values=[f.name for f in self.app.flows])
            self.flow_var.set(name)

    # One side panel per step kind.

    def side_image(self, step):
        gui = self.app.gui
        r = step.rule
        box = tk.Label(self.side, bg=T.BG, relief="flat", bd=0)
        box.pack(fill="x", pady=(12, 0), ipady=6)
        self.side_photo = gui.to_photo(self.side, r.preview, 290, 110)
        box.configure(image=self.side_photo)
        needs = (f"a patch of {r.min_area} px" if r.detect == "color"
                 else f"similarity {r.threshold:.2f}")
        self.note(f"Looks for {r.describe_target()} in: {gui.describe_region(r.region).lower()}, "
                  f"{needs}.\nWhen found: {gui.describe_action(r).lower()}.")
        ttk.Button(self.side, text="Edit image & action…", style="Accent.TButton",
                   command=lambda: self.edit_image(step)).pack(anchor="w", pady=(10, 0))
        self.field_spin("Wait for it up to (s)", step.timeout, 0, 3600, 0.5, float,
                        lambda v: setattr(step, "timeout", v), "0 = look once")
        test = ttk.Frame(self.side, style="Panel.TFrame")
        test.pack(fill="x", pady=(10, 0))
        result = ttk.Label(test, style="PanelMuted.TLabel")
        ttk.Button(test, text="Test on screen now", style="Tool.TButton",
                   command=lambda: self.test_image(step, result)).pack(side="left")
        result.pack(side="left", padx=8)
        self.note(f"Found lane: {len(step.found)} step(s). Not found lane: {len(step.missing)} "
                  "step(s). Only one of them runs; then the flow continues after this step. "
                  "(The rule's “wait after acting” isn't used in flows.)")

    def test_image(self, step, label):
        frame, origin = self.app.while_hidden(self.app.gui.capture_desktop)
        try:
            hit = step.rule.find(frame, origin)
        except ValueError as exc:
            label.configure(text=str(exc), foreground=T.BAD, wraplength=200)
            return
        if hit is None:
            label.configure(text="search area too small", foreground=T.BAD)
            return
        ok = hit.score >= step.rule.needed
        label.configure(text=f"{hit.score:.3f}: {'found ✓' if ok else 'not found'}",
                        foreground=T.GOOD if ok else T.BAD)

    def side_key(self, step):
        r = step.rule
        self.label("Key")
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x")
        var = tk.StringVar(value=r.key)
        entry = ttk.Entry(row, textvariable=var, width=16)
        entry.pack(side="left")
        status = ttk.Label(row, style="PanelMuted.TLabel")

        def on_key(*a):
            try:
                im.parse_combo(var.get().strip())
            except ValueError:
                status.configure(text="unknown key", foreground=T.BAD)
                return
            status.configure(text="")
            r.key = var.get().strip()
            self.changed()
        var.trace_add("write", on_key)
        self.vars.append(var)
        ttk.Button(row, text="Record…", style="Tool.TButton",
                   command=lambda: self.record_key(var, status)).pack(side="left", padx=6)
        status.pack(side="left")
        self.side_action(step)

    def record_key(self, var, status):
        status.configure(text="press a key…", foreground=T.ACCENT)
        top = self.winfo_toplevel()
        top.focus_set()

        def on_press(event):
            if event.keycode in self.app.gui.MODIFIER_VKS:
                return "break"
            top.unbind("<KeyPress>")
            name = im.VK_TO_NAME.get(event.keycode)
            if name is None:
                status.configure(text=f"“{event.keysym}” isn't supported", foreground=T.BAD)
                return "break"
            mods = [m for m, bit in (("ctrl", 0x4), ("shift", 0x1), ("alt", 0x20000)) if event.state & bit]
            var.set("+".join(mods + [name]))
            return "break"
        top.bind("<KeyPress>", on_press)

    def side_click(self, step):
        r = step.rule
        self.point_fields(step, "Where", "Pick: drag a small box around the spot; its centre is clicked.")
        self.label("Button")
        button = tk.StringVar(value=r.button)
        box = ttk.Combobox(self.side, textvariable=button, values=list(im.MOUSE_BUTTONS),
                           state="readonly", width=10)
        box.pack(anchor="w")
        box.bind("<<ComboboxSelected>>", lambda e: (setattr(r, "button", button.get()), self.changed()))
        self.side_action(step)

    def point_fields(self, step, label, note):
        """x / y boxes and a Pick… button for step.point."""
        self.label(label)
        row = ttk.Frame(self.side, style="Panel.TFrame")
        row.pack(fill="x")
        xs, ys = tk.StringVar(value=str(step.point[0])), tk.StringVar(value=str(step.point[1]))
        for text, var in (("x", xs), ("y", ys)):
            ttk.Label(row, text=text, style="Panel.TLabel").pack(side="left")
            ttk.Spinbox(row, textvariable=var, from_=-20000, to=20000, width=7).pack(side="left", padx=(4, 10))

        def on_point(*a):
            try:
                step.point = (int(xs.get()), int(ys.get()))
            except ValueError:
                return
            self.changed()
        for var in (xs, ys):
            var.trace_add("write", on_point)
            self.vars.append(var)

        def pick():
            picked = self.app.while_hidden(lambda: im.select_region(self.app.root))
            if picked:
                left, top, w, h = picked.region
                xs.set(str(left + w // 2))
                ys.set(str(top + h // 2))
        ttk.Button(row, text="Pick…", style="Tool.TButton", command=pick).pack(side="left")
        self.note(note)

    def side_action(self, step):
        """Hold and spam settings shared by key and click steps."""
        r = step.rule
        if r.release_image:
            self.note(f"Lets go when {r.release_image.name} appears (set in the config file); "
                      "the hold time below is then the longest it holds.")
        verb = "click" if step.kind == "click" else "press"
        self.label("Hold and spam")
        grid = ttk.Frame(self.side, style="Panel.TFrame")
        grid.pack(fill="x")
        self.grid_spin(grid, 0, "Hold down (s)", r.hold, 0, 600, 0.1, float,
                       lambda v: (setattr(r, "hold", v), v > 0 and setattr(r, "repeat", 1)),
                       f"0 = a quick {verb}")
        self.grid_spin(grid, 1, f"Spam ({verb}es)" if verb == "press" else "Spam (clicks)",
                       r.repeat, 1, 10000, 1, int,
                       lambda v: setattr(r, "repeat", v if r.hold <= 0 else 1), "1 = once")
        self.grid_spin(grid, 2, "Every (s)", r.repeat_gap, 0, 60, 0.05, float,
                       lambda v: setattr(r, "repeat_gap", v), "between them")
        self.note(f"Spam needs a quick {verb} (hold 0). {self.app.cfg.stop_key} stops a long run.")

    def grid_spin(self, grid, row, label, value, low, high, step, kind, apply, note=""):
        """A compact label / spinbox / note row."""
        ttk.Label(grid, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=f"{value:g}" if isinstance(value, float) else str(value))
        ttk.Spinbox(grid, textvariable=var, from_=low, to=high, increment=step, width=7).grid(
            row=row, column=1, sticky="w", padx=8, pady=2)
        ttk.Label(grid, text=note, style="PanelMuted.TLabel").grid(row=row, column=2, sticky="w")

        def on_change(*a):
            try:
                v = kind(var.get())
            except ValueError:
                return  # mid-typing
            if low <= v <= high:
                apply(v)
                self.changed()
        var.trace_add("write", on_change)
        self.vars.append(var)

    def side_mouse(self, step):
        self.point_fields(step, "Move the mouse to",
                          "Pick: drag a small box around the spot; the mouse goes to its centre.")
        self.label("…or to where this step last clicked (leave empty for the spot above)")
        names = []
        for s in self.flow.all_steps():
            if s.kind in ("image", "click") and s.rule is not None \
                    and s.rule.action in ("click", "move") and s.title() not in names:
                names.append(s.title())
        target = tk.StringVar(value=step.target)
        box = ttk.Combobox(self.side, textvariable=target, values=names, width=28)
        box.pack(anchor="w")

        def on_target(*a):
            step.target = target.get().strip()
            self.changed()
        target.trace_add("write", on_target)
        self.vars.append(target)
        self.note("Only moves the mouse, without clicking. Steps are matched by name, so "
                  "keep the names unique. If that step hasn't clicked yet in this run, the "
                  "mouse stays where it is.")
        ttk.Button(self.side, text="Clear (use the spot above)", style="Tool.TButton",
                   command=lambda: target.set("")).pack(anchor="w", pady=(8, 0))

    def side_wait(self, step):
        self.field_spin("Wait (s)", step.seconds, 0, 86400, 0.5, float,
                        lambda v: setattr(step, "seconds", v))

    def side_repeat(self, step):
        self.field_spin("Repeat the steps inside (times)", step.times, 0, 1000000, 1, int,
                        lambda v: setattr(step, "times", v), "0 = forever")
        self.note(f"{len(step.body)} step(s) inside. Add them with the + on the line after this step. "
                  "When the repeats are done, the flow carries on after the block.")

    def side_macro(self, step):
        self.label("Macro")
        names = [m.name for m in self.app.macros]
        var = tk.StringVar(value=step.macro)
        box = ttk.Combobox(self.side, textvariable=var, values=names, state="readonly", width=28)
        box.pack(anchor="w")
        box.bind("<<ComboboxSelected>>", lambda e: (setattr(step, "macro", var.get()), self.changed()))
        if not names:
            self.note("No macros yet: record one on the Macros tab.")
        self.field_spin("Play it (times)", step.times, 0, 100000, 1, int,
                        lambda v: setattr(step, "times", v), "0 = until stopped")
        self.field_spin("Speed (×)", step.speed, 0, 20, 0.25, float,
                        lambda v: setattr(step, "speed", v), "0 = the macro's own")
        self.note("Its safeguards and checkpoints work as on the Macros tab. The Finished lane runs "
                  "when it plays to the end, the Failed lane when a checkpoint's image doesn't "
                  "appear in time; then the flow carries on after this step.")

    def side_flow(self, step):
        self.label("Flow to run")
        names = [f.name for f in self.app.flows if f is not self.flow]
        var = tk.StringVar(value=step.flow)
        box = ttk.Combobox(self.side, textvariable=var, values=names, state="readonly", width=28)
        box.pack(anchor="w")
        box.bind("<<ComboboxSelected>>", lambda e: (setattr(step, "flow", var.get()), self.changed()))
        self.note("Runs the other flow from its start (as many times as it's set to), then carries "
                  "on here. Chain flows this way: each one stays small and readable. A flow can't "
                  "run itself, even through another flow.")

    def side_type(self, step):
        self.label("Text")
        var = tk.StringVar(value=step.text)
        entry = ttk.Entry(self.side, textvariable=var)
        entry.pack(fill="x")

        def on_text(*a):
            step.text = var.get()
            self.changed()
        var.trace_add("write", on_text)
        self.vars.append(var)
        self.field_spin("Between characters (s)", step.gap, 0, 5, 0.01, float,
                        lambda v: setattr(step, "gap", v))
        self.note("Types into whatever has focus, as characters (any language). For game controls, "
                  "use “Press a key” steps instead: games often only read keys.")

    def side_scroll(self, step):
        self.field_spin("Notches", step.amount, -100, 100, 1, int,
                        lambda v: setattr(step, "amount", v), "+ up, − down")
        move = tk.BooleanVar(value=step.move)
        ttk.Checkbutton(self.side, text="Move the mouse here first", variable=move,
                        style="Panel.TCheckbutton",
                        command=lambda: (setattr(step, "move", move.get()), self.changed())
                        ).pack(anchor="w", pady=(10, 0))
        self.point_fields(step, "Where", "Pick: drag a small box around the spot. Without “move "
                                         "the mouse here first”, it scrolls wherever the mouse is.")

    def side_restart(self, step):
        self.note("Goes back to the start of the flow, e.g. at the end of a Not found lane to try "
                  "again. Anything after it in the same lane never runs (dashed line).")

    def side_stop(self, step):
        self.note("Ends the flow right here. Anything after it in the same lane never runs (dashed "
                  "line).")
