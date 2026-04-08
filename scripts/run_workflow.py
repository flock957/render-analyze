#!/usr/bin/env python3
"""Render Analysis Workflow - Main entry point.

Executes all phases sequentially with clear stage logging.

Phases:
  1. Analyze jank frames via SQL
  2. Capture Perfetto UI screenshots
  3. Generate HTML report

Usage:
  # Full workflow
  python3 run_workflow.py --trace /path/to/trace.perfetto-trace --output-dir /path/to/output

  # Skip screenshots (faster)
  python3 run_workflow.py --trace /path/to/trace --output-dir /out --no-screenshots
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable


def banner(phase, title, detail=""):
    """Print a clear phase banner."""
    line = "=" * 60
    print(f"\n{line}")
    print(f"  Phase {phase}: {title}")
    if detail:
        print(f"  {detail}")
    print(f"{line}\n")


def run_script(name, args, phase):
    """Run a script and stream its output."""
    script = SCRIPT_DIR / name
    if not script.exists():
        print(f"  ERROR: Script not found: {script}")
        return False

    cmd = [PYTHON, str(script)] + args
    print(f"  $ {' '.join(cmd)}\n")

    t0 = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in iter(proc.stdout.readline, ""):
        sys.stdout.write(f"  {line}")
        sys.stdout.flush()
    proc.wait()
    elapsed = time.time() - t0

    if proc.returncode != 0:
        print(f"\n  Phase {phase} FAILED (exit={proc.returncode}, {elapsed:.1f}s)")
        return False

    print(f"\n  Phase {phase} completed in {elapsed:.1f}s")
    return True


def main():
    parser = argparse.ArgumentParser(description="Render Analysis Workflow")
    parser.add_argument("--trace", required=True, help="Path to .perfetto-trace file")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--no-screenshots", action="store_true", help="Skip Phase 2 (screenshots)")
    parser.add_argument("--no-report", action="store_true", help="Skip Phase 3 (report)")
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    trace_path = str(Path(args.trace).resolve())

    if not Path(trace_path).exists():
        print(f"ERROR: Trace not found: {trace_path}")
        sys.exit(1)

    size_mb = Path(trace_path).stat().st_size / 1024 / 1024

    print("╔══════════════════════════════════════════════════════════╗")
    print("║        Render Jank Analysis Workflow                    ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Trace:  {Path(trace_path).name:<48}║")
    print(f"║  Size:   {size_mb:.1f}MB{'':<45}║")
    print(f"║  Output: {str(output):<48}║")
    print(f"║  Screenshots: {'skip' if args.no_screenshots else 'yes':<44}║")
    print("╚══════════════════════════════════════════════════════════╝")

    t_start = time.time()

    # ─── Phase 1: Analyze jank ──────────────────────────────────
    banner(1, "Analyze Jank Frames", f"Trace: {Path(trace_path).name}")
    if not run_script("analyze_jank.py", ["--trace", trace_path, "--output-dir", str(output)], 1):
        sys.exit(1)

    # ─── Phase 2: Capture screenshots ───────────────────────────
    if not args.no_screenshots:
        banner(2, "Capture Perfetto Screenshots")
        screenshot_dir = str(output / "screenshots")
        if not run_script("capture_screenshots.py", [
            "--trace", trace_path,
            "--analysis-dir", str(output),
            "--output-dir", screenshot_dir,
        ], 2):
            print("  WARNING: Screenshots failed, continuing without them.")
    else:
        print("\n  Phase 2: SKIPPED (--no-screenshots)")

    # ─── Phase 3: Generate report ───────────────────────────────
    if not args.no_report:
        banner(3, "Generate HTML Report")
        if not run_script("generate_report.py", [
            "--analysis-dir", str(output),
            "--output", str(output / "render_report.html"),
        ], 3):
            print("  WARNING: Report generation failed.")
    else:
        print("\n  Phase 3: SKIPPED (--no-report)")

    # ─── Summary ────────────────────────────────────────────────
    elapsed = time.time() - t_start
    print("\n" + "=" * 60)
    print(f"  Workflow complete in {elapsed:.0f}s")
    print(f"  Output: {output}/")

    for f in sorted(output.rglob("*")):
        if f.is_file():
            size = f.stat().st_size
            if size > 1024 * 1024:
                size_str = f"{size / 1024 / 1024:.1f}MB"
            elif size > 1024:
                size_str = f"{size / 1024:.0f}KB"
            else:
                size_str = f"{size}B"
            rel = f.relative_to(output)
            print(f"    {rel} ({size_str})")
    print("=" * 60)


if __name__ == "__main__":
    main()
