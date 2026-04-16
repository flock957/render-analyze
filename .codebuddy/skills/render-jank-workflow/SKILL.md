---
name: render-jank-workflow
description: Run the 3-phase render-analyze pipeline on an Android Perfetto trace to produce an HTML report with annotated screenshots and Framework root-cause analysis. Trigger when the user mentions a .perfetto-trace file AND asks to analyze render performance, jank, frame timeline, SurfaceFlinger jank, DrawFrame delays, or wants a rendering HTML report. Typical phrases — "分析这个 trace 的渲染 jank", "analyze this perfetto trace for jank", "帮我看这个 trace 里为什么卡", "跑一下渲染性能分析", "generate a render report for this trace".
allowed-tools: Bash, Read, Write, Edit
---

# Render Jank Analysis Workflow

This skill wraps the `render-analyze` 3-phase pipeline. All logic lives in
Python scripts under `scripts/` — the skill's job is only to orchestrate
them and report back.

## Pre-flight

Before running anything, check the repo is set up:

1. If `.venv/` is **missing**, stop and tell the user to run setup first:
   - Online: `./scripts/setup.sh`
   - Offline bundle: `python3 scripts/setup_offline.py`
2. If `.venv/` exists, assume the environment is ready (setup is idempotent —
   if the user is unsure, re-running setup is safe).

## Required inputs (ask user if not given)

- **`<trace>`** — path to a `.perfetto-trace` / `.pb` / `.pftrace` file
- **`<output-dir>`** — where to write results (suggest `/tmp/render-out` or
  `./render-output` if the user has no preference)

## Execution — pick one path

### Path A (default): single-command orchestrator

One command runs all 3 phases in order, prints per-phase progress,
~4–5 minutes total for a 60 MB trace:

```bash
.venv/bin/python3 scripts/run_workflow.py \
  --trace <trace> \
  --output-dir <output-dir>
```

Optional flags:
- `--no-screenshots` → skip Phase 2 (fast: Phase 1 + 3 only, ~10 s)
- `--no-report` → raw JSON + screenshots only, skip HTML

### Path B: per-phase (for debugging or partial reruns)

```bash
# Phase 1 — jank analysis (always needed; ~6 s)
.venv/bin/python3 scripts/analyze_jank.py \
  --trace <trace> --output-dir <output-dir>

# Phase 2 — Perfetto UI screenshots (optional; ~4 min)
.venv/bin/python3 scripts/capture_screenshots.py \
  --trace <trace> \
  --analysis-dir <output-dir> \
  --output-dir <output-dir>/screenshots

# Phase 3 — HTML report (~0.1 s)
.venv/bin/python3 scripts/generate_report.py \
  --analysis-dir <output-dir> \
  --output <output-dir>/render_report.html
```

Only use Path B when the user explicitly wants per-phase control, or when
a previous Path A run failed partway and we want to resume from a specific
phase without redoing earlier ones.

## Constraints

- **Do not write new SQL queries or analysis code** — only call the
  provided scripts. The repo's whole point is reproducible analysis with
  no LLM in the critical path.
- **Do not modify the scripts.** If they seem wrong, surface that to the
  user instead of patching silently.
- **Do not guess the trace path.** Ask if not given.
- **Phase 2 is optional.** If screenshots fail (Chromium can't start,
  Perfetto UI times out), record the error, continue to Phase 3 — do
  not abort the whole workflow.

## After the run

1. Verify `<output-dir>/render_report.html` exists.
2. Open it and summarize for the user:
   - Overall jank rate and total frames (from `<output-dir>/jank_types.json`)
   - Top 5 jank types with the worst durations
   - One-line takeaway per top issue (pulled from the report's problem
     descriptions — the scripts already wrote human-readable summaries)
3. Offer follow-ups:
   - Local desktop: `xdg-open <output-dir>/render_report.html` (Linux) or
     `open <output-dir>/render_report.html` (macOS)
   - Remote / SSH: `scp` the self-contained HTML to the user's local
     machine (screenshots are base64-embedded; no need to copy the
     `screenshots/` directory)

## Quick reference

| File | Purpose |
|---|---|
| `scripts/run_workflow.py` | Orchestrator (Path A) |
| `scripts/analyze_jank.py` | Phase 1 — SQL + thread map |
| `scripts/capture_screenshots.py` | Phase 2 — Perfetto UI screenshots |
| `scripts/generate_report.py` | Phase 3 — HTML report |
| `scripts/setup.sh` | One-command online setup |
| `scripts/setup_offline.py` | Offline-bundle installer |
| `skills/workflow.md` | Full technical spec (8-step screenshot strategy, pin rules, FRAMEWORK_KB) — read this when the user asks **why** or **how** the pipeline works |
| `docs/quickstart.md` | New-user guide including Offline / Windows install |
| `docs/known_issue_detail_auto_scroll.md` | Known issue: issue #4 detail shot drops main thread rows (deferred fix) |

## References

When the user asks for technical detail beyond "just run it" — for
example, "why do you pin only 2 tracks", "what is the 8-step screenshot
strategy", "what does the FRAMEWORK_KB cover" — read `skills/workflow.md`
first and cite it. That file is the authoritative spec.
