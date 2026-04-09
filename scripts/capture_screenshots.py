#!/usr/bin/env python3
"""Phase 2: Capture Perfetto UI screenshots for top jank issues.

v4: Uses trace_processor HTTP RPC for fast loading.
- Starts trace_processor --httpd in background (loads trace once)
- Perfetto UI connects via RPC, no file upload, no in-browser parsing
- Tall viewport (1920x2400) for long screenshots
- Full rendering pipeline pin: Timeline → App → SF → HWC → CrtcCommit
- Auto-crop screenshots to pinned content area
- DOM-based wait (not fixed sleep) for trace ready state

Two screenshot types per jank:
- Overview: ±500ms context for pattern recognition
- Detail: tight zoom for slice text readability
"""
import argparse
import json
import os
import subprocess
import time
import sys
from pathlib import Path


# Viewport dimensions — tall to show many pinned tracks
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 2400

# trace_processor binary for HTTP RPC mode
TRACE_PROCESSOR_BIN = "/home/wq/workspace/test_render_traces/trace_processor"
TRACE_PROCESSOR_PORT = 9001


def main():
    parser = argparse.ArgumentParser(description="Capture Perfetto trace screenshots")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    trace = Path(args.trace)
    analysis = Path(args.analysis_dir)
    output = Path(args.output_dir) if args.output_dir else analysis / "screenshots"
    output.mkdir(parents=True, exist_ok=True)

    # Load analysis data
    app_jank = _load(analysis / "app_jank.json")
    target = _load(analysis / "target_process.json")
    thread_map = _load(analysis / "thread_map.json")

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
    tp_proc = _start_trace_processor(trace)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})

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

                # Collapse all non-pinned tracks (pinned stay at top)
                _cmd(page, 'dev.perfetto.CollapseAllGroups')
                time.sleep(0.3)

                # === Overview screenshot ===
                overview_pad = max(int(dur * 3), 500_000_000)  # min 500ms total context
                _zoom_to(page, ts - overview_pad, ts + dur + overview_pad)
                time.sleep(1.5)
                _dismiss_cookie(page)
                _close_drawer(page)
                _hide_nonpinned_tracks(page)

                overview_file = f"{i:02d}_{safe_name}_overview.png"
                _take_screenshot(page, output / overview_file)
                print(f"         -> {overview_file}")

                # === Detail screenshot: tight zoom for slice readability ===
                detail_window = max(int(dur * 0.5), 50_000_000)
                _zoom_to(page, ts - detail_window, ts + dur + detail_window)
                time.sleep(1.0)
                _dismiss_cookie(page)
                _close_drawer(page)
                _hide_nonpinned_tracks(page)

                detail_file = f"{i:02d}_{safe_name}_detail.png"
                _take_screenshot(page, output / detail_file)
                print(f"         -> {detail_file}")

                results.append({
                    "name": jank_type,
                    "overview": overview_file,
                    "detail": detail_file,
                    "dur_ms": dur_ms,
                    "ts": ts,
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
        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        "screenshots": results,
    }
    _write(output / "screenshot_manifest.json", manifest)

    print(f"\n[Phase 2] Complete: {len(results)} issues -> {output}/")


# ─── trace_processor HTTP RPC management ──────────────────────────────

def _start_trace_processor(trace_path):
    """Start trace_processor in HTTP RPC mode and wait for it to be ready."""
    if not Path(TRACE_PROCESSOR_BIN).exists():
        print(f"         WARNING: trace_processor not found at {TRACE_PROCESSOR_BIN}")
        print(f"         Falling back to file upload mode")
        return None

    # Kill any existing instance on the port
    try:
        subprocess.run(["pkill", "-f", "trace_processor.*--httpd"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    except: pass

    # Start trace_processor with HTTP RPC
    proc = subprocess.Popen(
        [TRACE_PROCESSOR_BIN, "-D", str(trace_path)],
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


def _hide_nonpinned_tracks(page):
    """Hide non-pinned tracks and bottom panels to maximize useful content.

    Strategy: find all panel containers in Perfetto UI and hide everything
    that isn't the overview bar, pinned tracks, or timeline header.
    """
    try:
        page.evaluate("""
            (() => {
                // 1. Hide the bottom details/drawer panel
                document.querySelectorAll(
                    '.pf-bottom-panel, .pf-details-panel, ' +
                    '[class*="bottom-panel"], [class*="details-panel"], ' +
                    '[class*="bottomPanel"], [class*="detailsPanel"]'
                ).forEach(el => el.style.display = 'none');

                // 2. Find and hide the non-pinned scrollable track area
                //    In Perfetto, the structure is:
                //    - Overview/timeline bar (keep)
                //    - Pinned tracks panel (keep)
                //    - Scrollable tracks panel (HIDE - this has all the collapsed groups)
                //    Look for the panel after pinned tracks
                const allPanels = document.querySelectorAll(
                    '.pf-panel-container > div, ' +
                    '[class*="panel-container"] > div'
                );

                // 3. More aggressive: hide any element that contains collapsed track groups
                //    but is NOT a pinned track
                const scrollContainer = document.querySelector(
                    '.pf-tracks-panel, [class*="tracks-panel"], ' +
                    '[class*="scrolling-panel"], [class*="scrollingPanel"]'
                );
                if (scrollContainer) {
                    scrollContainer.style.display = 'none';
                }

                // 4. Cookie cleanup
                document.querySelectorAll(
                    '[class*="cookie"], [id*="cookie"], [class*="consent"]'
                ).forEach(el => el.remove());
            })()
        """)
    except: pass


def _take_screenshot(page, filepath):
    """Take a cropped screenshot focusing on the pinned tracks area.

    Crops from top to the bottom of the last pinned track,
    eliminating wasted space from collapsed non-pinned track groups.
    """
    # Detect the effective content height (overview bar + pinned tracks)
    try:
        content_bottom = page.evaluate("""
            (() => {
                // Find pinned track area - look for pin icons or pinned container
                const pinIcons = document.querySelectorAll(
                    '[class*="pin"], .pf-pin-icon, [title*="Unpin"]'
                );
                let maxBottom = 0;
                pinIcons.forEach(el => {
                    const r = el.getBoundingClientRect();
                    // Walk up to find the track row container
                    let parent = el.closest('[class*="track"], [class*="row"]') || el;
                    const pr = parent.getBoundingClientRect();
                    if (pr.bottom > maxBottom) maxBottom = pr.bottom;
                });

                // If we found pinned tracks, add some padding
                if (maxBottom > 100) {
                    return Math.min(maxBottom + 30, window.innerHeight);
                }

                // Fallback: find the last visible track with content
                const tracks = document.querySelectorAll(
                    '[class*="track-shell"], [class*="trackShell"]'
                );
                tracks.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.bottom > maxBottom && r.top < window.innerHeight) {
                        maxBottom = r.bottom;
                    }
                });

                if (maxBottom > 100) {
                    return Math.min(maxBottom + 30, window.innerHeight);
                }

                // Ultimate fallback: use 60% of viewport (pinned tracks typically fill top portion)
                return Math.floor(window.innerHeight * 0.6);
            })()
        """)

        if content_bottom and content_bottom > 200:
            page.screenshot(
                path=str(filepath),
                clip={"x": 0, "y": 0, "width": VIEWPORT_WIDTH, "height": int(content_bottom)}
            )
            return
    except Exception as e:
        print(f"         [crop] fallback to full: {e}")

    # Fallback: full viewport
    page.screenshot(path=str(filepath))


# ─── File I/O ─────────────────────────────────────────────────────────

def _load(path):
    return json.loads(Path(path).read_text())

def _write(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
