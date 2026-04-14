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

## Two ways to install

**Online (has internet access, Linux/macOS)** — use `scripts/setup.sh`.
See Step 1 below.

**Offline / air-gapped / Windows** — download a pre-built offline
bundle from GitHub Releases and run `scripts/setup_offline.py`. See
the [Offline / Windows install](#offline--windows-install) section
further down.

## 1. Clone and set up (online Linux/macOS)

One command does everything — creates `.venv`, installs Python deps,
downloads Chromium (with CN mirror fallback), runs a smoke test:

```bash
git clone -b feat/portrait-longshot https://github.com/flock957/render-analyze.git
cd render-analyze
./scripts/setup.sh
```

Total setup time on a fresh machine: ~1 min for the pip install plus
~30 s for the Chromium download (longer if the CN mirror fallback kicks
in). The script is **idempotent** — safe to re-run any time the
environment feels broken.

> **What `setup.sh` does, step by step**
>
> 1. **Python check** — verifies `python3 >= 3.10`, bails out with a
>    clear error if not.
> 2. **venv** — creates `.venv/` (reuses it if it already exists).
> 3. **pip install** — installs everything in `requirements.txt`
>    (`perfetto`, `playwright`, `Pillow`).
> 4. **Chromium download** — tries the official
>    `playwright.download.prss.microsoft.com` host first. If that
>    returns non-zero (common in CN: timeout / 5xx / DNS),
>    automatically retries with
>    `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`
>    and `--force` to clear any partial download.
> 5. **Smoke test** — actually launches headless Chromium once via
>    `sync_playwright()`. If this prints `SMOKE TEST PASSED` you're
>    good. If it crashes with `Executable doesn't exist`, the download
>    didn't land where playwright expects — see Troubleshooting.
>
> If both the official host and the npmmirror fallback fail you're
> most likely on a fully air-gapped or aggressively firewalled
> machine. See the Troubleshooting table below for the manual
> escape hatch.

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
# Linux (local desktop)
xdg-open /path/to/output/render_report.html

# macOS
open /path/to/output/render_report.html
```

**Running on a remote / SSH-only box?** The report is self-contained —
`scp` just the one HTML file to your local machine and open it there:

```bash
# From your local machine
scp user@remote:/path/to/output/render_report.html ./
# Then open render_report.html in your local browser
```

You do **not** need to copy the `screenshots/` directory — all images
are base64-embedded in the HTML.

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
| `playwright install chromium` fails / hangs (CN network) | The Step 1.4 `\|\|` fallback should auto-retry via the Alibaba npmmirror. If you skipped that line, run it manually: `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright .venv/bin/playwright install chromium --force`. |
| Both official and mirror attempts fail (fully air-gapped / hard firewall) | Use the **offline bundle** — see [Offline / Windows install](#offline--windows-install) below. Do **not** try to patch the online install on a fully offline box. |
| `xdg-open` / `open` fails on a remote box | You're on an SSH-only server without a desktop. See Step 4 — `scp` the self-contained `render_report.html` to your local machine and open it there. |

## Offline / Windows install

Use this path when:
- Your machine has **no internet access** (corporate LAN, isolated dev box)
- You're on **Windows** (the online `setup.sh` is bash-only)
- `playwright install chromium` keeps failing even through CN mirrors

### What you need

- A pre-built offline bundle matching your **OS** + **Python minor
  version**, from the repo's [GitHub Releases](https://github.com/flock957/render-analyze/releases) page:

| Filename | For |
|---|---|
| `render-analyze-offline-linux-x64-py310.tar.gz` | Linux x86_64 + Python 3.10 |
| `render-analyze-offline-linux-x64-py311.tar.gz` | Linux x86_64 + Python 3.11 |
| `render-analyze-offline-linux-x64-py312.tar.gz` | Linux x86_64 + Python 3.12 |
| `render-analyze-offline-windows-x64-py310.zip` | Windows x64 + Python 3.10 |
| `render-analyze-offline-windows-x64-py311.zip` | Windows x64 + Python 3.11 |
| `render-analyze-offline-windows-x64-py312.zip` | Windows x64 + Python 3.12 |

Each bundle is ~150 MB and is fully self-contained — no pip, no
`playwright install`, no external downloads during setup.

### Linux / macOS

```bash
# 1. Extract (size: ~150 MB compressed, ~400 MB unpacked)
tar xzf render-analyze-offline-linux-x64-py312.tar.gz
cd render-analyze

# 2. Run the offline installer — pure Python, no network access
python3 scripts/setup_offline.py

# 3. Run the workflow exactly as in the online case
.venv/bin/python3 scripts/run_workflow.py \
  --trace /path/to/your.perfetto-trace \
  --output-dir /path/to/output
```

### Windows

```cmd
REM 1. Extract with any zip tool (Windows Explorer right-click → Extract All)
REM    or from cmd:
tar -xf render-analyze-offline-windows-x64-py312.zip
cd render-analyze

REM 2. Run the offline installer. Either double-click
REM    scripts\setup_offline.bat, or run it manually:
python scripts\setup_offline.py

REM 3. Run the workflow
.venv\Scripts\python.exe scripts\run_workflow.py ^
  --trace path\to\your.perfetto-trace ^
  --output-dir path\to\output
```

### How the offline mode works

The bundle ships with:
- `wheels/` — every Python dependency as a `.whl` file, matched to
  the target OS + Python version. `setup_offline.py` runs
  `pip install --no-index --find-links wheels/ -r requirements.txt`
  so pip never touches the network.
- `vendor/ms-playwright/` — a pre-downloaded Chromium headless shell
  in the exact layout `playwright install chromium` would produce
  locally, plus an `INSTALLATION_COMPLETE` marker file.

At runtime, `scripts/run_workflow.py` checks whether `vendor/ms-playwright/`
exists inside the repo. If it does, it sets
`PLAYWRIGHT_BROWSERS_PATH=<repo>/vendor/ms-playwright` before launching
child processes. The online-mode user never sees or needs to know about
this — `vendor/` just isn't there in a normal `git clone`.

### Building a new bundle (maintainers only)

On a build host with outbound internet:

```bash
# Single combo
python3 scripts/build_offline_bundle.py --target linux-x64 --python 3.12

# All 6 combos (Linux+Windows × 3.10/3.11/3.12)
python3 scripts/build_offline_bundle.py --all
```

Output lands in `./dist/`. Upload to GitHub Releases as attached
assets; users download them from the Releases page.

### Offline troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `ERROR: wheels/ directory not found` | You're running `setup_offline.py` outside an extracted bundle, or you're inside a normal `git clone`. Extract the bundle first. |
| `ERROR: vendor/ms-playwright/ not found` | Bundle is incomplete. Re-download from Releases. |
| `pip install` still goes to the network | Check you're using `setup_offline.py`, not `setup.sh`. `setup_offline.py` passes `--no-index --find-links wheels/` which forbids network access. |
| Smoke test crashes `spawn ... EACCES` | Unix permission bits were not preserved on the Chromium binary. This shouldn't happen with a fresh bundle — re-download from Releases instead of copying the extracted directory between machines. |
| Windows: `'python' is not recognized` | Python isn't on your PATH. Install Python 3.10+ from <https://www.python.org/downloads/> and check "Add Python to PATH" during install. |

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
./scripts/setup.sh

# Replace this with your actual trace path
TRACE=~/Downloads/my_app.perfetto-trace
OUT=/tmp/render-out

.venv/bin/python3 scripts/run_workflow.py --trace "$TRACE" --output-dir "$OUT"

# On a local desktop — or scp render_report.html to your laptop if remote
xdg-open "$OUT/render_report.html"   # or `open` on macOS
```

On a typical Linux dev box, expect ~5 minutes from the `run_workflow.py`
invocation to the browser opening, dominated by the screenshot phase.
