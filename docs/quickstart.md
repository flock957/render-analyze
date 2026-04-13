# Render Jank Analysis — New User Quickstart

Run `render-analyze` end-to-end on a fresh machine and get an HTML report
with annotated Perfetto screenshots for the top 5 jank frames in your
trace.

> **Branch**: this guide targets `feat/portrait-longshot`, the current
> leading branch. Screenshots are portrait long-shots (1072×1598 @ 2×)
> and include the full 7-layer graphics pipeline (SF main → HWC →
> Expected/Actual Timeline → App main → RenderThread → hwuiTask →
> GPU completion).

## 0. Prerequisites

- Linux (tested on Ubuntu 22.04 / 24.04)
- **Python 3.10+** (tested on 3.12.3)
- **git**
- ~800 MB free disk for the venv + Playwright's Chromium download
- A `.perfetto-trace` file captured with frame timeline + ftrace
  graphics categories. If you don't have one yet, any Perfetto trace
  from a jank session on a 60 Hz / 90 Hz Android device will do —
  e.g. record ~20 s of app use via `adb shell perfetto -c <config>` or
  the Perfetto UI recording page.

Internet access is required on the first run (Playwright downloads
Chromium, and the screenshot phase uses `https://ui.perfetto.dev`).

## 1. Clone and set up

```bash
# 1.1 Clone the portrait-longshot branch
git clone -b feat/portrait-longshot https://github.com/flock957/render-analyze.git
cd render-analyze

# 1.2 Create a dedicated venv (do NOT reuse another project's venv)
python3 -m venv .venv

# 1.3 Install Python deps
.venv/bin/pip install -r requirements.txt
# (equivalent to: .venv/bin/pip install perfetto playwright Pillow)

# 1.4 Download Playwright's Chromium (~130 MB)
.venv/bin/playwright install chromium
```

Total setup time on a fresh machine: ~1 min for the pip install,
~30 s for the Chromium download.

### Reusing an existing Chromium

If you already have Playwright's Chromium somewhere from another
project, point at it instead of downloading a second copy:

```bash
export PLAYWRIGHT_BROWSERS_PATH=/path/to/ms-playwright
```

## 2. Run the workflow

One command runs all three phases (analyze → screenshots → report):

```bash
.venv/bin/python3 scripts/run_workflow.py \
  --trace /path/to/your.perfetto-trace \
  --output-dir /path/to/output
```

That's the full happy path. Expect **~4–5 minutes** of wall time for a
~60 MB trace (Phase 1 ~6 s, Phase 2 ~4 min — the screenshot phase is
dominated by Perfetto UI rendering and 5 frames × 2 shots each —
Phase 3 <1 s).

### Useful variants

```bash
# Skip screenshots (fast: Phase 1 + report only, ~10 s)
.venv/bin/python3 scripts/run_workflow.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir /tmp/out \
  --no-screenshots

# Skip the HTML report (raw JSON + screenshots only)
.venv/bin/python3 scripts/run_workflow.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir /tmp/out \
  --no-report

# If you already have Playwright's Chromium downloaded:
PLAYWRIGHT_BROWSERS_PATH=/path/to/ms-playwright \
  .venv/bin/python3 scripts/run_workflow.py \
    --trace /path/to/trace.perfetto-trace \
    --output-dir /tmp/out
```

## 3. What lands in `--output-dir`

```
<output-dir>/
├── render_report.html              # 4–5 MB self-contained HTML (open in a browser)
├── app_jank.json                   # Per-type top jank frames with evidence slices
├── jank_types.json                 # Distribution of jank types across the trace
├── sf_jank.json                    # SurfaceFlinger CPU misses (if any)
├── target_process.json             # Auto-detected target app process
├── thread_map.json                 # Pin patterns + tid map
├── tp_state.json                   # trace_processor session state
└── screenshots/
    ├── 00_<jank_type>_global.png   # Full-timeline overview for issue 0 (~400 KB)
    ├── 00_<jank_type>_detail.png   # Tight zoom around the problem frame (~300 KB)
    ├── 01_..._global.png
    ├── 01_..._detail.png
    ├── ...                         # 5 issues × 2 shots each
    └── screenshot_manifest.json    # Which frame → which file, metadata
```

The HTML report is **self-contained** — screenshots are base64-embedded,
so you can share `render_report.html` alone and the recipient doesn't
need the `screenshots/` folder.

## 4. Open the report

Any modern browser works:

```bash
# Linux
xdg-open /path/to/output/render_report.html

# macOS
open /path/to/output/render_report.html
```

The report shows:
- **Overview**: total frames, jank frames, jank rate, type distribution
- **Top 5 重点问题分析** (Top 5 detailed issues), each with:
  - Top problem frames table
  - Problem frame metadata (jank type, frame number, target_ts,
    focus track, hit keywords, problem description, screenshot reasoning)
  - Evidence slices (Top 5)
  - Global + annotated detail screenshots
  - Android Framework 根因分析 (framework root-cause analysis):
    call chain, source refs, Perfetto trace guide, likely root causes,
    optimization suggestions

## 5. Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| `ModuleNotFoundError: No module named 'perfetto'` | You're not using the venv's python. Use `.venv/bin/python3`, not bare `python3`. |
| Phase 2 hangs / Chromium never connects | `playwright install chromium` didn't run. Re-run it inside the venv. |
| Phase 2 says "trace_processor not found" and falls back to in-browser upload | That's fine, it only costs a few extra seconds. To remove the warning, put a `trace_processor` binary on `$PATH` — download from <https://perfetto.dev/docs/quickstart/trace-analysis>. |
| Screenshots are landscape / small text | Viewport is hard-coded to `1072×1598 @ device_scale_factor=2.0` (portrait long-shot). Don't override these unless you know what you're doing — the pin/collapse logic assumes this height. |
| Report is empty / Phase 1 says 0 jank frames | Your trace is missing `android.surfaceflinger.frametimeline` data. Re-record with frame timeline enabled. |
| `requirements.txt` pip install fails | Make sure Python is ≥ 3.10. Older Python can't resolve `playwright>=1.57.0`. |

## 6. Known issues

- **`04_*_detail.png` (SurfaceFlinger GPU Deadline Missed) may omit the
  App main thread rows.** Root cause and fix options are documented in
  `docs/known_issue_detail_auto_scroll.md`. The auto-annotated global
  shot and the JSON evidence (in `app_jank.json` + the report's
  "Evidence slices" table) still cover the diagnosis.
- **Default target process detection targets the app with the most jank
  frames.** If your trace has multiple heavy apps and picks the wrong
  one, edit the process selection block in `scripts/analyze_jank.py`.

## 7. End-to-end example (copy-paste)

```bash
# Starting from an empty directory
git clone -b feat/portrait-longshot https://github.com/flock957/render-analyze.git
cd render-analyze
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# Replace this with your actual trace path
TRACE=~/Downloads/my_app.perfetto-trace
OUT=/tmp/render-out

.venv/bin/python3 scripts/run_workflow.py --trace "$TRACE" --output-dir "$OUT"

xdg-open "$OUT/render_report.html"   # or `open` on macOS
```

On a typical Linux dev box, expect ~5 minutes from the `run_workflow.py`
invocation to the browser opening, dominated by the screenshot phase.
