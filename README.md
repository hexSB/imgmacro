# imgmacro

An image-triggered macro for Windows. It watches the screen for template
images, colours or text, and clicks, moves the mouse, presses keys or plays a
recorded macro when one appears. Use **rules** that are checked all the time,
**flows** (step-by-step macros with branches, drawn as a graph), or plain
**recorded macros** that replay your mouse and keyboard with their timing.
**Autopilot** runs them on a schedule and keeps a game from kicking you for
being idle.

The code is a handful of files you can read end to end:

- `imgmacro.py`: the engine (rules, matching, input, click recording) and command line
- `imgflow.py`: flows, and `python imgflow.py run` to run one
- `imgplay.py`: recorded macros (recording, playback, safeguards, checkpoints)
- `imgauto.py`: Anti-AFK, schedules and the run history
- `imgbundle.py`: export and import of portable bundles
- `imgocr.py`: text detection with Windows' own OCR (optional)
- `imgmacro_gui.py`: the window, built on Python's built-in tkinter
- `imgflow_gui.py`, `imgplay_gui.py`, `imgauto_gui.py`: the Flows, Macros and Autopilot tabs
- `imgtheme.py`: the dark theme

It never touches the network. Input is sent through the Win32 `SendInput` API
using Python's built-in `ctypes`. The only third-party packages are `numpy`,
`opencv-python` (template matching) and `mss` (screen capture). Text detection
optionally adds Microsoft's Python bindings for Windows' built-in OCR engine;
everything else works without them.

## Setup

Requires Python 3.10+.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

For text detection (optional), also install:

```powershell
.venv\Scripts\python -m pip install -r requirements-ocr.txt
```

It reads the languages installed in Windows (Settings → Time & language →
Language), on this computer.

## The window

```powershell
.venv\Scripts\pythonw imgmacro_gui.py
```

`pythonw` opens the window without a console. It loads `config.json` from
this folder if it exists. You can also pass a config path as an argument.

The window has four tabs at the top: **Watch** (rules), **Flows**, **Macros**
and **Autopilot**. Settings (hotkeys, how often to check the screen, recording,
humanize, on-screen display) are under **⚙ Settings**; **Dry run** and
**Minimize while running** are next to it and apply to all of them.

### Watch: rules

1. **Add rule…** opens the rule editor:
   - **What to look for:** an image, a colour or text.
     - **An image:** click **Snip from screen…** and drag a box around
       the button or icon. The window hides itself while you snip. Set **Wait
       before snip** if you need a few seconds to switch to the game. Click the
       image (or **Adjust crop…**) to fine-tune the crop afterwards. The whole
       screenshot is kept, so you can widen the box as well as shrink it.
       Tick **Also find it at other sizes** if the game's window or UI scale
       changes (70–130%), and **Also by its outline** if its colours change
       (a different theme, a highlight). Both only kick in when the plain
       match misses, and are slower, so give the rule a search area.
     - **A colour:** **Pick colour from screen…** and click a pixel (or drag a
       box to take its most common colour). The rule acts on the middle of
       the biggest patch of that colour, once it's at least **a patch of**
       pixels big; **within ±** sets how far each of red, green and blue may
       be off. For **pixel detection**, click **Only that pixel** (and a patch
       of 1): the rule then checks just that one pixel, which is very fast.
     - **Text:** type the words to look for, e.g. `Claim reward`. Windows'
       own OCR reads the search area; case, spaces and punctuation don't
       matter, and the similarity setting allows a misread letter or two.
       Give it a search area: reading the whole screen takes a while.
   - **Where to look:** the whole screen, an area you pick, or **Near the
     snip** (100 px around where you snipped). A smaller area is faster and
     gives fewer false matches.
   - **When it appears:** click it, move the mouse to it, press a key, play a
     recorded macro, or only log it. **Record…** captures a key or combo such as `ctrl+s`. Tick
     **Spam** to click (or press the key) a number of times with a set delay
     between them, e.g. 20 times, 0.05 s apart. The stop key ends a run early.
   - **Matching:** how similar the screen must be (0.85 is a good start), and
     how long to wait before this rule may act again.
   - **Test on screen now** shows the current best score.
2. **Test now** in the main window scores every rule against the screen once.
3. **Start** runs the rules. Hotkeys work even while the game has focus:
   **F6** start, **F7** pause/resume, **F8** stop. You can change them under
   Settings. Turn on **Dry run** to watch the log without sending any input.
4. **File → Save** (Ctrl+S) writes the config, rules, flows, macros and
   schedules together. Snipped images are saved to a `templates` folder next
   to it, recordings to a `macros` folder.

### Flows: step by step, with branches

Rules all compete at once, so when two can match at the same time it's hard to
say which acts. A flow runs one step at a time instead:

- **If image appears** waits for an image, a colour or text (up to the time
  you set) and acts on it with everything a rule can do: click, hold, drag,
  spam, let go on a release image, press a key, play a macro. Then it
  branches: the **Found** lane runs if it showed up, the **Not found** lane if
  it didn't. Only that lane runs; when it ends, the flow carries on along the
  main line after the step. Lanes can hold any steps, including more image
  checks.
- **Play a macro** plays a recorded macro (N times, at its own speed or one
  you set), with its safeguards and checkpoints. It branches too: the
  **Finished** lane runs if it played to the end, the **Failed** lane if a
  checkpoint's image never appeared.
- **Run another flow** runs a flow from its start, then carries on. Chain
  small flows this way instead of building one huge one. A flow can't run
  itself, even through another flow.
- **Move mouse back** puts the mouse (without clicking) on a spot you pick,
  or where another step, picked by name, last clicked in this run.
- **Type text** types characters into whatever has focus (any language; for
  game controls use **Press a key**, since games often only read keys).
- **Scroll** turns the mouse wheel, where the mouse is or at a spot you pick.
- **Press a key**, **Click a point** (with hold and spam), **Wait**,
  **Repeat** (the steps inside it, N times or forever), **Back to start** and
  **Stop**.

The graph lays itself out top to bottom: the main line and each Found (or
Finished) lane go straight down, each Not found (or Failed) lane gets its own
column to the right, and both meet again at a dot under the step, so lines
never cross. There are no free-form connections to tangle.

- Click **+ New flow**, then the **+** on a line to insert a step there.
- Click a step to edit it on the right; double-click an image step (or
  **Edit image & action…**) for the full rule editor. Right-click for
  duplicate, move, switch off and delete.
- Click **Start** to set how many times the whole flow runs (0 = until you
  stop it).
- **● Record steps** records like the Watch tab, but the review adds the
  clicks and keys to the flow as image steps, in order (a spam-click burst
  becomes one step that clicks that many times). The pauses between them
  become **Wait** steps, so the flow keeps your timing; untick **Add the
  pauses between them as Wait steps** in the review to leave them out.
- **▶ Run** (or the start key) runs the flow on screen; the step running is
  outlined in green. **F7** pauses, **F8** stops.
- Drag or use the wheel to scroll; Ctrl+wheel zooms; **⤢** fits the flow.

### Macros: replay your mouse and keyboard

Sometimes you just want to do something once and have it repeated exactly. A
macro records every mouse move, click, wheel turn and key press, with its
timing, and plays it back.

1. Click **● Record macro**, switch to the game and play. Recording starts at
   your first click or key (with the mouse where it was just before), so the
   time it takes to get to the game isn't recorded. Clicks on imgmacro's own
   window and on the taskbar, keys typed into imgmacro, and the hotkeys are
   left out. Press **F8** (or **■ Stop recording**) to finish, and name it.
2. Pick it in the list and click **▶ Play** (or press the start key while the
   Macros tab is shown). **Speed** plays it faster or slower (every pause
   between inputs is divided by it); **Play it (times)** repeats it, 0 = until
   you stop it. **F7** pauses (and lets go of any held key or button), **F8**
   stops (and always lets go).
3. **Safeguards** keep a long, unattended run alive: rules, with any image,
   colour or text, that are checked all the time while the macro plays. When
   one appears (a disconnect dialog, a "you died" screen), playback pauses,
   lets go of anything held, the safeguard acts (e.g. clicks Reconnect), waits
   the **Wait after a safeguard acts** time, presses again whatever was held,
   and carries on exactly where it stopped.
4. **Checkpoints** wait at a moment in the recording until something appears:
   at 12.5 s, wait up to 30 s for the shop to open. A slow loading screen then
   can't throw the rest of the timing off. If it never appears, the macro
   stops (it failed: in a flow, the Failed lane runs) or carries on, as you
   choose. What it waits for can also be clicked.

A macro replays at the same screen positions, so keep the game where it was,
at the same size and display scale; imgmacro warns in the log when the desktop
size differs from the recording. Recordings are saved in `macros/` next to the
config, one readable JSON file each. **Duplicate** a macro to keep a variant
(a different speed or safeguards) without recording again. A macro can also be
played from a rule (action **Play a macro**) or a flow step.

Games that lock the mouse to steer the camera (e.g. Roblox while holding the
right button) report no mouse movement to Windows, so camera turns can't be
recorded; clicks and keys still are.

### Autopilot: schedules, Anti-AFK and the run history

- **Anti-AFK** stops a game from kicking you for being idle. Pick the game's
  window, how often (default every 15 minutes) and the key (default space,
  a jump), and tick **On**. Each time, it switches to the game, presses the
  key and switches straight back to what you were using. If Windows won't let
  it switch, it presses nothing, so the key can't land in the wrong window. It
  waits while you're recording.
- **Schedules** run a flow or a macro every N minutes, or daily at a time
  (on the days you tick). If something else is running at that moment the
  run is skipped and logged. They count from when the window opens; a run
  missed while it was closed isn't made up.
- **Run history** lists every run of rules, flows and macros: when, how long
  and how it ended (finished, stopped, failed, error), plus totals per flow
  and macro. It's kept in `<config>.stats.json` next to the config, so
  running things doesn't change the config itself.

All three work while the window is open, even minimized.

### Settings worth knowing

- **Record key** (default none): starts and stops recording on the tab
  you're on (rules, flow steps or a macro), so you don't have to come back to
  the window to begin.
- **Humanize**: rules and flow steps click a random few pixels off and wait a
  random bit longer each time, so they don't hit the exact same pixel at the
  exact same rhythm. Recorded macros and drags always replay exactly.
- **Show matches and what's running on screen** draws a box around each
  match for a moment, and a badge at the top of the screen saying what's
  running and which key stops it. The overlay lets every click through and is
  invisible to screen capture, so the macros never see it.

### Sharing: bundles

**File → Export bundle…** writes rules, flows and macros, with every image and
recording they need, to one `.imgbundle` file. A flow brings along the macros
and flows it runs. **File → Import bundle…** adds a bundle's contents to the
open config. Before writing anything, the import checks the whole bundle:
only the files its manifest lists, names that can't point outside the config's
folder, each file's size and SHA-256, images that open, recordings that parse,
and 200 MB at most. Nothing is overwritten: a file whose name is taken gets a
new one (or the existing one is used if it's identical), and a flow or macro
whose name is taken is imported as "Name (2)".

## Recording clicks and keys

**Record** is the quickest way to build rules: play normally and let imgmacro
watch what you click and which keys you press.

1. Click **● Record**, then switch to the game and click or press the things
   you want automated. Every click and key press takes a full-screen
   screenshot (with imgmacro's own window left out). The taskbar title shows
   how many it has.
2. **Spam-clicking makes one screenshot, not dozens.** Clicks in the same spot
   less than the **spam gap** (default 1 s) apart are counted against the
   previous screenshot. Holding a burst for 30 seconds still gives one
   screenshot, marked ×N. A quick click somewhere else still gets its own.
3. **Holding and dragging are recorded too.** Keep the button down for 0.3 s
   or more and it's recorded as a **hold**, with how long you held it. Move
   the mouse 8 px or more while it's down and it's a **drag**: the mouse is
   followed the whole time, and the rule replays the same path, at the same
   speed, from wherever the image is found. Holds and drags always get their
   own screenshot.
4. **Keys are recorded too.** A quick press becomes a "press the key" rule;
   holding a key for 0.3 s or more becomes a "hold the key" rule. Ctrl, shift
   and alt held with another key are recorded as a combo such as `ctrl+s`.
   Pressing the same key again within the spam gap shares a screenshot, like
   spam clicks. Since a key has no click position, its crop starts in the
   middle of the game window: move it to whatever tells you to press the key.
   Keys typed into imgmacro itself, and the start/pause/stop hotkeys, are
   never recorded. Only keys imgmacro can press are recorded: letters,
   digits, F1–F24, arrows, space, enter, tab, esc, backspace, punctuation
   (`` ` - = [ ] \ ; ' , . / ``) and the numpad. The Windows and menu keys
   are not.
5. Press **F8** (the stop key) or **■ Stop recording** to finish. Clicks on
   imgmacro's own window and on the taskbar are never recorded.
6. The **review** window opens with each screenshot already cropped around
   the click (the **auto-crop size**, default 96 px). The red cross marks
   where you pressed, and a blue line shows the path of a drag. Trim each box to just the button: drag a new box, drag
   its edges, zoom with the wheel, pan with right-drag, nudge with the arrow
   keys. Name the rules, skip or delete the ones you don't want, then click
   **Add rules**.

### Letting go at the right moment (charge bars)

Some games want you to hold and let go when a bar lines up with a mark. For
those, a hold can **let go when a release image appears** instead of after a
fixed time:

1. Record the hold as usual: hold, and let go when the bar is lined up.
2. In the review, pick the hold and switch to **Release image**. These are
   screenshots of the moments just before (and after) you let go. Use
   **◀ Earlier / Later ▶** to find the one where the bar is lined up. The last
   one before you let go is picked for you.
3. Draw a tight box around the part that tells you to let go, e.g. the top
   of the fill where it meets the mark. Leave out things that don't change.

While the rule holds the button it checks that box every few milliseconds
and lets go as soon as it looks like your crop. If the screenshot you picked
was taken 30 ms before you let go, the rule also waits 30 ms, so it copies
your timing. If the bar never lands on exactly the same pixels, it lets go
just after the closest match. If the image never appears, it gives up and
lets go after **Hold for at most** seconds.

This works the same for a held key. You can also set it up by hand in the
rule editor: give a click or key rule a hold time, then **Snip release image…** under **When it appears**. **then
wait (ms)** adjusts the timing: raise it if the rule lets go too early.

Each recorded rule clicks exactly where you clicked, relative to the image,
even if your crop isn't centred on that spot. Spam-clicked rules start with
no wait after clicking, so they keep clicking while the image is on screen.
Afterwards, every rule can be edited like any other.

The full screenshots are kept in a temporary folder and deleted as soon as
the review is closed, so nothing of your screen is left behind. Only the
cropped templates are saved.

## Running

Rules are checked from top to bottom (use ▲ ▼ to reorder), and only the first
match acts on each check. The **Score** column updates live while running.
Double-click a rule to edit it, or press Space to switch it on or off.

Once an image has been found, the next check looks right around where it was
first, which takes a millisecond instead of a full-screen search; the whole
screen is only searched again when it has moved or gone.

While a macro runs, the window is hidden from screen capture so the macro
can't match the thumbnails in its own window. You still see it normally, but
screenshots and screen recordings taken while it runs will show what's behind
it.

## Command line

The window and the command line use the same config files.

```powershell
# flows and macros
.venv\Scripts\python imgflow.py run config.json "Flow name" --dry-run
.venv\Scripts\python imgplay.py record config.json "Macro name"      # F8 stops
.venv\Scripts\python imgplay.py run config.json "Macro name" --speed 2 --loops 3
.venv\Scripts\python imgplay.py list config.json

# unattended
.venv\Scripts\python imgauto.py windows                              # windows Anti-AFK can pick
.venv\Scripts\python imgauto.py antiafk "Roblox" --every 15 --key space
.venv\Scripts\python imgauto.py schedule config.json                 # run the config's schedules

# bundles
.venv\Scripts\python imgbundle.py export config.json farm.imgbundle --flow "Farm" --rules
.venv\Scripts\python imgbundle.py import config.json farm.imgbundle

# a colour (and where it is) for a colour rule
.venv\Scripts\python imgmacro.py pick --delay 3
```

## Usage

1. **Capture a template.** Run this, switch to your game, and drag a box
   around the thing to detect (a button, an icon, a warning):

   ```powershell
   .venv\Scripts\python imgmacro.py snip templates\accept.png --delay 3
   ```

   Run `snip` with no file name to only print a region's coordinates, for
   restricting where a rule looks.

2. **Write a config.** Copy `config.example.json` to `config.json` and edit it.
   Image paths are relative to the config file.

3. **Check the scores** without acting:

   ```powershell
   .venv\Scripts\python imgmacro.py test config.json
   ```

4. **Run it.** Use `--dry-run` first to see what it would do:

   ```powershell
   .venv\Scripts\python imgmacro.py run config.json --dry-run
   .venv\Scripts\python imgmacro.py run config.json
   ```

   **F8** stops and **F7** pauses or resumes. Both keys can be changed in the
   config.

## Config

| Key          | Default | Meaning                                    |
|--------------|---------|--------------------------------------------|
| `interval`   | `0.25`  | Seconds between screen checks              |
| `start_key`  | `"F6"`  | Start key (window only)                    |
| `record_gap` | `1.0`   | Spam gap for recording (window only)       |
| `record_crop`| `96`    | Auto-crop size for recording (window only) |
| `stop_key`   | `"F8"`  | Stop key, read even when the game has focus |
| `pause_key`  | `"F7"`  | Pause/resume toggle                        |
| `record_key` | none    | Starts and stops recording (window only)   |
| `humanize_px`, `humanize_ms` | `0` | Vary rule and step clicks by up to this many px and ms |
| `show_matches` | `false` | On-screen match boxes and status badge (window only) |
| `rules`      |         | Checked top to bottom; first match wins    |
| `flows`      |         | Flows, edited in the Flows tab (steps with `type`, `found`/`missing` lanes, …) |
| `macros`     |         | Macros: `name`, `file` (the recording), `speed`, `loops`, `guards`, `resume_delay`, `checkpoints` |
| `schedules`  |         | `what` (`flow`/`macro`), `target`, `kind` (`every`/`daily`), `every` (minutes) or `at` (`"09:30"`) and `days` (0 = Monday) |
| `anti_afk`   |         | `every` (minutes), `key`, and `window` (the last window's title, picked again when found) |

Newer keys are only written when used, so a config that doesn't use them
stays exactly as it was.

Per rule:

| Key              | Default   | Meaning                                                  |
|------------------|-----------|----------------------------------------------------------|
| `detect`         | `"image"` | `image`, `color` or `text`                               |
| `image`          | required for images | Template PNG                                   |
| `multiscale`     | `false`   | Image: also try 70–130% of its size                      |
| `edges`          | `false`   | Image: also try matching its outline                     |
| `color`          |           | Colour: `"#rrggbb"`                                      |
| `tolerance`      | `24`      | Colour: how far each of red, green, blue may be off      |
| `min_area`       | `20`      | Colour: the patch must be at least this many pixels      |
| `text`           |           | Text: the words to find (needs the OCR packages)         |
| `name`           | `rule N`  | Shown in the log                                         |
| `enabled`        | `true`    | Switch the rule off without deleting it                  |
| `threshold`      | `0.85`    | Match score 0–1 needed to fire (images and text)         |
| `region`         | whole desktop | `[left, top, width, height]` to search in            |
| `action`         | `"click"` | `click`, `move`, `key`, `macro`, or `none` (log only)    |
| `macro`          |           | For `macro`: the name of the macro to play               |
| `button`         | `"left"`  | `left`, `right`, `middle`                                |
| `offset`         | `[0, 0]`  | Pixels added to the match centre before clicking/moving  |
| `key`            |           | For `key`: `"space"`, `"e"`, `"ctrl+s"`, `"F5"`, `"grave"` (or `` "`" ``), `"num5"`, … |
| `cooldown`       | `1.0`     | Seconds before this rule may fire again                  |
| `pause_after`    | `0`       | Watching: after this rule acts, check no rule for this many seconds (recordings fill it in with your pause before the next action) |
| `restore_cursor` | `false`   | Put the mouse back after clicking                        |
| `repeat`         | `1`       | For `click`/`key`: spam it this many times when the image is seen |
| `repeat_gap`     | `0.1`     | Seconds from one spam click or press to the next          |
| `hold`           | `0`       | For `click`/`key`: seconds to hold it down (0 = a quick click or press) |
| `path`           |           | For `click`: a recorded drag, `[[seconds, dx, dy], ...]` from the press point |
| `release_image`  |           | For a hold: let go as soon as this image appears; `hold` is then the longest to hold |
| `release_region` | whole desktop | `[left, top, width, height]` to look for the release image in |
| `release_threshold` | `0.9`  | Match score needed to let go                             |
| `release_delay`  | `0`       | Seconds to keep holding after the release image appears  |

Only one rule fires per screen check. After an action the screen is captured
again, so higher rules in the list take priority.

## Tips

- **Templates must match the game's current scale.** Capture them at the
  resolution and UI scale you play at. If you change either, capture them
  again, or tick **Also find it at other sizes** (slower).
- A colour or a single pixel is the fastest thing to check, when the thing
  you're waiting for has a colour nothing else on screen has.
- Pick small, distinctive templates. Areas of flat colour match everywhere.
  Leave out parts that animate or change.
- A hold or drag keeps the button down until it ends, and the next screen
  check waits for it. The stop key still releases the button at once.
- Use `region` when you can. It is faster and avoids false matches elsewhere.
- Lower `threshold` if `test` shows real matches scoring around 0.7–0.8.
  Raise it if the wrong things trigger.
- If the game runs as administrator, Windows blocks input from normal
  programs. Run imgmacro from an administrator terminal in that case.
- Some games ignore synthetic input or treat macros as cheating. Check the
  game's rules. Using macros in online games such as Roblox can get your
  account banned.
