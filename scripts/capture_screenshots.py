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
            # URL is configurable via PERFETTO_UI_URL env var for intranet
            # deployments where the public ui.perfetto.dev is unreachable
            # (e.g. corporate firewall). Default is the public instance,
            # so external users don't need to set anything.
            perfetto_ui_url = os.environ.get(
                "PERFETTO_UI_URL", "https://ui.perfetto.dev"
            )
            print(f"  [2.1] Loading Perfetto UI: {perfetto_ui_url}")
            page.goto(perfetto_ui_url)
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

                target_ts = int(frame.get("target_ts", ts))
                focus_track = frame.get("focus_track", "Actual Timeline")

                # ── Setup (shared between global and detail) ──────
                # Step 1: Reset state
                _cmd(page, 'dev.perfetto.UnpinAllTracks')
                _cmd(page, 'dev.perfetto.CollapseAllGroups')
                _close_drawer(page)
                _clear_search(page)
                time.sleep(0.3)

                # Step 2: ExpandAll → CollapseAll (create all track nodes)
                _cmd(page, 'dev.perfetto.ExpandAllGroups')
                time.sleep(3)
                _cmd(page, 'dev.perfetto.CollapseAllGroups')
                time.sleep(0.5)

                # Step 3: Pin ONLY SF main thread + composer (compact pinned area).
                # Use exact "surfaceflinger {tid}" to avoid pinning all SF sub-threads.
                sf_tid = thread_map.get('sf_main_tid', '')
                if sf_tid:
                    _cmd(page, 'dev.perfetto.PinTracksByRegex', f'surfaceflinger {sf_tid}')
                    time.sleep(0.2)
                _cmd(page, 'dev.perfetto.PinTracksByRegex', 'composer-servic')
                time.sleep(0.2)
                page.keyboard.press("Escape")
                time.sleep(0.2)
                page.keyboard.press("Escape")
                time.sleep(0.2)

                # Step 4: Selectively expand target process + Frame Timeline
                _cmd(page, 'dev.perfetto.ExpandTracksByRegex', target['process_name'])
                time.sleep(0.5)
                _cmd(page, 'dev.perfetto.ExpandTracksByRegex', str(target['pid']))
                time.sleep(0.5)
                _cmd(page, 'dev.perfetto.ExpandTracksByRegex', 'Expected Timeline')
                time.sleep(0.5)
                _cmd(page, 'dev.perfetto.ExpandTracksByRegex', 'Actual Timeline')
                time.sleep(0.5)
                page.keyboard.press("Escape")
                time.sleep(0.2)

                # Step 5: Collapse noise (system + non-target process groups)
                for noise in ['CPU Scheduling', 'CPU Frequency', 'Ftrace',
                              'GPU', 'Scheduler', 'System', 'Kernel']:
                    _cmd(page, 'dev.perfetto.CollapseTracksByRegex', noise)
                    time.sleep(0.1)

                # Step 6: Force-hide sidebar + bottom panel + cookie via CSS
                _force_hide_ui_noise(page)

                # ── GLOBAL screenshot ────────────────────────────
                global_start = int(tp_state["trace_start"])
                global_end = int(tp_state["trace_end"])
                _zoom_to(page, global_start, global_end)
                time.sleep(5)
                _force_hide_ui_noise(page)

                # Search "DrawFrames" to center on RenderThread.
                # This puts main thread ABOVE and GPU completion BELOW in view,
                # covering the full Frame Timeline: main → RT → GPU completion.
                # (Searching "Choreographer" puts main thread at top, cutting off
                # GPU completion at the bottom.)
                _search_and_navigate(page, "DrawFrames")

                # Perfetto centers the match — RenderThread ends up in the
                # middle of the viewport. GPU completion (2 rows) below RT
                # then gets cut off at the bottom edge. Shift scroll down by
                # ~160px so the pipeline is packed higher and GPU completion
                # is fully visible at the bottom.
                page.evaluate("""(() => {
                    const panels = document.querySelectorAll(
                        '[class*="scroll"], [class*="panel-container"], [class*="viewer"]'
                    );
                    for (const p of panels) {
                        if (p.scrollHeight > p.clientHeight && p.clientHeight > 200) {
                            p.scrollTop = p.scrollTop + 160;
                            return;
                        }
                    }
                    window.scrollBy(0, 160);
                })()""")
                time.sleep(0.5)

                global_file = f"{i:02d}_{safe_name}_global.png"
                page.screenshot(path=str(output / global_file))
                print(f"         -> {global_file}")

                # Save scroll position (Frame Timeline area)
                saved_scroll = page.evaluate("""(() => {
                    const panels = document.querySelectorAll(
                        '[class*="scroll"], [class*="panel-container"], [class*="viewer"]'
                    );
                    for (const p of panels) {
                        if (p.scrollHeight > p.clientHeight && p.clientHeight > 200) {
                            return {top: p.scrollTop, height: p.scrollHeight};
                        }
                    }
                    return {top: window.scrollY, height: document.body.scrollHeight};
                })()""")

                # ── DETAIL screenshot ────────────────────────────
                detail_window = max(int(dur * 2), 80_000_000)
                detail_start = target_ts - detail_window
                detail_end = target_ts + detail_window
                _zoom_to(page, detail_start, detail_end)
                time.sleep(3)

                # Restore scroll to Frame Timeline area
                page.evaluate(f"""(() => {{
                    const panels = document.querySelectorAll(
                        '[class*="scroll"], [class*="panel-container"], [class*="viewer"]'
                    );
                    for (const p of panels) {{
                        if (p.scrollHeight > p.clientHeight && p.clientHeight > 200) {{
                            p.scrollTop = {saved_scroll.get('top', 0)};
                            return;
                        }}
                    }}
                    window.scrollTo(0, {saved_scroll.get('top', 0)});
                }})()""")
                time.sleep(1.5)

                _click_slice_at(page, target_ts, detail_start, detail_end)
                time.sleep(0.4)
                _force_hide_ui_noise(page)

                detail_file = f"{i:02d}_{safe_name}_detail.png"
                page.screenshot(path=str(output / detail_file))

                # Annotate with highlight box + title bar
                evidence = frame.get("evidence_slices", [])
                _annotate_detail(
                    output / detail_file,
                    target_ts, detail_start, detail_end,
                    jank_type, evidence,
                )
                print(f"         -> {detail_file} (annotated)")

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


def _force_hide_ui_noise(page):
    """Force-hide sidebar, bottom panel, and cookie via CSS.
    This reclaims ~300px of vertical space for track content."""
    try:
        page.evaluate("""(() => {
            // Hide sidebar
            document.querySelectorAll('.pf-sidebar, [class*="sidebar"], nav').forEach(el => {
                if (el.offsetWidth > 50) el.style.display = 'none';
            });
            // Hide bottom panel
            const kw = ['current selection', 'ftrace events', 'nothing selected'];
            document.querySelectorAll('div, section').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.top < window.innerHeight * 0.6 || r.height < 20) return;
                const t = (el.textContent || '').toLowerCase().trim();
                if (t.length > 300) return;
                for (const k of kw) {
                    if (t.includes(k)) {
                        let n = el;
                        while (n.parentElement && n.parentElement.getBoundingClientRect().top > window.innerHeight * 0.6) n = n.parentElement;
                        n.style.display = 'none';
                        let s = n.nextElementSibling;
                        while (s) { s.style.display = 'none'; s = s.nextElementSibling; }
                        return;
                    }
                }
            });
            // Cookie cleanup
            document.querySelectorAll('[class*="cookie"],[id*="cookie"],[class*="consent"]').forEach(el => el.remove());
            document.querySelectorAll('div').forEach(el => {
                try {
                    const s = getComputedStyle(el);
                    if (s.position === 'fixed' && el.offsetHeight < 200 &&
                        (parseInt(s.bottom) <= 20 || parseInt(s.top) > window.innerHeight - 200)) el.remove();
                } catch(e) {}
            });
        })()""")
    except:
        pass


def _collapse_bottom_panel(page):
    """Hide the 'Current Selection' / 'Ftrace Events' bottom panel entirely.

    Uses a position-based approach: any element whose top edge is in the
    bottom 35% of the viewport AND contains tab-like text (Current Selection,
    Ftrace Events, Nothing selected) gets hidden via display:none, along
    with all its siblings below it.
    """
    try:
        page.evaluate("""
            (() => {
                const vh = window.innerHeight;
                const threshold = vh * 0.65;

                // 1. Hide any element that looks like the bottom panel tab strip
                //    by checking for characteristic text content
                const keywords = ['current selection', 'ftrace events', 'nothing selected',
                                  'selected', 'filter'];
                document.querySelectorAll('div, section').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.top < threshold || r.height < 20 || r.height > vh * 0.5) return;
                    const text = (el.textContent || '').toLowerCase().trim();
                    if (text.length > 200) return;
                    for (const kw of keywords) {
                        if (text.includes(kw)) {
                            // Found the panel — hide it and everything below
                            let node = el;
                            // Walk up to find the panel container
                            while (node.parentElement &&
                                   node.parentElement.getBoundingClientRect().top > threshold) {
                                node = node.parentElement;
                            }
                            node.style.display = 'none';
                            // Also hide siblings after it
                            let sib = node.nextElementSibling;
                            while (sib) {
                                sib.style.display = 'none';
                                sib = sib.nextElementSibling;
                            }
                            return;
                        }
                    }
                });

                // 2. Fallback: hide by known class selectors
                const selectors = [
                    '.pf-bottom-panel', '.pf-details-panel',
                    '[class*="bottom-panel"]', '[class*="details-panel"]',
                    '[class*="bottomPanel"]', '[class*="detailsPanel"]'
                ];
                document.querySelectorAll(selectors.join(',')).forEach(el => {
                    try { el.style.display = 'none'; } catch(e) {}
                });
            })()
        """)
    except:
        pass


def _hide_drawer_and_cookies(page):
    """Collapse the bottom panel and remove cookie banners before screenshot."""
    _collapse_bottom_panel(page)
    try:
        page.evaluate("""
            (() => {

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

    Uses the largest canvas element's bounding rect to calculate the precise
    click position, then uses Playwright's real mouse click for Perfetto's
    canvas hit-testing.
    """
    try:
        if vis_end_ns <= vis_start_ns:
            return
        ratio = (int(target_ts) - int(vis_start_ns)) / (int(vis_end_ns) - int(vis_start_ns))
        ratio = max(0.05, min(0.95, ratio))

        # Find the largest canvas (the main trace rendering surface)
        canvas_rect = page.evaluate("""
            (() => {
                let best = null;
                let maxArea = 0;
                document.querySelectorAll('canvas').forEach(c => {
                    const r = c.getBoundingClientRect();
                    const area = r.width * r.height;
                    if (area > maxArea) {
                        maxArea = area;
                        best = {x: r.x, y: r.y, w: r.width, h: r.height};
                    }
                });
                return best || {x: 0, y: 0, w: window.innerWidth, h: window.innerHeight};
            })()
        """)
        if not canvas_rect:
            return

        # Click at the computed x on the canvas, vertically centered
        click_x = canvas_rect["x"] + canvas_rect["w"] * ratio
        click_y = canvas_rect["y"] + canvas_rect["h"] * 0.35  # upper-center of canvas
        page.mouse.click(click_x, click_y)
        time.sleep(0.2)
        # Try a second click slightly lower in case first missed
        page.mouse.click(click_x, click_y + 40)
    except Exception:
        pass


def _annotate_detail(filepath, target_ts, vis_start, vis_end, jank_type, evidence):
    """Overlay a bounded highlight rectangle + title bar on detail screenshot.

    The rectangle has clear top/bottom/left/right borders covering the pinned
    tracks area (roughly top 40% of the image) at the x-position of target_ts.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return

    try:
        img = Image.open(str(filepath)).convert("RGBA")
        w, h = img.size

        if vis_end <= vis_start:
            return
        ts_ratio = (int(target_ts) - int(vis_start)) / (int(vis_end) - int(vis_start))
        ts_ratio = max(0.02, min(0.98, ts_ratio))

        # Track area: after left label gutter (~18% of width)
        gutter = int(w * 0.18)
        track_w = w - gutter
        center_x = gutter + int(track_w * ts_ratio)
        half_w = max(int(track_w * 0.06), 50)

        x_left = max(gutter, center_x - half_w)
        x_right = min(w - 10, center_x + half_w)

        # Bounded rectangle: top = below title bar, bottom = ~45% of image height
        # This covers the pinned tracks area without extending to collapsed groups
        bar_h = 40
        rect_top = bar_h + 10
        rect_bottom = int(h * 0.45)

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Semi-transparent fill + solid red border (all 4 sides)
        draw.rectangle(
            [x_left, rect_top, x_right, rect_bottom],
            fill=(255, 50, 50, 30),
            outline=(255, 60, 60, 200),
            width=3,
        )

        # Corner markers for emphasis (short thick lines at each corner)
        corner_len = 20
        corner_w = 4
        red = (255, 40, 40, 230)
        for cx, cy in [(x_left, rect_top), (x_right, rect_top),
                       (x_left, rect_bottom), (x_right, rect_bottom)]:
            dx = corner_len if cx == x_left else -corner_len
            dy = corner_len if cy == rect_top else -corner_len
            draw.line([(cx, cy), (cx + dx, cy)], fill=red, width=corner_w)
            draw.line([(cx, cy), (cx, cy + dy)], fill=red, width=corner_w)

        # Title bar at very top
        top_ev = evidence[0] if evidence else None
        if top_ev:
            title = f"{jank_type}  |  {top_ev['name']}@{top_ev['thread']} ({top_ev['dur_ms']}ms)"
        else:
            title = jank_type
        title = title[:120]

        draw.rectangle([0, 0, w, bar_h], fill=(20, 20, 20, 210))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        except Exception:
            font = ImageFont.load_default()
        draw.text((14, 9), title, fill=(255, 200, 200, 255), font=font)

        # Label below the box
        try:
            small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except Exception:
            small_font = font
        label = "target_ts focus"
        label_x = max(gutter, center_x - 60)
        draw.text((label_x, rect_bottom + 6), label, fill=(255, 80, 80, 200), font=small_font)

        result = Image.alpha_composite(img, overlay)
        result.convert("RGB").save(str(filepath))
    except Exception as e:
        print(f"         [annotate] {e}")


def _search_and_navigate(page, search_term):
    """Use Perfetto's omnibox to search for a slice and scroll to its track.

    This is the reliable way to navigate in Perfetto's virtualized track list.
    Typing a slice name (e.g. "DrawFrames") in the omnibox triggers Perfetto's
    internal search which scrolls the virtualized list to the matching track.
    """
    if not search_term:
        return
    try:
        omnibox = page.locator('input').first
        omnibox.click()
        time.sleep(0.3)
        omnibox.fill(search_term)
        time.sleep(0.3)
        page.keyboard.press('Enter')
        time.sleep(0.5)
        page.keyboard.press('Enter')
        time.sleep(0.3)
        page.keyboard.press('Escape')
        time.sleep(0.3)
    except Exception:
        pass


def _scroll_to_track(page, slice_search_term):
    """Scroll Perfetto UI to show a track by searching for a SLICE on it.

    Perfetto uses virtualized scrolling — off-screen tracks have no DOM.
    The reliable method: use Perfetto's search mode to find a slice by name
    (e.g. "DrawFrames" finds a slice on RenderThread), which causes Perfetto
    to scroll the track into view and highlight the match.

    slice_search_term should be a SLICE name (not track name).
    """
    if not slice_search_term:
        return
    try:
        # Activate search mode via Perfetto command
        _cmd(page, 'dev.perfetto.SwitchToSearchMode')
        time.sleep(0.3)
        # Type search term — Perfetto will search slice names
        page.keyboard.type(slice_search_term, delay=20)
        time.sleep(0.5)
        # Navigate to first match — this scrolls the view
        _cmd(page, 'dev.perfetto.SearchNext')
        time.sleep(0.5)
        # Search again to ensure we're on the right match
        _cmd(page, 'dev.perfetto.SearchNext')
        time.sleep(0.3)
    except Exception:
        pass


def _take_clipped_screenshot(page, filepath, anchor_track):
    """Take a screenshot clipped to the canvas area near anchor_track.

    Strategy (from the reference implementation):
    1. Find the canvas element's bounding rect (skip left track-label gutter)
    2. Find the anchor track's y-position
    3. Clip: x from canvas left, y from slightly above anchor, full canvas width,
       height = viewport height covering the tracks of interest
    """
    try:
        clip = page.evaluate("""(anchorName) => {
            const vw = window.innerWidth;
            const vh = window.innerHeight;

            // Find the main canvas (largest one)
            let canvas = null;
            let maxArea = 0;
            document.querySelectorAll('canvas').forEach(c => {
                const r = c.getBoundingClientRect();
                const area = r.width * r.height;
                if (area > maxArea) { maxArea = area; canvas = c; }
            });

            // Canvas left edge (this naturally clips out the track label sidebar)
            let cx = 0, cw = vw;
            if (canvas) {
                const cr = canvas.getBoundingClientRect();
                cx = Math.max(0, Math.floor(cr.x));
                cw = Math.floor(cr.width);
            }

            // Find anchor track y-position
            let anchorY = vh * 0.15;  // default: 15% from top
            if (anchorName) {
                const needle = anchorName.toLowerCase();
                const els = document.querySelectorAll(
                    '[class*="track"] [class*="title"], [class*="track"] [class*="name"],' +
                    '[class*="shell"], [class*="pf-track"]'
                );
                for (const el of els) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text && text.includes(needle)) {
                        const r = el.getBoundingClientRect();
                        anchorY = r.top;
                        break;
                    }
                }
            }

            // Detect bottom panel top edge (to exclude from clip)
            let panelTop = vh;
            const keywords = ['current selection', 'ftrace events', 'nothing selected'];
            document.querySelectorAll('div, section').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.top < vh * 0.5 || r.height < 20) return;
                const text = (el.textContent || '').toLowerCase().trim();
                if (text.length > 300) return;
                for (const kw of keywords) {
                    if (text.includes(kw) && r.top < panelTop) {
                        panelTop = r.top;
                    }
                }
            });

            // Clip region: start above anchor, extend to just above bottom panel
            const clipY = Math.max(0, Math.floor(anchorY - vh * 0.08));
            const maxH = Math.floor(panelTop - clipY - 5);
            const clipH = Math.max(200, Math.min(maxH, Math.floor(vh * 0.85)));

            return {x: cx, y: clipY, width: cw, height: clipH};
        }""", anchor_track)

        if clip and clip.get("width", 0) > 100 and clip.get("height", 0) > 100:
            page.screenshot(path=str(filepath), clip=clip)
            return
    except Exception as e:
        print(f"         [clip] fallback: {e}")

    # Fallback: full viewport
    page.screenshot(path=str(filepath))


# ─── File I/O ─────────────────────────────────────────────────────────

def _load(path):
    return json.loads(Path(path).read_text())

def _write(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
