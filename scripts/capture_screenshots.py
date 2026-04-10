#!/usr/bin/env python3
"""Phase 2: Capture Perfetto UI screenshots for top jank issues.

v4: Uses trace_processor HTTP RPC for fast loading.
- Starts trace_processor --httpd in background (loads trace once)
- Perfetto UI connects via RPC, no file upload, no in-browser parsing
- Portrait-long viewport (1072x1598) + high device scale for crisp detail
- Full rendering pipeline pin: Timeline → App → SF → HWC → CrtcCommit
- Auto-crop screenshots to pinned content area
- DOM-based wait (not fixed sleep) for trace ready state

Two screenshot types per jank:
- Global: full trace window for global pattern recognition
- Detail: narrowed window around target_ts and slice click for evidence focus
"""
import argparse
import json
import os
import subprocess
import time
import sys
from pathlib import Path


# Viewport dimensions — portrait long-shot
VIEWPORT_WIDTH = 1072
VIEWPORT_HEIGHT = 1598
DEVICE_SCALE_FACTOR = 2.0

# trace_processor binary port for HTTP RPC mode.
# The binary path itself is auto-discovered via shutil.which("trace_processor")
# or supplied via the --trace-processor CLI argument. If neither is available
# the workflow falls back to in-browser file upload, which is slightly slower
# but functionally equivalent.
TRACE_PROCESSOR_PORT = 9001


def main():
    parser = argparse.ArgumentParser(description="Capture Perfetto trace screenshots")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--trace-processor",
        default=None,
        help=(
            "Path to a trace_processor binary used for HTTP RPC mode. "
            "Default: auto-discover via $PATH (shutil.which). "
            "If neither is available, falls back to in-browser file upload."
        ),
    )
    args = parser.parse_args()

    trace = Path(args.trace)
    analysis = Path(args.analysis_dir)
    output = Path(args.output_dir) if args.output_dir else analysis / "screenshots"
    output.mkdir(parents=True, exist_ok=True)

    # Load analysis data
    app_jank = _load(analysis / "app_jank.json")
    target = _load(analysis / "target_process.json")
    thread_map = _load(analysis / "thread_map.json")
    tp_state = _load(analysis / "tp_state.json")

    top_frames = app_jank["top_frames"][:5]
    pin_patterns = thread_map["pin_patterns"]

    print(f"[Phase 2] Capturing screenshots for {len(top_frames)} issues")
    print(f"  Target: {target['process_name']} pid={target['pid']}")
    print(f"  Pin patterns ({len(pin_patterns)}): {pin_patterns}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[capture] ERROR: playwright not found. Install: pip install playwright && playwright install chromium")
        sys.exit(1)

    results = []

    # --- Start trace_processor HTTP RPC server (loads trace ONCE) ---
    size_mb = trace.stat().st_size // 1024 // 1024
    print(f"  [2.0] Starting trace_processor HTTP RPC for {size_mb}MB trace...")
    tp_proc = _start_trace_processor(trace, args.trace_processor)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )

            # --- Load Perfetto UI (will auto-detect RPC server) ---
            print("  [2.1] Loading Perfetto UI...")
            page.goto("https://ui.perfetto.dev")
            page.wait_for_load_state("networkidle")
            time.sleep(2)
            _dismiss_cookie(page)

            # --- Load trace (try RPC first, fallback to file upload) ---
            print("  [2.2] Loading trace...")
            t0 = time.time()
            rpc_used = False

            if tp_proc is not None:
                # Wait briefly for Perfetto UI to auto-detect RPC server
                # When detected, it may either:
                # 1. Show "YES, use loaded trace" dialog
                # 2. Auto-load the trace silently
                time.sleep(3)

                # Try to click YES dialog if present
                try:
                    btn = page.locator("button:has-text('YES'), text='YES, use loaded trace'").first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        print(f"         RPC dialog accepted")
                        rpc_used = True
                except: pass

                # Check if trace is already loaded via RPC (no dialog needed)
                if not rpc_used:
                    try:
                        loaded = page.evaluate(
                            "() => !!(window.app && window.app._activeTrace && window.app._activeTrace.timeline)"
                        )
                        if loaded:
                            print(f"         RPC auto-loaded (no dialog)")
                            rpc_used = True
                    except: pass

            if not rpc_used:
                # Fall back to file upload
                print("         Using file upload...")
                try:
                    with page.expect_file_chooser(timeout=5000) as fc:
                        page.click("text=Open trace file")
                    fc.value.set_files(str(trace))
                except Exception as e:
                    print(f"         File upload failed: {e}")
                    raise

            # --- Wait for trace to be fully loaded (DOM-based, not sleep) ---
            print("  [2.2.1] Waiting for trace to be ready...")
            try:
                page.wait_for_function(
                    "() => window.app && window.app._activeTrace && window.app._activeTrace.timeline",
                    timeout=120000,
                )
                print(f"         Trace ready in {time.time()-t0:.1f}s ({'RPC' if rpc_used else 'file upload'})")
            except Exception as e:
                print(f"         Wait timeout: {e}")
            time.sleep(2)  # small grace period for UI render
            _dismiss_cookie(page)

            # --- Prepare UI ---
            print("  [2.3] Preparing UI (sidebar, expand, discover tracks)...")
            # Hide sidebar for maximum trace area
            sidebar_visible = page.evaluate("""
                (() => {
                    const sb = document.querySelector('.pf-sidebar');
                    return sb && sb.offsetWidth > 50;
                })()
            """)
            if sidebar_visible:
                _cmd(page, 'dev.perfetto.ToggleLeftSidebar')
                time.sleep(0.5)

            # Expand all to make tracks discoverable, then collapse
            _cmd(page, 'dev.perfetto.ExpandAllGroups')
            time.sleep(2)
            _cmd(page, 'dev.perfetto.CollapseAllGroups')
            time.sleep(0.5)

            # --- Process each jank frame ---
            for i, frame in enumerate(top_frames):
                jank_type = frame["jank_type"]
                dur_ms = frame["actual_dur_ms"]
                ts = frame["ts"]
                dur = frame["dur"]
                safe_name = jank_type.replace(",", "").replace(" ", "_")[:40]

                print(f"\n  [2.4.{i+1}] [{i+1}/{len(top_frames)}] {jank_type} ({dur_ms:.1f}ms)")

                # Reset: unpin, collapse, close drawer, clear search
                _cmd(page, 'dev.perfetto.UnpinAllTracks')
                _cmd(page, 'dev.perfetto.CollapseAllGroups')
                _close_drawer(page)
                _clear_search(page)
                time.sleep(0.3)

                # Expand target process + SF groups so their children are pinnable
                _cmd(page, 'dev.perfetto.ExpandTracksByRegex', target['process_name'])
                time.sleep(0.3)
                _cmd(page, 'dev.perfetto.ExpandTracksByRegex', 'surfaceflinger')
                time.sleep(0.3)

                # Pin all rendering pipeline tracks (order matters for top-to-bottom layout)
                for pat in pin_patterns:
                    _cmd(page, 'dev.perfetto.PinTracksByRegex', pat)
                    time.sleep(0.2)

                # Dismiss any lingering omnibox/track-path dialog left by pin commands
                page.keyboard.press("Escape")
                time.sleep(0.2)
                page.keyboard.press("Escape")
                time.sleep(0.2)

                # Collapse all non-pinned tracks (pinned stay at top)
                _cmd(page, 'dev.perfetto.CollapseAllGroups')
                time.sleep(0.3)

                target_ts = int(frame.get("target_ts", ts))
                focus_track = frame.get("focus_track", "Actual Timeline")

                # === Global screenshot (full trace window) ===
                global_start = int(tp_state["trace_start"])
                global_end = int(tp_state["trace_end"])
                _zoom_to(page, global_start, global_end)
                time.sleep(1.5)
                _dismiss_cookie(page)
                _close_drawer(page)
                _hide_drawer_and_cookies(page)

                global_file = f"{i:02d}_{safe_name}_global.png"
                _take_screenshot(page, output / global_file)
                print(f"         -> {global_file}")

                # === Detail screenshot: target_ts-centered + click evidence slice ===
                detail_window = max(int(dur * 2), 80_000_000)
                detail_start = target_ts - detail_window
                detail_end = target_ts + detail_window
                _zoom_to(page, detail_start, detail_end)
                time.sleep(1.0)
                _focus_track_y(page, focus_track)
                _click_slice_at(page, target_ts, detail_start, detail_end)
                time.sleep(0.4)
                _dismiss_cookie(page)
                _close_drawer(page)
                _hide_drawer_and_cookies(page)

                detail_file = f"{i:02d}_{safe_name}_detail.png"
                _take_screenshot(page, output / detail_file)
                print(f"         -> {detail_file}")

                results.append({
                    "name": jank_type,
                    "global": global_file,
                    "detail": detail_file,
                    "dur_ms": dur_ms,
                    "ts": ts,
                    "target_ts": target_ts,
                    "focus_track": focus_track,
                    "keywords_hit": frame.get("keywords_hit", []),
                    "evidence_slices": frame.get("evidence_slices", []),
                    "region_range": frame.get("region_range", {}),
                    "problem_description": frame.get("problem_description", ""),
                    "screenshot_reasoning": frame.get("screenshot_reasoning", ""),
                    "success": True,
                })

            browser.close()
    finally:
        _stop_trace_processor(tp_proc)

    # Write manifest
    manifest = {
        "trace_file": str(trace),
        "target_process": target["process_name"],
        "total_jank": app_jank.get("jank_frames", 0),
        "captured": len(results),
        "pin_patterns": pin_patterns,
        "viewport": {
            "width": VIEWPORT_WIDTH,
            "height": VIEWPORT_HEIGHT,
            "device_scale_factor": DEVICE_SCALE_FACTOR,
        },
        "screenshots": results,
    }
    _write(output / "screenshot_manifest.json", manifest)

    print(f"\n[Phase 2] Complete: {len(results)} issues -> {output}/")


# ─── trace_processor HTTP RPC management ──────────────────────────────

def _start_trace_processor(trace_path, override_bin=None):
    """Start trace_processor in HTTP RPC mode and wait for it to be ready.

    Discovery order for the binary:
    1. The `--trace-processor` CLI override (if provided)
    2. `shutil.which("trace_processor")` — i.e. anything on $PATH

    Returns None if no binary is found, in which case the caller falls
    back to in-browser file upload mode.
    """
    import shutil
    bin_path = override_bin or shutil.which("trace_processor")
    if not bin_path or not Path(bin_path).exists():
        print(f"         trace_processor binary not found on $PATH")
        print(f"         (pass --trace-processor /path/to/trace_processor to override)")
        print(f"         Falling back to in-browser file upload mode")
        return None

    # Kill any existing instance on the port
    try:
        subprocess.run(["pkill", "-f", "trace_processor.*--httpd"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    except: pass

    # Start trace_processor with HTTP RPC
    proc = subprocess.Popen(
        [bin_path, "-D", str(trace_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for HTTP server to be ready (poll /status)
    import socket
    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            with socket.create_connection(("127.0.0.1", TRACE_PROCESSOR_PORT), timeout=1):
                # Server is up, but trace might still be loading
                # Wait for it to actually respond to status
                import urllib.request
                req = urllib.request.Request(f"http://127.0.0.1:{TRACE_PROCESSOR_PORT}/status")
                try:
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        if resp.status == 200:
                            elapsed = time.time() - t0
                            print(f"         trace_processor ready in {elapsed:.1f}s")
                            return proc
                except: pass
        except: pass
        time.sleep(0.5)

    print(f"         WARNING: trace_processor RPC not ready after 60s")
    return proc


def _stop_trace_processor(proc):
    """Stop the trace_processor RPC server."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except: pass
    try:
        subprocess.run(["pkill", "-f", "trace_processor.*--httpd"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass


# ─── Perfetto interaction helpers ─────────────────────────────────────

def _cmd(page, cmd_id, *args):
    args_js = ", ".join(json.dumps(a) for a in args) if args else ""
    r = page.evaluate(f"""
        (() => {{
            try {{
                app.commands.runCommand('{cmd_id}'{', ' + args_js if args_js else ''});
                return 'OK';
            }} catch(e) {{ return 'ERR: ' + e.message; }}
        }})()
    """)
    if r != 'OK':
        print(f"         [cmd] {cmd_id}: {r}")
    return r


def _zoom_to(page, start_ns, end_ns):
    dur_ns = end_ns - start_ns
    r = page.evaluate(f"""
        (() => {{
            try {{
                const tl = app._activeTrace.timeline;
                const vw = tl.visibleWindow;
                const HPT = vw.start.constructor;
                const HPTS = vw.constructor;
                tl.setVisibleWindow(new HPTS(new HPT(BigInt('{start_ns}')), {dur_ns}));
                return 'OK';
            }} catch(e) {{ return 'ERR: ' + e.message; }}
        }})()
    """)
    if r != 'OK':
        print(f"         [zoom] {r}")


def _dismiss_cookie(page):
    try:
        page.evaluate("""
            document.querySelectorAll(
                '[class*="cookie"], [id*="cookie"], [class*="consent"], .fc-consent-root'
            ).forEach(el => el.remove());
            document.querySelectorAll('div').forEach(el => {
                try {
                    const s = getComputedStyle(el);
                    if (s.position === 'fixed' && el.offsetHeight < 200 &&
                        (parseInt(s.bottom) <= 20 || parseInt(s.top) > 900)) el.remove();
                } catch(e) {}
            });
        """)
    except: pass
    try: page.click("text=OK", timeout=800)
    except: pass


def _close_drawer(page):
    """Close the bottom drawer/panel (Found Events etc) if open."""
    try:
        # Check multiple selectors for the bottom panel
        is_open = page.evaluate("""
            (() => {
                const selectors = [
                    '.pf-bottom-panel', '[class*="bottom-panel"]',
                    '[class*="details-panel"]', '[class*="bottomPanel"]',
                    '[class*="detailsPanel"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetHeight > 50 && el.offsetParent !== null) {
                        return true;
                    }
                }
                return false;
            })()
        """)
        if is_open:
            _cmd(page, 'dev.perfetto.ToggleDrawer')
            time.sleep(0.3)
            # Also try Escape to close any open panels
            page.keyboard.press("Escape")
            time.sleep(0.2)
    except: pass


def _clear_search(page):
    """Clear search and switch back to normal mode."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except: pass


def _hide_drawer_and_cookies(page):
    """Hide the bottom drawer panel and any cookie banners.

    Uses only narrow, well-known selectors so we don't accidentally hide the
    pinned tracks or main scroll container.
    """
    try:
        page.evaluate("""
            (() => {
                const drawerSelectors = [
                    '.pf-bottom-panel', '.pf-details-panel',
                    '[class*="bottom-panel"]', '[class*="details-panel"]',
                    '[class*="bottomPanel"]', '[class*="detailsPanel"]'
                ];
                document.querySelectorAll(drawerSelectors.join(',')).forEach(el => {
                    try { el.style.display = 'none'; } catch(e) {}
                });

                document.querySelectorAll(
                    '[class*="cookie"], [id*="cookie"], [class*="consent"]'
                ).forEach(el => el.remove());
            })()
        """)
    except: pass


def _focus_track_y(page, focus_track):
    """Best-effort: locate a track label whose text contains focus_track and
    scroll it into view. Sticky-pinned tracks are no-ops, which is fine —
    they're already visible at the top."""
    if not focus_track:
        return
    try:
        page.evaluate(
            """(focusTrack) => {
                const needle = (focusTrack || '').toLowerCase();
                if (!needle) return false;
                const labels = Array.from(document.querySelectorAll(
                    '[class*="track-shell"], [class*="trackShell"], ' +
                    '[class*="track-name"], [class*="trackName"], ' +
                    '[class*="pf-track"]'
                ));
                for (const el of labels) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text && text.includes(needle)) {
                        try { el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch(e) {}
                        return true;
                    }
                }
                return false;
            }""",
            focus_track,
        )
    except Exception:
        pass


def _click_slice_at(page, target_ts, vis_start_ns, vis_end_ns):
    """Click on the canvas at the x-position corresponding to target_ts.

    We compute the click x in CSS pixels using the known visible window range
    (vis_start_ns, vis_end_ns) and use Playwright's real mouse click so that
    Perfetto's canvas hit-testing receives proper pointer events.
    """
    try:
        if vis_end_ns <= vis_start_ns:
            return
        ratio = (int(target_ts) - int(vis_start_ns)) / (int(vis_end_ns) - int(vis_start_ns))
        ratio = max(0.05, min(0.95, ratio))
        # Discover canvas / track area bounding box (skip the left track-label gutter)
        bbox = page.evaluate("""
            (() => {
                const candidates = document.querySelectorAll(
                    '[class*="pf-track"], [class*="trackShell"], [class*="track-shell"]'
                );
                let best = null;
                for (const el of candidates) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 200 && r.height > 10 && r.top < window.innerHeight) {
                        if (!best || r.width > best.width) best = {x: r.x, y: r.y, w: r.width, h: r.height};
                    }
                }
                if (best) return best;
                // Fallback: assume the canvas occupies the right 80% of viewport
                return {x: Math.floor(window.innerWidth * 0.2), y: Math.floor(window.innerHeight * 0.3),
                        w: Math.floor(window.innerWidth * 0.78), h: Math.floor(window.innerHeight * 0.4)};
            })()
        """)
        if not bbox:
            return
        # Aim above any left-side label gutter (assume gutter ~ 200px from x)
        gutter = 200
        track_x = bbox["x"] + gutter
        track_w = max(50, bbox["w"] - gutter)
        click_x = track_x + track_w * ratio
        click_y = bbox["y"] + bbox["h"] * 0.5
        page.mouse.move(click_x, click_y)
        page.mouse.click(click_x, click_y)
    except Exception:
        pass


def _take_screenshot(page, filepath):
    """Take a full portrait viewport screenshot.

    The viewport is intentionally fixed to a tall portrait shape (1072x1598)
    so a single page.screenshot already crops one large vertical region.
    No further cropping is applied — the bottom portion (collapsed group
    labels) is kept on purpose as visual context.
    """
    page.screenshot(path=str(filepath))


# ─── File I/O ─────────────────────────────────────────────────────────

def _load(path):
    return json.loads(Path(path).read_text())

def _write(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
