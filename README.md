# imgmacro

Image-triggered macros for Windows. Watch the screen for an image, colour or
text, then click, press keys or play a recorded macro.

- **Watch:** rules checked all the time; the first match acts.
- **Flows:** step-by-step macros with branches, drawn as a graph.
- **Macros:** record your mouse and keyboard, replay at any speed, with
  safeguards (handle pop-ups mid-run) and checkpoints (wait for the screen).
- **Autopilot:** Anti-AFK, schedules and run history.
- **Bundles:** share rules, flows and macros as one checked file.

Runs locally, no network. Input goes through Win32 `SendInput`; dependencies
are `numpy`, `opencv-python` and `mss`.

## Setup

Python 3.10+.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -r requirements-ocr.txt   # optional: text detection
.venv\Scripts\pythonw imgmacro_gui.py
```

## Use

1. **Watch:** **Add rule…**, snip a button (or pick a colour, or type text),
   choose what happens, then **Start**.
2. **Flows:** **+ New flow**, click **+** on a line to add steps, **▶ Run**.
3. **Macros:** **● Record macro**, play, press **F8**, then **▶ Play**.
4. **Record** on the Watch tab turns your clicks and keys into rules.

Hotkeys work while the game has focus: **F6** start, **F7** pause, **F8** stop
(changeable under Settings). Tick **Dry run** to test without sending input.
**File → Save** writes the config, with images in `templates/` and recordings
in `macros/` next to it.

## Command line

```powershell
.venv\Scripts\python imgmacro.py run config.json --dry-run     # watch rules
.venv\Scripts\python imgflow.py run config.json "Flow"          # run a flow
.venv\Scripts\python imgplay.py run config.json "Macro" --speed 2
.venv\Scripts\python imgauto.py schedule config.json            # run schedules
.venv\Scripts\python imgbundle.py export config.json out.imgbundle --rules
```

See `config.example.json` for the config format.

## Tips

- Capture templates at the resolution and UI scale you play at.
- Small, distinctive templates and a search area are faster and more reliable.
- If the game runs as administrator, run imgmacro as administrator too.
- Macros can break a game's rules (e.g. Roblox) and get accounts banned.
