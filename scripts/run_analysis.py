#!/usr/bin/env python3
"""Android Render Performance Analysis - One-click orchestrator.

Runs all 10 phases automatically. The AI agent only needs to call:
    python3 scripts/run_analysis.py --trace /path/to/trace.perfetto-trace

This script handles all intermediate logic (reading JSON outputs, deciding
which jank types to analyze, passing parameters between phases).

Usage:
    python3 scripts/run_analysis.py --trace /path/to/trace.perfetto-trace
    python3 scripts/run_analysis.py --trace /path/to/trace.perfetto-trace --output-dir /custom/output --top-n 3
    python3 scripts/run_analysis.py --trace /path/to/trace.perfetto-trace --skip-screenshot --skip-setup
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR = "/workspace/render_output"
DEFAULT_PORT = 9001


def _run(cmd: list[str], desc: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command and print status."""
    print(f"\n{'='*60}")
    print(f"[Phase] {desc}")
    print(f"[Cmd]   {' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, capture_output=False, timeout=timeout)
    if check and result.returncode != 0:
        print(f"[FAIL] {desc} (exit code {result.returncode})")
        sys.exit(1)
    return result


def _load_json(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Android Render Performance Analysis - One-click orchestrator"
    )
    parser.add_argument("--trace", required=True, help="Path to .perfetto-trace file")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"trace_processor port (default: {DEFAULT_PORT})")
    parser.add_argument("--top-n", type=int, default=5, help="Top N issues in report (default: 5)")
    parser.add_argument("--skip-setup", action="store_true", help="Skip environment setup")
    parser.add_argument("--skip-screenshot", action="store_true", help="Skip Perfetto screenshot")
    args = parser.parse_args()

    trace_path = Path(args.trace).resolve()
    output_dir = Path(args.output_dir)
    port = args.port
    python = sys.executable

    if not trace_path.exists():
        print(f"[ERROR] Trace file not found: {trace_path}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"""
============================================================
  Android Render Performance Analyzer
============================================================
  Trace:      {trace_path}
  Output:     {output_dir}
  Port:       {port}
  Top N:      {args.top_n}
  Screenshot: {'skip' if args.skip_screenshot else 'yes'}
============================================================
""")

    # Set RENDER_OUTPUT env var so analysis scripts write to the correct dir
    os.environ["RENDER_OUTPUT"] = str(output_dir)

    # ── Phase 0: Environment Setup ──
    if not args.skip_setup:
        _run([python, str(SCRIPT_DIR / "setup_env.py")],
             "Phase 0: Environment Setup", check=False, timeout=600)

    # ── Phase 1: Load Trace ──
    # trace_processor_init.py starts trace_processor as a daemon process
    # (start_new_session=True) so it survives after the script exits.
    _run([python, str(SCRIPT_DIR / "trace_processor_init.py"),
          "--trace", str(trace_path),
          "--port", str(port),
          "--output-dir", str(output_dir)],
         "Phase 1: Load Trace into trace_processor", timeout=120)

    # ── Phase 2: Find Foreground Process ──
    # Note: find_foreground_process uses --output-dir
    _run([python, str(SCRIPT_DIR / "find_foreground_process.py"),
          "--port", str(port),
          "--output-dir", str(output_dir)],
         "Phase 2: Find Foreground Process", check=False)

    process_name = None
    target_json = _load_json(output_dir / "target_process.json")
    if target_json:
        process_name = target_json.get("process_name", "")
        print(f"[INFO] Target process: {process_name}")

    # ── Phase 3: Initialize Jank Metrics ──
    # Note: uses RENDER_OUTPUT env var (set above), not --output-dir
    _run([python, str(SCRIPT_DIR / "init_render_jank_metric.py"),
          "--port", str(port)],
         "Phase 3: Initialize Jank Metrics")

    # ── Phase 4: Analyze Jank Types ──
    # Note: uses RENDER_OUTPUT env var
    _run([python, str(SCRIPT_DIR / "analyze_jank_types.py"),
          "--port", str(port)],
         "Phase 4: Analyze Jank Type Distribution")

    # Read jank types to decide which analyses to run
    jank_types_data = _load_json(output_dir / "jank_types.json")
    detected_types = []
    if jank_types_data:
        detected_types = jank_types_data.get("detected_types", [])
        jank_type_list = jank_types_data.get("jank_types", [])
        type_names = set()
        for jt in jank_type_list:
            for name in jt.get("jank_type", "").split(", "):
                type_names.add(name.strip())
        print(f"[INFO] Detected {len(type_names)} jank types: {', '.join(sorted(type_names))}")

    # ── Phase 5: App Jank Analysis ──
    all_types_str = " ".join(str(t) for t in detected_types)
    has_app_jank = any(k in all_types_str for k in [
        "App Deadline", "Buffer Stuffing", "AppDeadlineMissed", "BufferStuffing"
    ])

    if has_app_jank:
        # Note: uses RENDER_OUTPUT env var
        # Jank type names must match what trace_processor returns (with spaces)
        _run([python, str(SCRIPT_DIR / "analyze_app_jank.py"),
              "--jank-types", "App Deadline Missed,Buffer Stuffing",
              "--port", str(port)],
             "Phase 5: App Layer Jank Analysis")
    else:
        print("\n[SKIP] Phase 5: No app-level jank types detected")

    # ── Phase 6: SF Jank Analysis ──
    sf_types = "SurfaceFlinger CPU Deadline Missed,SurfaceFlinger GPU Deadline Missed,Display HAL,Prediction Error,SurfaceFlinger Scheduling,SurfaceFlinger Stuffing,Dropped Frame"
    has_sf_jank = any(k in all_types_str for k in [
        "SurfaceFlinger", "Display HAL", "DisplayHal", "Prediction",
        "Dropped", "SF", "Unknown"
    ])

    if has_sf_jank:
        # Note: uses RENDER_OUTPUT env var
        _run([python, str(SCRIPT_DIR / "analyze_sf_jank.py"),
              "--jank-types", sf_types,
              "--port", str(port)],
             "Phase 6: SurfaceFlinger Jank Analysis")
    else:
        print("\n[SKIP] Phase 6: No SF-level jank types detected")

    # ── Phase 7: Screenshots (Optional) ──
    if not args.skip_screenshot:
        screenshot_cmd = [
            python, str(SCRIPT_DIR / "capture_trace_screenshot.py"),
            "--trace", str(trace_path),
            "--analysis-dir", str(output_dir),
            "--output-dir", str(output_dir / "screenshots"),
            "--top-n", str(args.top_n),
            "--force",
        ]
        if process_name:
            screenshot_cmd.extend(["--process-name", process_name])
        _run(screenshot_cmd,
             "Phase 7: Perfetto UI Screenshots", check=False, timeout=600)
    else:
        print("\n[SKIP] Phase 7: Screenshots skipped")

    # ── Phase 8: Cleanup ──
    _run([python, str(SCRIPT_DIR / "trace_processor_cleanup.py"),
          "--output-dir", str(output_dir)],
         "Phase 8: Stop trace_processor", check=False)

    # ── Phase 9: Generate Report ──
    _run([python, str(SCRIPT_DIR / "render_report_generator.py"),
          "--output-dir", str(output_dir),
          "--top-n", str(args.top_n)],
         "Phase 9: Generate HTML Report")

    # ── Summary ──
    report_path = output_dir / "render_report.html"
    print(f"""
============================================================
  Analysis Complete!
============================================================
  Report: {report_path}
  Output: {output_dir}
""")

    # Print summary from jank_types
    if jank_types_data:
        total = jank_types_data.get("total_frames", 0)
        jank_count = jank_types_data.get("jank_frame_count", 0)
        jank_rate = jank_types_data.get("jank_rate_pct", 0)
        print(f"  Total frames: {total}")
        print(f"  Jank frames:  {jank_count} ({jank_rate:.1f}%)")

    if process_name:
        print(f"  Process:      {process_name}")

    print(f"============================================================")

    # Output final result as JSON for programmatic use
    result = {
        "status": "complete",
        "report": str(report_path),
        "output_dir": str(output_dir),
        "process_name": process_name,
        "total_frames": jank_types_data.get("total_frames", 0) if jank_types_data else 0,
        "jank_rate_pct": jank_types_data.get("jank_rate_pct", 0) if jank_types_data else 0,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
