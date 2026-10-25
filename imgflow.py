"""imgflow: step-by-step macros with branches, built on the imgmacro engine.

A flow is a main line of steps run in order, optionally repeated. An "image"
step waits for an image (or a colour, or text) and acts on it like a rule,
then runs its Found lane or its Not found lane. A "macro" step plays a
recorded macro, then runs its Finished or Failed lane. Only that lane runs;
when it ends, the flow carries on along the main line after the step. Lanes
can hold any steps, including more branching steps, so branches nest but never
cross: there are no free-form jumps, only "back to start" and "stop". A "flow"
step runs another flow in place, so flows can be chained.

    python imgflow.py run config.json "Flow name" [--dry-run]

Flows live in the same config file as the rules, under "flows".
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import mss

import imgmacro as im

STEP_TYPES = {
    "image": "If image appears",
    "macro": "Play a macro",
    "key": "Press a key",
    "click": "Click a point",
    "type": "Type text",
    "scroll": "Scroll",
    "mouse": "Move mouse back",
    "wait": "Wait",
    "repeat": "Repeat",
    "flow": "Run another flow",
    "restart": "Back to start",
    "stop": "Stop",
}
# Steps that branch, and what their two lanes are called.
LANE_NAMES = {"image": ("Found", "Not found"), "macro": ("Finished", "Failed")}


@dataclass(eq=False)  # identity hashing: the GUI keys canvas items by step
class Step:
    kind: str                     # one of STEP_TYPES
    name: str = ""
    enabled: bool = True          # a switched-off step is skipped
    # image: what to look for and what to do when it's found, as a rule (its
    # cooldown is ignored), how long to wait for it, and the two lanes.
    rule: im.Rule | None = None
    timeout: float = 5.0          # seconds; 0 = look once
    found: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    # key / click: the action, without an image. Kept as a Rule too, so holds,
    # spam and the rest work the same as in rules.
    point: tuple = (0, 0)         # click / mouse / scroll: screen position
    target: str = ""              # mouse: go where this step last clicked instead of to `point`
    seconds: float = 1.0          # wait
    times: int = 2                # repeat: 0 = forever; macro: how many times to play it
    body: list = field(default_factory=list)  # repeat
    macro: str = ""               # macro: which one (it runs `found` when it finishes, else `missing`)
    speed: float = 0.0            # macro: playback speed; 0 = the macro's own
    flow: str = ""                # flow: the flow to run
    text: str = ""                # type
    gap: float = 0.03             # type: seconds between characters
    amount: int = -3              # scroll: wheel notches; negative is down
    move: bool = False            # scroll: move the mouse to `point` first

    def lanes(self):
        """The step lists inside this step, as (label, list)."""
        if self.kind in LANE_NAMES:
            return [("found", self.found), ("missing", self.missing)]
        if self.kind == "repeat":
            return [("body", self.body)]
        return []

    def title(self):
        return self.name or STEP_TYPES[self.kind]

    def summary(self):
        """A one-line description for the graph."""
        r = self.rule
        if self.kind == "image":
            wait = "look once" if self.timeout <= 0 else f"wait ≤ {self.timeout:g} s"
            return f"{wait}, then {action_words(r)}"
        if self.kind == "macro":
            times = "" if self.times == 1 else (" until stopped" if self.times == 0 else f" ×{self.times}")
            speed = f" at {self.speed:g}×" if self.speed else ""
            return f"“{self.macro}”{times}{speed}" if self.macro else "pick a macro"
        if self.kind == "flow":
            return f"“{self.flow}”, then carry on" if self.flow else "pick a flow"
        if self.kind == "type":
            return f"“{self.text}”" if self.text else "nothing yet"
        if self.kind == "scroll":
            where = f" at ({self.point[0]}, {self.point[1]})" if self.move else ""
            n = abs(self.amount)
            return f"{n} notch{'es' if n != 1 else ''} {'up' if self.amount > 0 else 'down'}{where}"
        if self.kind == "key":
            return action_words(r)
        if self.kind == "click":
            return f"{action_words(r)} at ({self.point[0]}, {self.point[1]})"
        if self.kind == "mouse":
            if self.target:
                return f"to where “{self.target}” last clicked"
            return f"to ({self.point[0]}, {self.point[1]})"
        if self.kind == "wait":
            return f"{self.seconds:g} seconds"
        if self.kind == "repeat":
            return "forever" if self.times == 0 else f"{self.times} times"
        if self.kind == "restart":
            return "run the flow again from the top"
        return "end the flow"


@dataclass(eq=False)
class Flow:
    name: str
    steps: list = field(default_factory=list)
    loops: int = 1                # times to run the whole flow; 0 = until stopped

    def all_steps(self):
        """Every step, depth first."""
        def walk(steps):
            for step in steps:
                yield step
                for _, lane in step.lanes():
                    yield from walk(lane)
        return list(walk(self.steps))


action_words = im.action_words  # 'left click', 'hold e 1.5 s', ... for a step's rule


def action_rule(kind="key", **fields):
    """A Rule with no image, for key and click steps."""
    rule = im.Rule(name=kind, image=Path(), template=None,
                   action="key" if kind == "key" else "click", **fields)
    if rule.action == "key" and not rule.key:
        rule.key = "space"
    return rule


def new_step(kind):
    """A step with sensible defaults (image steps get their image in the editor)."""
    step = Step(kind)
    if kind == "key":
        step.rule = action_rule("key", key="space")
    elif kind == "click":
        step.rule = action_rule("click")
        step.point = im.cursor_pos()
    elif kind in ("mouse", "scroll"):
        step.point = im.cursor_pos()
    elif kind == "macro":
        step.times = 1
    return step


# --- JSON ------------------------------------------------------------------------

ACTION_FIELDS = ("key", "button", "hold", "repeat", "repeat_gap", "restore_cursor")


def step_to_dict(step, base):
    data = {"type": step.kind}
    if step.name:
        data["name"] = step.name
    if not step.enabled:
        data["enabled"] = False
    if step.kind == "image":
        data["timeout"] = step.timeout
        data["rule"] = im.rule_to_dict(step.rule, base)
        data["found"] = [step_to_dict(s, base) for s in step.found]
        data["missing"] = [step_to_dict(s, base) for s in step.missing]
    elif step.kind in ("key", "click"):
        r = step.rule
        data.update({f: getattr(r, f) for f in ACTION_FIELDS
                     if not (step.kind == "key" and f in ("button", "restore_cursor"))
                     and not (step.kind == "click" and f == "key")})
        if step.kind == "click":
            data["point"] = list(step.point)
        if r.release_image:
            data["release_image"] = im._relative(r.release_image, base)
            data["release_threshold"] = round(r.release_threshold, 3)
            data["release_delay"] = round(r.release_delay, 3)
            data["release_region"] = list(r.release_region) if r.release_region else None
    elif step.kind == "mouse":
        data["point"] = list(step.point)
        if step.target:
            data["target"] = step.target
    elif step.kind == "wait":
        data["seconds"] = step.seconds
    elif step.kind == "repeat":
        data["times"] = step.times
        data["body"] = [step_to_dict(s, base) for s in step.body]
    elif step.kind == "macro":
        data.update(macro=step.macro, times=step.times)
        if step.speed:
            data["speed"] = step.speed
        data["found"] = [step_to_dict(s, base) for s in step.found]
        data["missing"] = [step_to_dict(s, base) for s in step.missing]
    elif step.kind == "flow":
        data["flow"] = step.flow
    elif step.kind == "type":
        data.update(text=step.text, gap=step.gap)
    elif step.kind == "scroll":
        data["amount"] = step.amount
        if step.move:
            data.update(move=True, point=list(step.point))
    return data


def step_from_dict(d, base):
    kind = d.get("type")
    if kind not in STEP_TYPES:
        raise ValueError(f"unknown step type {kind!r}")
    step = Step(kind, name=d.get("name", ""), enabled=bool(d.get("enabled", True)))
    if kind == "image":
        step.timeout = float(d.get("timeout", 5.0))
        step.rule = im.rule_from_dict(d["rule"], base)
        step.found = [step_from_dict(s, base) for s in d.get("found", [])]
        step.missing = [step_from_dict(s, base) for s in d.get("missing", [])]
    elif kind in ("key", "click"):
        fields = {f: d[f] for f in ACTION_FIELDS if f in d}
        step.rule = action_rule(kind, **fields)
        if d.get("release_image"):  # let go of a hold when this image appears, as in rules
            release = Path(d["release_image"])
            if not release.is_absolute():
                release = Path(base) / release
            step.rule.release_image = release
            step.rule.release_template = im.read_image(release)
            step.rule.release_threshold = float(d.get("release_threshold", 0.9))
            step.rule.release_delay = float(d.get("release_delay", 0.0))
            region = d.get("release_region")
            step.rule.release_region = tuple(region) if region else None
        step.rule.validate()
        if kind == "click":
            step.point = tuple(int(v) for v in d.get("point", (0, 0)))
    elif kind == "mouse":
        step.point = tuple(int(v) for v in d.get("point", (0, 0)))
        step.target = str(d.get("target", ""))
    elif kind == "wait":
        step.seconds = max(float(d.get("seconds", 1.0)), 0.0)
    elif kind == "repeat":
        step.times = max(int(d.get("times", 2)), 0)
        step.body = [step_from_dict(s, base) for s in d.get("body", [])]
    elif kind == "macro":
        step.macro = str(d.get("macro", ""))
        step.times = max(int(d.get("times", 1)), 0)
        step.speed = max(float(d.get("speed", 0.0)), 0.0)
        step.found = [step_from_dict(s, base) for s in d.get("found", [])]
        step.missing = [step_from_dict(s, base) for s in d.get("missing", [])]
    elif kind == "flow":
        step.flow = str(d.get("flow", ""))
    elif kind == "type":
        step.text = str(d.get("text", ""))
        step.gap = min(max(float(d.get("gap", 0.03)), 0.0), 5.0)
    elif kind == "scroll":
        step.amount = int(d.get("amount", -3))
        step.move = bool(d.get("move", False))
        step.point = tuple(int(v) for v in d.get("point", (0, 0)))
    return step


def flow_to_dict(flow, base):
    return {"name": flow.name, "loops": flow.loops,
            "steps": [step_to_dict(s, base) for s in flow.steps]}


def flow_from_dict(d, base):
    return Flow(name=d.get("name", "Flow"), loops=max(int(d.get("loops", 1)), 0),
                steps=[step_from_dict(s, base) for s in d.get("steps", [])])


def load_flows(cfg, config_path):
    base = Path(config_path).parent
    return [flow_from_dict(d, base) for d in cfg.flows]


def store_flows(cfg, flows, config_path):
    """Put `flows` into `cfg` as JSON, ready for im.save_config(cfg, config_path)."""
    base = Path(config_path).parent
    cfg.flows = [flow_to_dict(f, base) for f in flows]


# --- running ----------------------------------------------------------------------

class _Stop(Exception):
    """The flow is over: a Stop step, the stop key, or Stop in the window."""


class _Restart(Exception):
    """A Back to start step."""


class FlowRunner(im.Control):
    """Run a flow on a worker thread, like Watcher does for rules.

    Callbacks fire on the runner thread: `on_log(text)`, `on_step(step or None)`
    when a step starts (None when the flow ends), `on_pause(paused)`, and
    `on_hit(rule, hit)` when an image step finds its image. `flows` (a list) are
    the flows a "flow" step can run, `macros` ({name: imgplay.Macro}) the macros
    a "macro" step (or an image step's "macro" action) can play.
    """

    MAX_DEPTH = 8  # flows running flows, this deep at most

    def __init__(self, flow, cfg, dry_run=False, on_log=print, on_step=None, on_pause=None,
                 on_hit=None, flows=(), macros=None):
        super().__init__(cfg, on_log, on_pause)
        self.flow, self.dry_run = flow, dry_run
        self.on_step, self.on_hit = on_step, on_hit
        self.flows = {f.name: f for f in flows}
        self.macros = macros or {}
        self.calls = [flow.name]  # the flows running, outermost first
        self.sct = None
        self.points = {}  # step title -> where it last clicked, for "mouse" steps

    def checkpoint(self):
        """Between and inside steps: stop if asked, and wait here while paused."""
        while True:
            if self.stopping():
                raise _Stop
            if not self.paused:
                return
            time.sleep(0.02)

    def sleep(self, seconds):
        end = time.perf_counter() + seconds
        while True:
            self.checkpoint()
            left = end - time.perf_counter()
            if left <= 0:
                return
            time.sleep(min(left, 0.01))

    def log(self, text):
        self.on_log(f"{time.strftime('%H:%M:%S')}  {text}")

    def run(self):
        im.set_humanize(self.cfg.humanize_px, self.cfg.humanize_ms)
        try:
            with mss.MSS() as self.sct:
                self.run_flow(self.flow)
                self.log(f"{self.flow.name}: finished.")
                self.outcome = "finished"
        except _Stop:
            self.outcome = "stopped"
        except Exception as exc:  # report, rather than silently kill the GUI's worker thread
            self.outcome = "error"
            self.on_log(f"error: {exc}")
        finally:
            if self.on_step:
                self.on_step(None)
            self.on_log("Stopped.")

    def run_flow(self, flow):
        """A flow's steps, as many times as it says ("back to start" starts it again)."""
        loops = flow.loops
        n = 0
        while loops == 0 or n < loops:
            n += 1
            if loops != 1:
                self.log(f"{flow.name}: run {n}" + (f" of {loops}" if loops else ""))
            try:
                self.run_steps(flow.steps)
            except _Restart:
                continue

    def run_steps(self, steps):
        for step in steps:
            if step.enabled:
                self.checkpoint()
                if self.on_step:
                    self.on_step(step)
                self.run_step(step)

    def run_step(self, step):
        kind = step.kind
        if kind == "image":
            lane = step.found if self.wait_for(step) else step.missing
            self.run_steps(lane)  # then back to the main line, after this step
        elif kind == "key":
            self.act(step, step.rule, 0, 0)
        elif kind == "click":
            self.act(step, step.rule, *step.point)
        elif kind == "mouse":
            point = self.points.get(step.target) if step.target else step.point
            if point is None:
                self.log(f"{step.title()}: “{step.target}” hasn't clicked yet, mouse left where it is")
            elif self.dry_run:
                self.log(f"{step.title()}: would move the mouse to ({point[0]}, {point[1]})")
            else:
                im.move_to(*point)
                self.log(f"{step.title()}: mouse back to ({point[0]}, {point[1]})")
        elif kind == "wait":
            self.sleep(step.seconds)
        elif kind == "repeat":
            i = 0
            while step.times == 0 or i < step.times:
                i += 1
                self.run_steps(step.body)
        elif kind == "macro":
            outcome = self.play(step.macro, self.stopping, step.times, step.speed or None)
            if outcome == "stopped":
                raise _Stop
            self.run_steps(step.found if outcome == "finished" else step.missing)
        elif kind == "flow":
            self.run_subflow(step)
        elif kind == "type":
            if self.dry_run:
                self.log(f"{step.title()}: would type “{step.text}”")
            else:
                note = im.type_text(step.text, step.gap, cancel=self.stopping)
                if note:
                    self.log(f"{step.title()}: {note}")
            self.checkpoint()
        elif kind == "scroll":
            where = f" at ({step.point[0]}, {step.point[1]})" if step.move else ""
            if self.dry_run:
                self.log(f"{step.title()}: would scroll {step.amount:+d}{where}")
            else:
                if step.move:
                    im.move_to(*step.point)
                    time.sleep(0.01)
                im.scroll(step.amount)
        elif kind == "restart":
            self.log(f"{step.title()}: back to the start")
            raise _Restart
        elif kind == "stop":
            self.log(f"{step.title()}: stop")
            raise _Stop

    def play(self, name, cancel, times=None, speed=None):
        """Play a macro by name. -> "finished", "failed" or "stopped"."""
        import imgplay  # here: imgplay imports imgmacro, which this file builds on
        macro = self.macros.get(name)
        if macro is None:
            self.log(f"there's no macro called “{name}”")
            return "failed"
        return imgplay.play(macro, self.cfg, stopping=cancel, paused=lambda: self.paused,
                            on_log=self.on_log, dry_run=self.dry_run, speed=speed, loops=times,
                            on_hit=self.on_hit)

    def run_subflow(self, step):
        flow = self.flows.get(step.flow)
        if flow is None:
            self.log(f"{step.title()}: there's no flow called “{step.flow}”; stopping")
            raise _Stop
        if flow.name in self.calls:
            self.log(f"{step.title()}: “{flow.name}” is already running (a flow can't run "
                     "itself, even through another flow); stopping")
            raise _Stop
        if len(self.calls) >= self.MAX_DEPTH:
            self.log(f"{step.title()}: flows run flows {self.MAX_DEPTH} deep; stopping")
            raise _Stop
        self.log(f"{step.title()}: running “{flow.name}”")
        self.calls.append(flow.name)
        try:
            self.run_flow(flow)
        finally:
            self.calls.pop()
        self.log(f"{step.title()}: “{flow.name}” finished, carrying on")

    def wait_for(self, step):
        """Look for the step's image until found or timed out. -> True if found (and acted on)."""
        rule = step.rule
        bounds = im.capture_bounds(self.sct, [rule])
        deadline = time.perf_counter() + step.timeout
        best = 0.0
        while True:
            self.checkpoint()
            tick = time.perf_counter()
            hit = rule.find(*im.grab(self.sct, bounds))
            if hit is not None:
                best = max(best, hit.score)
                if hit.score >= rule.needed:
                    how = f", {hit.how}" if hit.how else ""
                    self.log(f"{step.title()}: found ({hit.score:.3f}{how}) at ({hit.x}, {hit.y})")
                    if self.on_hit:
                        self.on_hit(rule, hit)
                    self.act(step, rule, hit.x, hit.y)
                    return True
            if tick >= deadline:
                self.log(f"{step.title()}: not found (best {best:.3f})")
                return False
            self.sleep(max(self.cfg.interval - (time.perf_counter() - tick), 0))

    def act(self, step, rule, x, y):
        if rule.action == "none":
            return
        if rule.action in ("click", "move"):
            self.points[step.title()] = (x + rule.offset[0], y + rule.offset[1])
        if rule.action == "macro":
            outcome = self.play(rule.macro, self.stopping)
            if outcome == "stopped":
                raise _Stop
            self.log(f"{step.title()}: macro “{rule.macro}” {outcome}")
            return
        if self.dry_run:
            self.log(f"{step.title()}: would {action_words(rule)}")
            return
        note = rule.fire(x, y, cancel=self.stopping)
        if note:
            self.log(f"{step.title()}: {note}")
        if self.stop_event.is_set():
            raise _Stop


# --- command line ----------------------------------------------------------------

def main(argv=None):
    sys.stdout.reconfigure(errors="replace")  # window titles and names may not fit the console's code page
    parser = argparse.ArgumentParser(prog="imgflow", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run a flow (F8 stops, F7 pauses)")
    p.add_argument("config")
    p.add_argument("flow", help="the flow's name")
    p.add_argument("--dry-run", action="store_true", help="log actions instead of doing them")
    args = parser.parse_args(argv)
    try:
        cfg = im.load_config(args.config)
        flows = {f.name: f for f in load_flows(cfg, args.config)}
        if args.flow not in flows:
            raise ValueError(f"no flow named {args.flow!r} (have: {', '.join(flows) or 'none'})")
        macros = {}
        if cfg.macros:
            import imgplay
            macros = {m.name: m for m in imgplay.load_macros(cfg, args.config)}
    except (ValueError, OSError, KeyError) as exc:
        print(f"imgflow: {exc}", file=sys.stderr)
        return 2
    print(f"Running {args.flow!r}. {cfg.stop_key} stops, {cfg.pause_key} pauses.")
    runner = FlowRunner(flows[args.flow], cfg, dry_run=args.dry_run, flows=flows.values(),
                        macros=macros)
    runner.run()
    return 0 if runner.outcome == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
