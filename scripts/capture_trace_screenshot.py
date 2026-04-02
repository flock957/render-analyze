#!/usr/bin/env python3
"""Capture Perfetto UI screenshots for top jank issues.

V2: Pin relevant tracks (main thread, RenderThread, SurfaceFlinger, Binder)
to the top, zoom to precise fault time range, only capture top N issues.

Usage:
    python3 capture_trace_screenshot.py \
        --trace /path/to/trace.perfetto-trace \
        --analysis-dir /workspace/perf_analysis_output \
        --output-dir /workspace/perf_analysis_output/screenshots \
        --process-name com.ss.android.ugc.aweme \
        --top-n 5

Dependencies (auto-installed if missing):
    - playwright
    - chromium (headless)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
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
    jank_category: str = ""  # e.g. "app_deadline", "display_hal", "sf_stuffing"


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


# ---------------------------------------------------------------------------
# Jank category mapping for track pinning
# ---------------------------------------------------------------------------
JANK_CATEGORY_TRACKS = {
    "app_deadline": {
        "desc": "App Deadline Missed - 应用侧帧超时",
        "tracks": ["Actual Timeline", "Expected Timeline", "{process}", "RenderThread"],
    },
    "buffer_stuffing": {
        "desc": "Buffer Stuffing - BufferQueue 塞满",
        "tracks": ["Actual Timeline", "{process}", "RenderThread", "SurfaceFlinger"],
    },
    "display_hal": {
        "desc": "Display HAL - 显示 HAL 延迟",
        "tracks": ["Actual Timeline", "SurfaceFlinger", "HWC", "hwcomposer"],
    },
    "sf_cpu": {
        "desc": "SF CPU Deadline Missed",
        "tracks": ["Actual Timeline", "SurfaceFlinger", "{process}", "Binder"],
    },
    "sf_gpu": {
        "desc": "SF GPU Deadline Missed",
        "tracks": ["Actual Timeline", "SurfaceFlinger", "RenderThread", "{process}"],
    },
    "prediction_error": {
        "desc": "VSync Prediction Error",
        "tracks": ["Actual Timeline", "Expected Timeline", "SurfaceFlinger"],
    },
    "sf_stuffing": {
        "desc": "SF Stuffing",
        "tracks": ["Actual Timeline", "SurfaceFlinger", "{process}", "RenderThread"],
    },
    "dropped": {
        "desc": "Dropped Frame",
        "tracks": ["Actual Timeline", "Expected Timeline", "{process}", "RenderThread"],
    },
}


def check_memory_available(min_mb: int = 500) -> bool:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                    return avail_kb // 1024 >= min_mb
    except Exception:
        pass
    return True


def ensure_playwright() -> bool:
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


# ---------------------------------------------------------------------------
# Auto-detect app process name from analysis files
# ---------------------------------------------------------------------------
def detect_process_name(analysis_dir: Path) -> str | None:
    """Try to auto-detect the app package name from analysis JSON files."""
    for fname in ["app_jank.json", "jank_types.json"]:
        fpath = analysis_dir / fname
        if not fpath.exists():
            continue
        try:
            text = fpath.read_text()
            # Look for com.xxx.xxx patterns
            matches = re.findall(r'com\.\S+?\.\S+?(?=[\s/\]\"])', text)
            if matches:
                # Return the most common one
                from collections import Counter
                pkg = Counter(matches).most_common(1)[0][0]
                # Clean trailing punctuation
                pkg = pkg.rstrip('.,;:')
                print(f"[screenshot] Auto-detected process: {pkg}")
                return pkg
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Extract & select top N issues
# ---------------------------------------------------------------------------
def extract_issues_from_analysis(analysis_dir: Path) -> list[IssueRegion]:
    """Read analysis JSON files and extract time ranges with issues."""
    issues: list[IssueRegion] = []

    file_mappings = {
        "app_jank.json": ("应用层Jank", "Application layer Jank", "app_deadline"),
        "sf_jank.json": ("SF层Jank", "SurfaceFlinger layer Jank", "sf_cpu"),
        "jank_types.json": ("Jank类型分布", "Jank type distribution", ""),
        # general performance analysis files
        "thread_state.json": ("主线程状态", "Main thread state", ""),
        "rendering.json": ("渲染分析", "Rendering/frame analysis", ""),
    }

    for filename, (cn_name, en_desc, default_cat) in file_mappings.items():
        filepath = analysis_dir / filename
        if not filepath.exists():
            continue
        try:
            data = json.loads(filepath.read_text())
            has_issue = data.get("has_issue", False)
            severity = data.get("severity", "normal")
            if not has_issue or severity == "normal":
                continue

            issue_regions = data.get("issue_regions", [])
            if issue_regions:
                for region in issue_regions:
                    ts = int(region.get("ts", 0))
                    dur = int(region.get("dur", 0))
                    if ts > 0 and dur > 0:
                        # Determine jank category from region name
                        rname = region.get("name", "")
                        cat = _classify_jank_category(rname, default_cat)
                        issues.append(IssueRegion(
                            name=region.get("name", cn_name),
                            description=region.get("desc", en_desc),
                            start_ns=ts,
                            end_ns=ts + dur,
                            severity=region.get("severity", severity),
                            source_file=filename,
                            jank_category=cat,
                        ))
        except Exception as e:
            print(f"[screenshot] Warning: failed to parse {filename}: {e}")

    return issues


def _classify_jank_category(name: str, default: str) -> str:
    """Classify issue into a jank category for track pinning."""
    name_lower = name.lower()
    if "app jank" in name_lower or "app deadline" in name_lower:
        return "app_deadline"
    if "buffer stuffing" in name_lower:
        return "buffer_stuffing"
    if "display hal" in name_lower:
        return "display_hal"
    if "sf cpu" in name_lower or "surfaceflinger cpu" in name_lower:
        return "sf_cpu"
    if "sf gpu" in name_lower or "surfaceflinger gpu" in name_lower:
        return "sf_gpu"
    if "prediction" in name_lower:
        return "prediction_error"
    if "sf stuffing" in name_lower or "surfaceflinger stuffing" in name_lower:
        return "sf_stuffing"
    if "dropped" in name_lower:
        return "dropped"
    return default or "app_deadline"


def select_top_issues(issues: list[IssueRegion], top_n: int = 5) -> list[IssueRegion]:
    """Select top N issues, deduplicated by jank category.

    Strategy:
    1. Group by jank_category
    2. For each category, keep the worst issue (longest duration)
    3. Sort by severity (high > medium > low), then by duration descending
    4. Return top N
    """
    severity_order = {"high": 0, "medium": 1, "low": 2, "normal": 3, "info": 4}

    # Group by category, keep the worst per category
    best_per_category: dict[str, IssueRegion] = {}
    for issue in issues:
        cat = issue.jank_category or "unknown"
        dur = issue.end_ns - issue.start_ns
        existing = best_per_category.get(cat)
        if existing is None:
            best_per_category[cat] = issue
        else:
            existing_dur = existing.end_ns - existing.start_ns
            existing_sev = severity_order.get(existing.severity, 3)
            new_sev = severity_order.get(issue.severity, 3)
            # Prefer higher severity, then longer duration
            if new_sev < existing_sev or (new_sev == existing_sev and dur > existing_dur):
                best_per_category[cat] = issue

    # Sort by severity then duration
    deduped = list(best_per_category.values())
    deduped.sort(key=lambda i: (
        severity_order.get(i.severity, 3),
        -(i.end_ns - i.start_ns),
    ))

    selected = deduped[:top_n]
    print(f"[screenshot] Selected {len(selected)} top issues from {len(issues)} total "
          f"({len(best_per_category)} categories)")
    for i, issue in enumerate(selected):
        dur_ms = (issue.end_ns - issue.start_ns) / 1e6
        print(f"  #{i+1} [{issue.severity}] {issue.name} ({dur_ms:.1f}ms) cat={issue.jank_category}")

    return selected


# ---------------------------------------------------------------------------
# Perfetto UI interaction: pin tracks & navigate
# ---------------------------------------------------------------------------

# JavaScript to pin tracks in Perfetto UI by searching DOM for track titles
# and clicking their pin buttons. Handles virtual scrolling by iterating.
JS_PIN_TRACKS = """
async (trackPatterns) => {
    const results = {pinned: [], failed: [], debug: {}};

    // Find the scrollable track tree container
    const containerSelectors = [
        '.pf-timeline-page__scrolling-track-tree',
        '.pf-track-tree',
        '[class*="scrolling-track"]',
    ];
    let container = null;
    for (const sel of containerSelectors) {
        const el = document.querySelector(sel);
        if (el && el.scrollHeight > 100) { container = el; break; }
    }
    if (!container) {
        results.debug.error = 'No scrollable track container found';
        // Try to find ANY scrollable container
        const all = document.querySelectorAll('div');
        for (const d of all) {
            if (d.scrollHeight > d.clientHeight + 200 && d.clientHeight > 100) {
                container = d;
                results.debug.fallbackContainer = d.className;
                break;
            }
        }
        if (!container) return results;
    }

    results.debug.containerClass = container.className;
    results.debug.scrollHeight = container.scrollHeight;
    results.debug.clientHeight = container.clientHeight;

    const totalHeight = container.scrollHeight;
    const viewHeight = container.clientHeight;
    const step = Math.max(viewHeight * 0.7, 200);
    const alreadyPinned = new Set();

    // Iterate through virtual scroll to find all matching tracks
    for (let scrollPos = 0; scrollPos < totalHeight + step; scrollPos += step) {
        container.scrollTop = scrollPos;
        await new Promise(r => setTimeout(r, 300));

        // Find track header/shell elements
        const shellSelectors = [
            '.pf-track__shell',
            '.pf-track-shell',
            '[class*="track-shell"]',
            '[class*="TrackShell"]',
            '.track-shell',
        ];
        let shells = [];
        for (const sel of shellSelectors) {
            shells = container.querySelectorAll(sel);
            if (shells.length > 0) break;
        }

        if (!shells.length && scrollPos === 0) {
            results.debug.noShellsFound = true;
            // Try broader search
            shells = container.querySelectorAll('[class*="track"]');
        }

        for (const shell of shells) {
            const text = (shell.textContent || '').trim();
            if (!text || text.length > 200) continue;

            const matchedPattern = trackPatterns.find(p => {
                // Support partial matching
                return text.includes(p) || text.toLowerCase().includes(p.toLowerCase());
            });
            if (!matchedPattern) continue;

            const trackKey = text.substring(0, 80);
            if (alreadyPinned.has(trackKey)) continue;

            // Hover to reveal pin button (some UIs hide it until hover)
            shell.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
            await new Promise(r => setTimeout(r, 150));

            // Try multiple selectors for the pin button
            const pinSelectors = [
                'button[title*="Pin"]',
                'button[title*="pin"]',
                '[class*="pin-btn"]',
                '[class*="pin_btn"]',
                '[class*="pinBtn"]',
                '.pf-track-shell-pin',
                'button[aria-label*="pin"]',
                'button[aria-label*="Pin"]',
                // Material icon based
                'button i.material-icons',
            ];

            let pinBtn = null;
            // Search in the shell element and its parent
            for (const sel of pinSelectors) {
                pinBtn = shell.querySelector(sel);
                if (!pinBtn && shell.parentElement) {
                    pinBtn = shell.parentElement.querySelector(sel);
                }
                if (pinBtn) break;
            }

            // If we found a material icon, check if it's actually a pin icon
            if (pinBtn && pinBtn.tagName === 'I') {
                pinBtn = pinBtn.closest('button');
            }

            if (pinBtn) {
                pinBtn.click();
                alreadyPinned.add(trackKey);
                results.pinned.push({pattern: matchedPattern, text: trackKey});
                await new Promise(r => setTimeout(r, 200));
            } else {
                // Fallback: try right-click context menu
                const rect = shell.getBoundingClientRect();
                shell.dispatchEvent(new MouseEvent('contextmenu', {
                    bubbles: true, cancelable: true,
                    clientX: rect.x + 50, clientY: rect.y + 10
                }));
                await new Promise(r => setTimeout(r, 300));

                // Look for "Pin" option in context menu
                const menuItems = document.querySelectorAll(
                    '[role="menuitem"], .context-menu-item, [class*="menu-item"], [class*="MenuItem"]'
                );
                let pinMenuItem = null;
                for (const item of menuItems) {
                    if ((item.textContent || '').toLowerCase().includes('pin')) {
                        pinMenuItem = item;
                        break;
                    }
                }
                if (pinMenuItem) {
                    pinMenuItem.click();
                    alreadyPinned.add(trackKey);
                    results.pinned.push({pattern: matchedPattern, text: trackKey, method: 'contextmenu'});
                    await new Promise(r => setTimeout(r, 200));
                } else {
                    // Dismiss context menu
                    document.body.click();
                    await new Promise(r => setTimeout(r, 100));
                    if (!results.failed.find(f => f.text === trackKey)) {
                        results.failed.push({pattern: matchedPattern, text: trackKey, reason: 'no pin button or menu'});
                    }
                }
            }

            shell.dispatchEvent(new MouseEvent('mouseleave', {bubbles: true}));
        }

        // Early exit if we've pinned enough tracks
        if (alreadyPinned.size >= trackPatterns.length) break;
    }

    // Scroll back to top to show pinned tracks
    container.scrollTop = 0;
    await new Promise(r => setTimeout(r, 500));

    return results;
}
"""

# JavaScript to expand a process group in the track tree
JS_EXPAND_PROCESS = """
async (processName) => {
    const container = document.querySelector(
        '.pf-timeline-page__scrolling-track-tree'
    ) || document.querySelector('[class*="scrolling-track"]');
    if (!container) return {found: false, reason: 'no container'};

    const totalHeight = container.scrollHeight;
    const viewHeight = container.clientHeight;
    const step = Math.max(viewHeight * 0.7, 200);

    for (let scrollPos = 0; scrollPos < totalHeight + step; scrollPos += step) {
        container.scrollTop = scrollPos;
        await new Promise(r => setTimeout(r, 200));

        const clickables = container.querySelectorAll(
            '.pf-track__shell--clickable, [class*="clickable"]'
        );
        for (const el of clickables) {
            const text = (el.textContent || '').trim();
            if (text.includes(processName)) {
                el.click();
                await new Promise(r => setTimeout(r, 500));

                // Scroll so the expanded group is visible
                const rect = el.getBoundingClientRect();
                const containerRect = container.getBoundingClientRect();
                container.scrollTop += (rect.top - containerRect.top) - 30;
                await new Promise(r => setTimeout(r, 300));

                return {found: true, text: text.substring(0, 80), scrollTop: container.scrollTop};
            }
        }
    }

    // Try SurfaceFlinger specifically
    container.scrollTop = 0;
    for (let scrollPos = 0; scrollPos < totalHeight + step; scrollPos += step) {
        container.scrollTop = scrollPos;
        await new Promise(r => setTimeout(r, 200));

        const clickables = container.querySelectorAll(
            '.pf-track__shell--clickable, [class*="clickable"]'
        );
        for (const el of clickables) {
            const text = (el.textContent || '').trim();
            if (text.includes('SurfaceFlinger') || text.includes('system_server')) {
                el.click();
                await new Promise(r => setTimeout(r, 500));
                return {found: true, text: text.substring(0, 80), scrollTop: container.scrollTop};
            }
        }
    }

    return {found: false, reason: 'process not found'};
}
"""


def _get_tracks_to_pin(issue: IssueRegion, process_name: str | None) -> list[str]:
    """Get list of track name patterns to pin based on issue category."""
    cat = issue.jank_category or "app_deadline"
    info = JANK_CATEGORY_TRACKS.get(cat, JANK_CATEGORY_TRACKS["app_deadline"])
    tracks = []
    for t in info["tracks"]:
        if t == "{process}" and process_name:
            tracks.append(process_name)
        elif t != "{process}":
            tracks.append(t)
    return tracks


def _get_search_term(issue: IssueRegion) -> str:
    """Get a Perfetto search term to navigate to the relevant track.

    Each jank type has a characteristic slice name. Searching for it
    in the Perfetto omnibox causes the UI to scroll vertically to the
    track that contains the matching slice, solving the track navigation
    problem.
    """
    cat = issue.jank_category or "app_deadline"
    search_map = {
        # App issues → search in app process tracks
        "app_deadline": "Choreographer#doFrame",
        "buffer_stuffing": "dequeueBuffer",
        # SF issues → search in SurfaceFlinger process tracks
        "display_hal": "waiting for presentFence",  # HWC presentFence wait in SF
        "sf_cpu": "onMessageRefresh",               # SF main thread composition
        "sf_gpu": "onMessageRefresh",
        "prediction_error": "waiting for presentFence",
        "sf_stuffing": "onMessageRefresh",               # SF stuffing
        "dropped": "Choreographer#doFrame",
    }
    return search_map.get(cat, "doFrame")


def _search_and_navigate(page, search_term: str):
    """Use Perfetto's omnibox search to find a slice and navigate to its track."""
    try:
        search_box = page.query_selector('input[placeholder*="Search"]')
        if not search_box:
            search_box = page.query_selector('.omnibox input, [class*="omnibox"] input')
        if not search_box:
            page.keyboard.press("/")
            time.sleep(0.5)
            search_box = page.query_selector('input:focus')

        if search_box:
            search_box.click()
            time.sleep(0.3)
            search_box.fill(search_term)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(1)
            # Close search and bottom panel
            page.keyboard.press("Escape")
            time.sleep(0.3)
            page.keyboard.press("Escape")
            time.sleep(0.3)
            print(f"[screenshot]   Search navigation done")
        else:
            print("[screenshot]   Could not find search box")
    except Exception as e:
        print(f"[screenshot]   Search failed: {e}")


def _close_bottom_panel(page):
    """Close the Perfetto bottom details/selection panel to maximize track area."""
    try:
        # Press Escape to deselect any selected slice
        page.keyboard.press("Escape")
        time.sleep(0.2)
        page.keyboard.press("Escape")
        time.sleep(0.2)

        # Click in timeline area to deselect
        page.mouse.click(960, 200)
        time.sleep(0.3)

        # Force-hide bottom panel via CSS injection (most aggressive approach)
        page.evaluate("""
            (() => {
                // Find the bottom panel by scanning all elements in the lower half
                const allDivs = document.querySelectorAll('div');
                for (const d of allDivs) {
                    const rect = d.getBoundingClientRect();
                    const text = d.textContent || '';
                    // The bottom panel typically contains "Current Selection" text
                    // and is positioned in the lower portion of the screen
                    if (rect.top > 500 && rect.height > 100 && rect.height < 600
                        && text.includes('Current Selection')) {
                        d.style.display = 'none';
                        return 'hidden-current-selection';
                    }
                }

                // Alternative: find by class patterns
                const selectors = [
                    '[class*="bottom-tab"]',
                    '[class*="BottomTab"]',
                    '[class*="details-content"]',
                    '[class*="bottom_tab"]',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.top > 400 && rect.height > 50) {
                            el.style.display = 'none';
                            return 'hidden-' + sel;
                        }
                    }
                }

                // Nuclear option: find the drag handle / splitter between timeline and bottom
                // and drag it all the way down
                const splitters = document.querySelectorAll(
                    '[class*="drag"], [class*="resize"], [class*="splitter"], [class*="Splitter"]'
                );
                for (const s of splitters) {
                    const rect = s.getBoundingClientRect();
                    if (rect.top > 400 && rect.top < 700 && rect.height < 20) {
                        // This looks like a horizontal splitter in the bottom area
                        // Hide everything below it
                        let sibling = s.nextElementSibling;
                        while (sibling) {
                            sibling.style.display = 'none';
                            sibling = sibling.nextElementSibling;
                        }
                        // Also move the splitter down
                        s.style.display = 'none';
                        return 'hidden-splitter';
                    }
                }

                return 'nothing-found';
            })()
        """)
        time.sleep(0.3)
        print("[screenshot]   Bottom panel closed")
    except Exception as e:
        print(f"[screenshot]   Close bottom panel: {e}")


def _expand_process_tracks(page, process_name: str | None):
    """Expand the app process group to show individual thread tracks."""
    if not process_name:
        return
    try:
        result = page.evaluate("""
            async (processName) => {
                // Find the scrollable track container
                const container = document.querySelector(
                    '.pf-timeline-page__scrolling-track-tree'
                ) || document.querySelector('[class*="scrolling-track"]');
                if (!container) return {found: false, reason: 'no container'};

                // Search for collapsed process group headers
                const viewHeight = container.clientHeight;
                const totalHeight = container.scrollHeight;
                const step = Math.max(viewHeight * 0.5, 200);

                const expandedGroups = [];

                for (let pos = 0; pos < totalHeight; pos += step) {
                    container.scrollTop = pos;
                    await new Promise(r => setTimeout(r, 200));

                    // Find all clickable elements (process group headers)
                    const clickables = container.querySelectorAll(
                        '.pf-track__shell--clickable, [class*="clickable"], ' +
                        '.pf-track-shell--clickable'
                    );

                    for (const el of clickables) {
                        const text = (el.textContent || '').trim();
                        if (text.includes(processName) || text.includes('SurfaceFlinger')) {
                            // Check if it has a collapse/expand indicator
                            const chevron = el.querySelector(
                                '[class*="chevron"], [class*="expand"], [class*="collapse"], ' +
                                'i.material-icons, [class*="arrow"]'
                            );
                            el.click();
                            await new Promise(r => setTimeout(r, 500));
                            expandedGroups.push(text.substring(0, 60));
                        }
                    }
                }

                return {found: expandedGroups.length > 0, expanded: expandedGroups};
            }
        """, process_name)
        if result and result.get("found"):
            print(f"[screenshot]   Expanded groups: {result.get('expanded', [])}")
        else:
            print(f"[screenshot]   No groups expanded: {result}")
    except Exception as e:
        print(f"[screenshot]   Expand failed: {e}")


# JavaScript to scroll the track tree to show process tracks instead of CPU tracks
JS_SCROLL_TO_PROCESS = """
async (processName) => {
    // Find the scrollable track tree container
    const selectors = [
        '.pf-timeline-page__scrolling-track-tree',
        '.pf-track-tree',
        '[class*="scrolling-track"]',
    ];
    let container = null;
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.scrollHeight > el.clientHeight + 50) {
            container = el;
            break;
        }
    }
    if (!container) {
        // Fallback: find any scrollable div with significant height
        const divs = document.querySelectorAll('div');
        for (const d of divs) {
            if (d.scrollHeight > d.clientHeight + 300 && d.clientHeight > 200) {
                container = d;
                break;
            }
        }
    }
    if (!container) return {success: false, reason: 'no scrollable container'};

    const totalHeight = container.scrollHeight;
    const viewHeight = container.clientHeight;

    // Strategy 1: Search for THREAD-level tracks (not Timeline tracks)
    // Timeline tracks (Expected/Actual) are very tall and push threads off screen
    const step = Math.max(viewHeight * 0.5, 200);
    const jankCat = processName.split('|')[1] || 'app_deadline';
    const procName = processName.split('|')[0];
    const sfCategories = ['display_hal', 'sf_cpu', 'sf_gpu', 'sf_stuffing', 'prediction_error'];
    // Thread names to search - NO "Timeline" entries here
    const threadTerms = sfCategories.includes(jankCat)
        ? ['waiting for presentFence', 'onMessageRefresh', 'SurfaceFlinger', 'VSYNC', 'HWC', 'Binder']
        : procName
            ? ['RenderThread', 'Choreographer', 'GPU completion', procName, 'Binder']
            : ['SurfaceFlinger', 'RenderThread', 'Binder'];
    // Words to EXCLUDE from matches (avoid FrameTimeline bars and group headers)
    const excludeTerms = ['Timeline', 'Expected', 'Actual', 'Default Workspace'];

    for (let pos = 0; pos < totalHeight; pos += step) {
        container.scrollTop = pos;
        await new Promise(r => setTimeout(r, 150));

        const elements = container.querySelectorAll('*');
        for (const el of elements) {
            if (el.children.length > 5) continue;
            const text = el.textContent?.trim() || '';
            if (text.length > 200 || text.length < 3) continue;

            // Skip if text contains excluded terms (FrameTimeline bars)
            const isExcluded = excludeTerms.some(ex => text.includes(ex));
            if (isExcluded) continue;

            for (const term of threadTerms) {
                if (text.includes(term)) {
                    const elRect = el.getBoundingClientRect();
                    const containerRect = container.getBoundingClientRect();
                    const offset = elRect.top - containerRect.top;
                    // Position found track ~20% from top for context
                    container.scrollTop = pos + offset - viewHeight * 0.2;
                    await new Promise(r => setTimeout(r, 200));
                    return {
                        success: true,
                        method: 'thread-search',
                        text: text.substring(0, 60),
                        scrollTop: container.scrollTop,
                    };
                }
            }
        }
    }

    // Strategy 2: Look for Actual Timeline and scroll past it
    for (let pos = 0; pos < totalHeight; pos += step) {
        container.scrollTop = pos;
        await new Promise(r => setTimeout(r, 150));

        const elements = container.querySelectorAll('*');
        for (const el of elements) {
            if (el.children.length > 5) continue;
            const text = el.textContent?.trim() || '';
            if (text.includes('Actual Timeline') && text.length < 100) {
                const elRect = el.getBoundingClientRect();
                const containerRect = container.getBoundingClientRect();
                const offset = elRect.top - containerRect.top;
                // Position Actual Timeline near the top
                container.scrollTop = pos + offset - 30;
                await new Promise(r => setTimeout(r, 200));
                return {
                    success: true,
                    method: 'timeline-search',
                    text: text.substring(0, 60),
                    scrollTop: container.scrollTop,
                };
            }
        }
    }

    // Strategy 3: Percentage scroll past CPU tracks
    const targetScroll = Math.round(totalHeight * 0.45);
    container.scrollTop = targetScroll;
    await new Promise(r => setTimeout(r, 200));
    return {
        success: true,
        method: 'percentage-scroll',
        scrollTop: targetScroll,
        totalHeight: totalHeight,
    };
}
"""


def _scroll_to_process_area(page, process_name: str | None, jank_category: str = "app_deadline"):
    """Scroll the Perfetto track tree to show relevant tracks based on jank type.

    For SF-related issues: scroll to SurfaceFlinger/VSYNC/HWC tracks.
    For app issues: scroll to app process RenderThread/main tracks.
    """
    # Pass process_name|jank_category as combined string to JS
    combined = f"{process_name or ''}|{jank_category}"
    try:
        result = page.evaluate(JS_SCROLL_TO_PROCESS, combined)
        if result:
            method = result.get("method", "?")
            text = result.get("text", "")[:50]
            print(f"[screenshot]   Scrolled to: {text} ({method})")
        else:
            print("[screenshot]   Scroll returned None")
    except Exception as e:
        print(f"[screenshot]   Scroll failed: {e}")


def capture_screenshots(
    trace_path: str,
    issues: list[IssueRegion],
    output_dir: Path,
    perfetto_url: str = "https://ui.perfetto.dev",
    process_name: str | None = None,
    top_n: int = 5,
) -> list[ScreenshotResult]:
    """Open Perfetto UI, load trace, pin tracks, and capture screenshots."""
    from playwright.sync_api import sync_playwright

    results: list[ScreenshotResult] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    # Select top N issues
    selected = select_top_issues(issues, top_n)
    if not selected:
        print("[screenshot] No issues to capture after filtering")
        return results

    with sync_playwright() as p:
        # Try system Chrome first, then default chromium
        try:
            browser = p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        # Load Perfetto UI
        print(f"[screenshot] Opening {perfetto_url}...")
        page.goto(perfetto_url)
        page.wait_for_load_state("networkidle")
        time.sleep(3)

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

        # Wait for trace to fully load and render
        print("[screenshot] Waiting for trace to render...")
        time.sleep(15)

        # Dismiss cookie/notification banners
        try:
            page.click("text=OK", timeout=3000)
        except Exception:
            pass
        time.sleep(1)

        # Get trace time bounds
        trace_start_ns = 0
        trace_end_ns = 0
        try:
            bounds = page.evaluate("""
                (() => {
                    const tl = app.trace && app.trace.timeline;
                    if (!tl || !tl.visibleWindow) return null;
                    const vw = tl.visibleWindow;
                    return {
                        start: Number(typeof vw.start === 'object' ? (vw.start.integral || 0) : vw.start),
                        duration: Number(vw.duration),
                    };
                })()
            """)
            if bounds:
                trace_start_ns = bounds["start"]
                trace_end_ns = trace_start_ns + bounds["duration"]
                print(f"[screenshot] Trace bounds: {trace_start_ns} - {trace_end_ns} "
                      f"({bounds['duration'] / 1e9:.1f}s)")
        except Exception:
            pass

        # Capture each top issue
        for i, issue in enumerate(selected):
            screenshot_name = f"{i:02d}_{issue.name}.png"
            screenshot_path = output_dir / screenshot_name
            dur_ms = (issue.end_ns - issue.start_ns) / 1e6

            print(f"\n[screenshot] [{i+1}/{len(selected)}] {issue.name} "
                  f"({issue.severity}, {dur_ms:.1f}ms, cat={issue.jank_category})")

            try:
                # Step 1: Navigate to the precise time range
                ts = issue.start_ns
                dur = issue.end_ns - issue.start_ns
                # Show 3x the issue duration for context, minimum 5ms
                pad = max(dur, 5_000_000)

                print(f"[screenshot]   Navigating to time range: "
                      f"{ts/1e9:.6f}s - {(ts+dur)/1e9:.6f}s (pad={pad/1e6:.1f}ms)")

                page.evaluate(f"""
                    (() => {{
                        if (app && app.trace && app.trace.scrollTo) {{
                            app.trace.scrollTo({{
                                time: {{
                                    start: {ts - pad}n,
                                    end: {ts + dur + pad}n,
                                    behavior: 'focus'
                                }}
                            }});
                        }}
                    }})()
                """)
                time.sleep(2)

                # Step 2: Close bottom details panel to maximize track area
                _close_bottom_panel(page)

                # Step 3: Expand app process group to show thread tracks
                if i == 0:  # Only need to expand once
                    _expand_process_tracks(page, process_name)
                    time.sleep(1)

                # Step 4: Use Perfetto search to navigate to relevant slice
                search_term = _get_search_term(issue)
                if search_term:
                    print(f"[screenshot]   Searching for: '{search_term}'")
                    _search_and_navigate(page, search_term)
                    time.sleep(0.5)
                    # Close bottom panel again (search may reopen it)
                    _close_bottom_panel(page)

                # Step 5: Scroll to show relevant tracks based on jank type
                jank_cat = issue.jank_category or "app_deadline"
                _scroll_to_process_area(page, process_name, jank_cat)
                time.sleep(0.5)

                # Step 6: Moderate zoom for detail
                page.mouse.move(960, 400)
                time.sleep(0.2)
                for _ in range(10):
                    page.mouse.wheel(0, -100)
                    time.sleep(0.03)
                time.sleep(1)

                # Step 7: Re-scroll after zoom (zoom changes vertical position)
                _scroll_to_process_area(page, process_name, jank_cat)
                time.sleep(0.5)

                # Step 8: Take screenshot - clip bottom to exclude "Nothing selected" panel
                page.screenshot(
                    path=str(screenshot_path),
                    clip={"x": 0, "y": 0, "width": 1920, "height": 750},
                )

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
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "screenshot_manifest.json"
    manifest_path.write_text(json.dumps(asdict(manifest), indent=2, ensure_ascii=False))
    print(f"[screenshot] Manifest saved: {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description="Capture Perfetto trace screenshots (V2)")
    parser.add_argument("--trace", required=True, help="Path to .perfetto-trace file")
    parser.add_argument("--analysis-dir", required=True, help="Directory with analysis JSON outputs")
    parser.add_argument("--output-dir", default=None, help="Output directory for screenshots")
    parser.add_argument("--perfetto-url", default="https://ui.perfetto.dev", help="Perfetto UI URL")
    parser.add_argument("--process-name", default=None, help="Target process name (auto-detected if omitted)")
    parser.add_argument("--top-n", type=int, default=5, help="Number of top issues to capture (default: 5)")
    parser.add_argument("--min-memory-mb", type=int, default=500)
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

    # Auto-detect process name if not provided
    process_name = args.process_name or detect_process_name(analysis_dir)

    # Extract all issues
    issues = extract_issues_from_analysis(analysis_dir)
    if not issues:
        print("[screenshot] No issues found in analysis results")
        manifest = CaptureManifest(
            trace_file=str(trace_path), total_issues=0, captured=0, skipped=0,
            skipped_reason="No issues found",
        )
        write_manifest(manifest, output_dir)
        print(json.dumps(asdict(manifest), ensure_ascii=False))
        return

    print(f"[screenshot] Found {len(issues)} total issue regions, will capture top {args.top_n}")

    # Check prerequisites
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

    # Capture screenshots
    screenshot_results = capture_screenshots(
        trace_path=str(trace_path),
        issues=issues,
        output_dir=output_dir,
        perfetto_url=args.perfetto_url,
        process_name=process_name,
        top_n=args.top_n,
    )

    captured = sum(1 for r in screenshot_results if r.success)
    skipped = sum(1 for r in screenshot_results if not r.success)

    manifest = CaptureManifest(
        trace_file=str(trace_path),
        total_issues=len(issues),
        captured=captured,
        skipped=skipped,
        screenshots=screenshot_results,
    )
    write_manifest(manifest, output_dir)

    print(f"\n[screenshot] Done: {captured} captured, {skipped} skipped")
    print(json.dumps(asdict(manifest), ensure_ascii=False))


if __name__ == "__main__":
    main()
