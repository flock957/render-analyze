#!/usr/bin/env python3
"""Capture Perfetto UI screenshots for problematic trace segments.

Loads a trace file into Perfetto Web UI via headless Chromium,
navigates to each issue time range, and saves screenshots.

Usage:
    python3 capture_trace_screenshot.py \
        --trace /path/to/trace.perfetto-trace \
        --analysis-dir /workspace/perf_analysis_output \
        --output-dir /workspace/perf_analysis_output/screenshots \
        --port 9001

Dependencies (auto-installed if missing):
    - playwright
    - chromium (headless)

This script is designed as a reusable tool. If dependencies are
unavailable or memory is insufficient, it exits gracefully with
a manifest indicating screenshots were skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict


@dataclass
class IssueRegion:
    """A problematic region in the trace to screenshot."""
    name: str
    description: str
    start_ns: int
    end_ns: int
    severity: str = "medium"
    source_file: str = ""


@dataclass
class ScreenshotResult:
    """Result of a screenshot capture attempt."""
    name: str
    file: str | None
    success: bool
    error: str | None = None


@dataclass
class CaptureManifest:
    """Manifest of all screenshot capture results."""
    trace_file: str
    total_issues: int
    captured: int
    skipped: int
    screenshots: list[ScreenshotResult] = field(default_factory=list)
    skipped_reason: str | None = None


def check_memory_available(min_mb: int = 500) -> bool:
    """Check if enough memory is available to run headless browser."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                    return avail_kb // 1024 >= min_mb
    except Exception:
        pass
    return True  # Assume OK if can't check


def ensure_playwright() -> bool:
    """Ensure playwright and chromium are installed. Returns True if ready."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        pass

    print("[screenshot] Installing playwright...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright", "-q"],
            check=True, capture_output=True, timeout=120,
        )
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True, timeout=300,
        )
        return True
    except Exception as e:
        print(f"[screenshot] Failed to install playwright: {e}")
        return False


def extract_issues_from_analysis(analysis_dir: Path) -> list[IssueRegion]:
    """Read analysis JSON files and extract time ranges with issues."""
    issues: list[IssueRegion] = []

    # Map of analysis files to human-readable names
    file_mappings = {
        "thread_state.json": ("主线程状态", "Main thread state distribution"),
        "big_core_ratio.json": ("大核占比", "Big core running ratio"),
        "cpu_frequency.json": ("CPU频率", "CPU frequency analysis"),
        "compile_level.json": ("编译优化", "Compilation optimization level"),
        "jit_thread.json": ("JIT线程", "JIT thread analysis"),
        "thread_priority.json": ("线程优先级", "Thread priority analysis"),
        "system_load.json": ("系统负载", "System load analysis"),
        "detailed_load.json": ("详细负载", "Detailed load breakdown"),
        "io_details.json": ("IO分析", "IO blocking analysis"),
        "non_io.json": ("非IO阻塞", "Non-IO blocking analysis"),
        "memory.json": ("内存分析", "Memory analysis"),
        "rendering.json": ("渲染分析", "Rendering/frame analysis"),
    }

    # First, get the global time range from launch_range.json
    launch_range_file = analysis_dir / "launch_range.json"
    global_start = 0
    global_end = 0
    if launch_range_file.exists():
        try:
            data = json.loads(launch_range_file.read_text())
            global_start = int(data.get("start_time", 0))
            global_end = int(data.get("end_time", 0))
        except Exception:
            pass

    for filename, (cn_name, en_desc) in file_mappings.items():
        filepath = analysis_dir / filename
        if not filepath.exists():
            continue

        try:
            data = json.loads(filepath.read_text())

            has_issue = data.get("has_issue", False)
            severity = data.get("severity", "normal")

            if not has_issue or severity == "normal":
                continue

            # Use the global time range as the screenshot range
            # (individual scripts may not output their own time ranges)
            start = int(data.get("start_time", global_start))
            end = int(data.get("end_time", global_end))

            if start == 0 and end == 0:
                start = global_start
                end = global_end

            if start > 0 and end > start:
                issues.append(IssueRegion(
                    name=cn_name,
                    description=en_desc,
                    start_ns=start,
                    end_ns=end,
                    severity=severity,
                    source_file=filename,
                ))
        except Exception as e:
            print(f"[screenshot] Warning: failed to parse {filename}: {e}")

    # Always add an overview screenshot of the full trace range
    if global_start > 0 and global_end > global_start:
        issues.insert(0, IssueRegion(
            name="全局概览",
            description="Full trace overview",
            start_ns=global_start,
            end_ns=global_end,
            severity="info",
            source_file="launch_range.json",
        ))

    return issues


def capture_screenshots(
    trace_path: str,
    issues: list[IssueRegion],
    output_dir: Path,
    perfetto_url: str = "https://ui.perfetto.dev",
    timeout_per_screenshot: int = 15,
) -> list[ScreenshotResult]:
    """Open Perfetto UI, load trace, and capture screenshots for each issue."""
    from playwright.sync_api import sync_playwright

    results: list[ScreenshotResult] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        # Load Perfetto UI
        print(f"[screenshot] Opening {perfetto_url}...")
        page.goto(perfetto_url)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        # Load trace file
        print(f"[screenshot] Loading trace: {trace_path}")
        try:
            with page.expect_file_chooser() as fc_info:
                page.click("text=Open trace file")
            fc_info.value.set_files(trace_path)
        except Exception as e:
            print(f"[screenshot] Failed to load trace: {e}")
            browser.close()
            return [ScreenshotResult(name="load_trace", file=None, success=False, error=str(e))]

        # Wait for trace to fully load
        print("[screenshot] Waiting for trace to render...")
        time.sleep(12)

        # Dismiss any cookie/notification banners
        try:
            page.click("text=OK", timeout=2000)
        except Exception:
            pass

        # Close sidebar for cleaner screenshots
        try:
            page.click('button[aria-label="Toggle sidebar"]', timeout=2000)
            time.sleep(0.5)
        except Exception:
            # Try clicking the hamburger menu to toggle sidebar
            try:
                page.locator(".sidebar").evaluate("el => el.style.display = 'none'")
            except Exception:
                pass

        # Capture each issue region
        for i, issue in enumerate(issues):
            screenshot_name = f"{i:02d}_{issue.name}.png"
            screenshot_path = output_dir / screenshot_name

            try:
                print(f"[screenshot] [{i+1}/{len(issues)}] {issue.name} "
                      f"({issue.severity}) ts={issue.start_ns}-{issue.end_ns}")

                # Navigate to the time range using Perfetto's internal API
                duration_ns = issue.end_ns - issue.start_ns
                # Add 10% padding on each side for context
                padding = int(duration_ns * 0.1)
                view_start = max(0, issue.start_ns - padding)
                view_end = issue.end_ns + padding

                # Use Perfetto's postMessage API to scroll to the time range
                page.evaluate(f"""
                    window.postMessage({{
                        perfetto: {{
                            scrollTo: {{
                                time: {{start: {view_start}, end: {view_end}}}
                            }}
                        }}
                    }}, '*');
                """)
                time.sleep(3)

                # If postMessage doesn't work, fall back to keyboard navigation
                # (we'll check if view changed by comparing screenshots)

                page.screenshot(path=str(screenshot_path))
                results.append(ScreenshotResult(
                    name=issue.name,
                    file=screenshot_name,
                    success=True,
                ))
                print(f"[screenshot]   -> saved {screenshot_name}")

            except Exception as e:
                print(f"[screenshot]   -> FAILED: {e}")
                results.append(ScreenshotResult(
                    name=issue.name,
                    file=None,
                    success=False,
                    error=str(e),
                ))

        browser.close()

    return results


def write_manifest(manifest: CaptureManifest, output_dir: Path):
    """Write the manifest JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "screenshot_manifest.json"
    manifest_path.write_text(json.dumps(asdict(manifest), indent=2, ensure_ascii=False))
    print(f"[screenshot] Manifest saved: {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description="Capture Perfetto trace screenshots")
    parser.add_argument("--trace", required=True, help="Path to .perfetto-trace file")
    parser.add_argument("--analysis-dir", required=True, help="Directory with analysis JSON outputs")
    parser.add_argument("--output-dir", default=None, help="Output directory for screenshots (default: analysis-dir/screenshots)")
    parser.add_argument("--perfetto-url", default="https://ui.perfetto.dev", help="Perfetto UI URL")
    parser.add_argument("--min-memory-mb", type=int, default=500, help="Minimum available memory in MB to proceed")
    parser.add_argument("--force", action="store_true", help="Skip memory check")
    args = parser.parse_args()

    trace_path = Path(args.trace)
    analysis_dir = Path(args.analysis_dir)
    output_dir = Path(args.output_dir) if args.output_dir else analysis_dir / "screenshots"

    if not trace_path.exists():
        print(f"[screenshot] ERROR: Trace file not found: {trace_path}")
        sys.exit(1)

    if not analysis_dir.exists():
        print(f"[screenshot] ERROR: Analysis directory not found: {analysis_dir}")
        sys.exit(1)

    # Step 1: Extract issues from analysis results
    issues = extract_issues_from_analysis(analysis_dir)
    if not issues:
        print("[screenshot] No issues found in analysis results, skipping screenshots")
        manifest = CaptureManifest(
            trace_file=str(trace_path), total_issues=0, captured=0, skipped=0,
            skipped_reason="No issues found",
        )
        write_manifest(manifest, output_dir)
        # Output manifest to stdout as well
        print(json.dumps(asdict(manifest), ensure_ascii=False))
        return

    print(f"[screenshot] Found {len(issues)} regions to capture")

    # Step 2: Check prerequisites
    if not args.force and not check_memory_available(args.min_memory_mb):
        reason = f"Insufficient memory (need {args.min_memory_mb}MB free)"
        print(f"[screenshot] SKIP: {reason}")
        manifest = CaptureManifest(
            trace_file=str(trace_path), total_issues=len(issues),
            captured=0, skipped=len(issues), skipped_reason=reason,
        )
        write_manifest(manifest, output_dir)
        print(json.dumps(asdict(manifest), ensure_ascii=False))
        return

    if not ensure_playwright():
        reason = "Failed to install playwright/chromium"
        print(f"[screenshot] SKIP: {reason}")
        manifest = CaptureManifest(
            trace_file=str(trace_path), total_issues=len(issues),
            captured=0, skipped=len(issues), skipped_reason=reason,
        )
        write_manifest(manifest, output_dir)
        print(json.dumps(asdict(manifest), ensure_ascii=False))
        return

    # Step 3: Capture screenshots
    results = capture_screenshots(
        trace_path=str(trace_path),
        issues=issues,
        output_dir=output_dir,
        perfetto_url=args.perfetto_url,
    )

    captured = sum(1 for r in results if r.success)
    skipped = sum(1 for r in results if not r.success)

    manifest = CaptureManifest(
        trace_file=str(trace_path),
        total_issues=len(issues),
        captured=captured,
        skipped=skipped,
        screenshots=results,
    )
    write_manifest(manifest, output_dir)

    print(f"\n[screenshot] Done: {captured} captured, {skipped} skipped")
    print(json.dumps(asdict(manifest), ensure_ascii=False))


if __name__ == "__main__":
    main()
