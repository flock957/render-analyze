# Render Jank Analysis (Perfetto)

Analyze Android render jank from a Perfetto trace and produce an HTML
report with annotated Perfetto-UI screenshots of each problem frame.

> **Branch policy.** `v4-stable` is the validated baseline. Use it.
> `v4-pipeline` is the leading-edge branch with experimental work in
> progress and is not guaranteed to produce correct screenshots.

## What it does

Given a `.perfetto-trace` from a problem device session, the workflow:

1. **Analyzes jank frames** via `trace_processor` SQL — finds the top
   problem frames by jank type, duration, and process.
2. **Captures Perfetto-UI screenshots** of each top frame —
   pins the right tracks (app main, RenderEngine, GPU completion,
   binder, HWC, crtc), zooms to the jank window, takes overview +
   detail screenshots.
3. **Generates an HTML report** with the screenshots embedded as
   base64 (single-file, easy to share).

Output: a self-contained `render_report.html` (~1.6 MB for a 64 MB
trace, 5 top jank issues × 2 screenshots each).

## Quick start

```bash
# 1. Clone the stable branch
git clone -b v4-stable https://github.com/flock957/render-analyze.git
cd render-analyze

# 2. Create a dedicated venv (DO NOT reuse other project venvs)
python3 -m venv .venv
.venv/bin/pip install perfetto playwright
.venv/bin/playwright install chromium

# 3. Run the full workflow
.venv/bin/python3 scripts/run_workflow.py \
  --trace /path/to/your.perfetto-trace \
  --output-dir /path/to/output
```

The workflow takes ~60 s for a 64 MB trace on a typical Linux dev box.

### If chromium is already cached

If you already have Playwright's Chromium downloaded somewhere
(e.g. from another project), point at it instead of re-downloading:

```bash
PLAYWRIGHT_BROWSERS_PATH=/path/to/ms-playwright \
.venv/bin/python3 scripts/run_workflow.py --trace ... --output-dir ...
```

## Requirements

- Python 3.10+ (tested on 3.12.3)
- `perfetto` (Python package, ≥ 0.16.0)
- `playwright` (≥ 1.57.0) + chromium browser
- *Optional, for faster trace loading*: a `trace_processor` binary on
  `$PATH` (or pass `--trace-processor /path/to/trace_processor` to
  `scripts/capture_screenshots.py`). If neither is available, the
  workflow automatically falls back to in-browser file upload mode,
  which is functionally equivalent and only marginally slower.
  Download from <https://perfetto.dev/docs/quickstart/trace-analysis>.
- A `.perfetto-trace` captured with frame timeline + render thread
  ftrace events enabled (`perfetto -c` config including
  `android.surfaceflinger.frametimeline`, `linux.ftrace` with the
  graphics atrace categories).

## Output structure

```
<output-dir>/
├── render_report.html              # 1.6 MB self-contained report
├── app_jank.json                   # Top jank frames per type
├── jank_types.json                 # Jank type distribution
├── sf_jank.json                    # SurfaceFlinger CPU misses
├── target_process.json             # Detected target process
├── thread_map.json                 # Pin patterns + tid map
├── tp_state.json                   # trace_processor session state
└── screenshots/
    ├── 00_<jank_type>_overview.png # Wide context (~120 KB)
    ├── 00_<jank_type>_detail.png   # Tight zoom slice readable (~120 KB)
    ├── 01_..._overview.png
    ├── 01_..._detail.png
    ├── ...
    └── screenshot_manifest.json    # Frame ts/dur/file map
```

## Repository layout

```
render-analyze/
├── README.md                  # This file
├── .gitignore
├── scripts/
│   ├── run_workflow.py        # Workflow entry point (use this)
│   ├── analyze_jank.py        # Phase 1: SQL jank analysis
│   ├── capture_screenshots.py # Phase 2: Perfetto UI screenshots
│   └── generate_report.py     # Phase 3: HTML report
├── skills/                    # AI-agent skill descriptions
│   ├── workflow.md            #   Top-level workflow
│   ├── analyze-jank.md        #   Phase 1 skill
│   ├── capture-screenshots.md #   Phase 2 skill
│   └── generate-report.md     #   Phase 3 skill
└── docs/
    ├── mambo_reference.md     # Honor Mambo screenshot skill (reference)
    └── modification_plan.md   # Future improvement plan (P0-P3)
```

## How the screenshot capture works

1. Start `trace_processor` HTTP RPC on a local port and load the
   trace once into memory.
2. Launch Playwright Chromium pointed at `https://ui.perfetto.dev`.
3. Wait for the UI to detect the RPC server and load the trace
   (typically ~9 s for a 64 MB trace).
4. For each top jank frame:
   - Unpin everything, expand parent groups, then pin the 9 patterns
     from `thread_map.json` (frame timeline, app main, surfaceflinger,
     RenderEngine, GPU completion, binder, HWC, crtc) in top-to-bottom
     order.
   - Zoom to the frame's `[ts, ts+dur]` plus padding for context.
   - Take an overview screenshot (wide context).
   - Re-zoom tighter for slice text readability.
   - Take a detail screenshot.

The whole capture phase is ~60 s for 5 frames × 2 screenshots each.

## Skill integration

The `skills/` directory holds Markdown skill descriptions designed
for AI-agent platforms (the workflow runs equivalently from the CLI).
Drop the contents of `skills/` into your agent's skill directory and
it can drive the workflow phase-by-phase, with reflection checkpoints
between phases.

The skill files invoke `python3` from `$PATH`. They assume the venv
from *Quick start* is already activated (`source .venv/bin/activate`)
or that `perfetto` and `playwright` are otherwise importable. Activate
the venv before launching your agent so its subprocess inherits the
right interpreter.

## Known limitations

- Defaults assume a vivo/Honor device target process. Edit the
  process detection in `scripts/analyze_jank.py` if your trace has
  a different app to focus on.
- The `trace_processor` binary path is hardcoded — see
  *Requirements*.
- Screenshots use the live `https://ui.perfetto.dev` over the
  internet. Air-gapped environments need a self-hosted Perfetto UI
  (not yet implemented; see `docs/modification_plan.md`).
- Perfetto UI version updates can change pin/zoom command IDs and
  break the capture flow. The current code targets the UI as of
  early April 2026.

## Reference

- Perfetto trace processor:
  <https://perfetto.dev/docs/analysis/trace-processor>
- Perfetto UI command API:
  <https://perfetto.dev/docs/visualization/perfetto-ui> (run
  `app.commands.commands` in the UI's devtools console for the live
  list)
- Honor Mambo screenshot skill (architectural reference, not used
  directly): see `docs/mambo_reference.md`

## License

Internal Honor / flock957. Not for redistribution.
