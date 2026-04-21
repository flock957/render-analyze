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

NS_PER_MS = 1_000_000
DETAIL_LEFT_MIN_NS = 40 * NS_PER_MS
DETAIL_LEFT_MAX_NS = 90 * NS_PER_MS
DETAIL_RIGHT_MIN_NS = 100 * NS_PER_MS
DETAIL_RIGHT_MAX_NS = 220 * NS_PER_MS

IGNORED_EVIDENCE_THREAD_TOKENS = (
    "heaptaskdaemon",
    "finalizer",
    "referencequeue",
    "profile saver",
    "signal catcher",
)

IGNORED_EVIDENCE_SLICE_TOKENS = (
    "binder transaction",
    "concurrent copying gc",
    "young concurrent copying gc",
    "background concurrent copying gc",
    "background young concurrent copying gc",
)


def _scaled_timeout(size_mb: int, baseline_sec: int, per_mb_sec: float) -> int:
    return max(baseline_sec, int(size_mb * per_mb_sec))


def _append_unique_pattern(patterns, seen, pattern):
    text = str(pattern or "").strip()
    if not text or text in seen:
        return
    patterns.append(text)
    seen.add(text)


def _append_thread_entry_patterns(patterns, seen, entry, include_name=False):
    if not entry:
        return
    name = str(entry.get("name", "")).strip()
    tid = entry.get("tid")
    if name and tid:
        _append_unique_pattern(patterns, seen, f"{name} {tid}")
    if include_name and name:
        _append_unique_pattern(patterns, seen, name)


def _pick_thread_entries(entries, preferred_tokens, limit=1):
    if not entries:
        return []
    picked = []
    seen = set()
    for token in [str(token or "").strip().lower() for token in preferred_tokens]:
        if not token:
            continue
        for entry in entries:
            name = str(entry.get("name", "")).strip()
            key = (name, entry.get("tid"))
            if key in seen:
                continue
            if token in name.lower():
                picked.append(entry)
                seen.add(key)
                if len(picked) >= limit:
                    return picked
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        key = (name, entry.get("tid"))
        if not name or key in seen:
            continue
        picked.append(entry)
        seen.add(key)
        if len(picked) >= limit:
            return picked
    return picked


def _should_include_evidence_thread(ev):
    thread_name = str(ev.get("thread", "")).strip().lower()
    slice_name = str(ev.get("name", "")).strip().lower()
    if not thread_name:
        return False
    if thread_name == "gc" or thread_name.startswith("gc "):
        return False
    if any(token in thread_name for token in IGNORED_EVIDENCE_THREAD_TOKENS):
        return False
    if any(token in slice_name for token in IGNORED_EVIDENCE_SLICE_TOKENS):
        return False
    return True


def _iter_relevant_evidence(frame):
    for item in frame.get("evidence_slices", []) or []:
        if isinstance(item, dict) and _should_include_evidence_thread(item):
            yield item


def _primary_span_ns(frame):
    primary = int(frame.get("dur", 0) or 0)
    for item in _iter_relevant_evidence(frame):
        try:
            primary = max(primary, int(item.get("dur_ns", 0) or 0))
        except Exception:
            continue
    return max(primary, 40 * NS_PER_MS)


def _fit_window(total_start, total_end, desired_start, desired_end):
    total_start = int(total_start)
    total_end = int(total_end)
    desired_start = int(desired_start)
    desired_end = int(desired_end)
    if total_end <= total_start:
        return total_start, total_end
    width = max(1, desired_end - desired_start)
    width = min(width, total_end - total_start)
    start = desired_start
    end = start + width
    if start < total_start:
        start = total_start
        end = start + width
    if end > total_end:
        end = total_end
        start = end - width
    return int(start), int(end)


def _clamp_int(value, lower, upper):
    return max(lower, min(upper, int(value)))


def _resolve_detail_anchor(focus_track):
    text = str(focus_track or "").strip()
    normalized = text.lower()
    if not normalized:
        return "Actual Timeline"
    if "dequeuebuffer" in normalized:
        return "RenderThread"
    if "presentfence" in normalized:
        return "surfaceflinger"
    if "gpu completion" in normalized:
        return "GPU completion"
    if "surfaceflinger" in normalized:
        return "surfaceflinger"
    if "renderthread" in normalized:
        return "RenderThread"
    if "actual timeline" in normalized:
        return "Actual Timeline"
    return text


def _issue_context(frame, detail_anchor):
    jank_norm = str(frame.get("jank_type", "") or "").strip().lower()
    anchor_norm = str(detail_anchor or "").strip().lower()
    return {
        "display_related": (
            "display hal" in jank_norm
            or "surfaceflinger" in anchor_norm
            or "presentfence" in anchor_norm
            or "composer" in anchor_norm
        ),
        "buffer_related": "buffer stuffing" in jank_norm or "dequeuebuffer" in anchor_norm,
        "deadline_related": "app deadline missed" in jank_norm,
        "unknown_related": "unknown jank" in jank_norm or "actual timeline" in anchor_norm,
    }


def _build_detail_window(frame, trace_start, trace_end, detail_anchor):
    trace_start = int(trace_start)
    trace_end = int(trace_end)
    target_ts = int(frame.get("target_ts", frame.get("ts", trace_start)) or trace_start)
    primary_span = _primary_span_ns(frame)
    issue = _issue_context(frame, detail_anchor)

    left_min = DETAIL_LEFT_MIN_NS
    left_max = DETAIL_LEFT_MAX_NS
    right_min = DETAIL_RIGHT_MIN_NS
    right_max = DETAIL_RIGHT_MAX_NS

    if issue["display_related"]:
        left_min = 50 * NS_PER_MS
        left_max = 110 * NS_PER_MS
        right_min = 120 * NS_PER_MS
        right_max = 260 * NS_PER_MS
    elif issue["unknown_related"]:
        left_min = 45 * NS_PER_MS
        left_max = 100 * NS_PER_MS
        right_min = 90 * NS_PER_MS
        right_max = 180 * NS_PER_MS

    left_ns = _clamp_int(max(int(primary_span * 0.45), left_min), left_min, left_max)
    right_ns = _clamp_int(max(int(primary_span * 1.10), right_min), right_min, right_max)
    return _fit_window(trace_start, trace_end, target_ts - left_ns, target_ts + right_ns)


def _build_global_pin_patterns(frame, thread_map):
    patterns = []
    seen = set()
    detail_anchor = _resolve_detail_anchor(frame.get("focus_track", ""))
    issue = _issue_context(frame, detail_anchor)

    _append_unique_pattern(patterns, seen, "Expected Timeline")
    _append_unique_pattern(patterns, seen, "Actual Timeline")
    _append_thread_entry_patterns(patterns, seen, (thread_map.get("app_main_thread") or [None])[0], include_name=False)
    _append_thread_entry_patterns(patterns, seen, (thread_map.get("app_render_threads") or [None])[0], include_name=False)
    _append_thread_entry_patterns(
        patterns,
        seen,
        (_pick_thread_entries(thread_map.get("app_hwui_threads") or [], ("gpu completion",), limit=1) or [None])[0],
        include_name=False,
    )

    sf_tid = thread_map.get("sf_main_tid")
    if sf_tid:
        _append_unique_pattern(patterns, seen, f"surfaceflinger {sf_tid}")
    else:
        _append_unique_pattern(patterns, seen, "surfaceflinger")

    if issue["display_related"]:
        for entry in _pick_thread_entries(thread_map.get("hwc_threads") or [], ("composer", "hwc", "overlay"), limit=2):
            _append_thread_entry_patterns(patterns, seen, entry, include_name=False)
        for entry in _pick_thread_entries(
            (thread_map.get("sf_render_engine") or []) + (thread_map.get("sf_gpu_completion") or []),
            ("renderengine", "gpu completion"),
            limit=2,
        ):
            _append_thread_entry_patterns(patterns, seen, entry, include_name=False)
    return patterns


def _build_detail_pin_patterns(frame, thread_map, detail_anchor):
    patterns = []
    seen = set()
    issue = _issue_context(frame, detail_anchor)

    _append_unique_pattern(patterns, seen, "Expected Timeline")
    _append_unique_pattern(patterns, seen, "Actual Timeline")
    _append_thread_entry_patterns(patterns, seen, (thread_map.get("app_main_thread") or [None])[0], include_name=False)
    _append_thread_entry_patterns(patterns, seen, (thread_map.get("app_render_threads") or [None])[0], include_name=False)
    _append_thread_entry_patterns(
        patterns,
        seen,
        (_pick_thread_entries(thread_map.get("app_hwui_threads") or [], ("gpu completion",), limit=1) or [None])[0],
        include_name=False,
    )

    if issue["display_related"]:
        sf_tid = thread_map.get("sf_main_tid")
        if sf_tid:
            _append_unique_pattern(patterns, seen, f"surfaceflinger {sf_tid}")
        else:
            _append_unique_pattern(patterns, seen, "surfaceflinger")
        for entry in _pick_thread_entries(thread_map.get("hwc_threads") or [], ("composer", "hwc", "overlay"), limit=1):
            _append_thread_entry_patterns(patterns, seen, entry, include_name=False)
    return patterns


def _apply_pin_patterns(page, patterns):
    _cmd(page, 'dev.perfetto.UnpinAllTracks')
    time.sleep(0.2)
    _cmd(page, 'dev.perfetto.CollapseAllGroups')
    time.sleep(0.2)
    for pattern in patterns:
        _cmd(page, 'dev.perfetto.PinTracksByRegex', pattern)
        time.sleep(0.15)
    _cmd(page, 'dev.perfetto.ExpandTracksByRegex', 'Expected Timeline')
    time.sleep(0.15)
    _cmd(page, 'dev.perfetto.ExpandTracksByRegex', 'Actual Timeline')
    time.sleep(0.15)
    for _ in range(3):
        page.keyboard.press("Escape")
        time.sleep(0.1)
    _force_hide_ui_noise(page)


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
    tp_proc = _start_trace_processor(trace, args.trace_processor, size_mb=size_mb)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--ignore-certificate-errors'])
            page = browser.new_page(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                device_scale_factor=DEVICE_SCALE_FACTOR,
                ignore_https_errors=True,
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
                if tp_proc is not None:
                    print("         WARNING: trace_processor RPC was started but Perfetto UI did not auto-detect it.")
                    print("                  Falling back to file upload. The RPC server is idle but still resident in memory.")
                else:
                    print("         Using file upload...")
                try:
                    with page.expect_file_chooser(timeout=5000) as fc:
                        page.click("text=Open trace file")
                    fc.value.set_files(str(trace))
                except Exception as e:
                    print(f"         File upload failed: {e}")
                    raise

            # --- Wait for trace to be fully loaded (DOM-based, not sleep) ---
            trace_ready_timeout = _scaled_timeout(size_mb, baseline_sec=120, per_mb_sec=0.6)
            print(f"  [2.2.1] Waiting for trace to be ready (timeout {trace_ready_timeout}s)...")
            try:
                page.wait_for_function(
                    "() => window.app && window.app._activeTrace && window.app._activeTrace.timeline",
                    timeout=trace_ready_timeout * 1000,
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

                # Step 3: Collapse noise (system groups stay hidden below pinned tracks)
                for noise in ['CPU Scheduling', 'CPU Frequency', 'Ftrace',
                              'GPU', 'Scheduler', 'System', 'Kernel']:
                    _cmd(page, 'dev.perfetto.CollapseTracksByRegex', noise)
                    time.sleep(0.1)

                # Step 4: Force-hide sidebar + bottom panel + cookie via CSS
                _force_hide_ui_noise(page)

                # ── GLOBAL screenshot ────────────────────────────
                detail_anchor = _resolve_detail_anchor(focus_track)
                global_patterns = _build_global_pin_patterns(frame, thread_map)
                _apply_pin_patterns(page, global_patterns)
                global_start = int(tp_state["trace_start"])
                global_end = int(tp_state["trace_end"])
                _zoom_to(page, global_start, global_end)
                time.sleep(5)
                _force_hide_ui_noise(page)

                global_file = f"{i:02d}_{safe_name}_global.png"
                _take_validated_screenshot(
                    page,
                    output / global_file,
                    'surfaceflinger' if any('surfaceflinger' in p.lower() for p in global_patterns) else detail_anchor,
                    track_hints=global_patterns,
                    height_ratio=0.78,
                    prefer_focus=False,
                    retry_wait_sec=4,
                )
                print(f"         -> {global_file}")

                # ── DETAIL screenshot ────────────────────────────
                detail_patterns = _build_detail_pin_patterns(frame, thread_map, detail_anchor)
                _apply_pin_patterns(page, detail_patterns)
                detail_start, detail_end = _build_detail_window(
                    frame,
                    int(tp_state["trace_start"]),
                    int(tp_state["trace_end"]),
                    detail_anchor,
                )
                _zoom_to(page, detail_start, detail_end)
                time.sleep(3)

                _click_slice_at(page, target_ts, detail_start, detail_end)
                time.sleep(0.4)
                _force_hide_ui_noise(page)

                detail_file = f"{i:02d}_{safe_name}_detail.png"
                _take_validated_screenshot(
                    page,
                    output / detail_file,
                    detail_anchor,
                    track_hints=detail_patterns,
                    height_ratio=0.68,
                )

                # Annotate with highlight box + title bar
                evidence = frame.get("evidence_slices", [])
                _annotate_detail(
                    output / detail_file,
                    target_ts, detail_start, detail_end,
                    jank_type, evidence,
                    focus_track=detail_anchor,
                    track_hints=detail_patterns,
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

def _start_trace_processor(trace_path, override_bin=None, size_mb=0):
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

    # Wait for HTTP server to be ready (poll /status). Scale for large traces.
    import socket
    rpc_timeout = _scaled_timeout(size_mb, baseline_sec=60, per_mb_sec=0.15)
    t0 = time.time()
    while time.time() - t0 < rpc_timeout:
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

    print(f"         WARNING: trace_processor RPC not ready after {rpc_timeout}s")
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
    _hide_noise_track_groups(page)


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


def _hide_noise_track_groups(page):
    try:
        page.evaluate(
            """() => {
                const noise = new Set([
                    'cpu scheduling',
                    'cpu frequency',
                    'scheduler',
                    'kernel threads',
                    'android logs',
                    'android app startups',
                    'ftrace events',
                    'power',
                    'memory',
                    'cpu',
                    'system',
                    'gpu',
                    'io',
                    'device state',
                ]);
                const els = document.querySelectorAll(
                    '[class*="track-shell"], [class*="trackShell"], ' +
                    '[class*="track-name"], [class*="trackName"], [class*="pf-track"]'
                );
                for (const el of els) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (!noise.has(text)) continue;
                    let node = el;
                    for (let i = 0; i < 4 && node.parentElement; i += 1) {
                        const parent = node.parentElement;
                        const r = parent.getBoundingClientRect();
                        if (r.height > 10 && r.height < 44 && r.width > 120) {
                            node = parent;
                        } else {
                            break;
                        }
                    }
                    try { node.style.display = 'none'; } catch (e) {}
                }
            }"""
        )
    except Exception:
        pass


def _pick_title_evidence(evidence, focus_track="", track_hints=None):
    focus_tokens = []
    if focus_track:
        focus_tokens.append(str(focus_track).strip().lower())
    for item in track_hints or []:
        text = str(item or "").strip().lower()
        if not text:
            continue
        focus_tokens.append(text)
        parts = text.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit():
            focus_tokens.append(parts[0].strip())

    best = None
    best_score = -10**9
    for idx, ev in enumerate(evidence or []):
        thread_name = str(ev.get("thread", "")).strip().lower()
        slice_name = str(ev.get("name", "")).strip().lower()
        score = -idx
        if _should_include_evidence_thread(ev):
            score += 100
        else:
            score -= 200
        matched_focus = any(token and (token in thread_name or token in slice_name) for token in focus_tokens)
        if matched_focus:
            score += 40
        elif focus_tokens:
            score -= 20
        if any(token in slice_name for token in ("drawframes", "traversal", "dequeuebuffer", "waiting for gpu completion", "draw-vri", "doframe")):
            score += 18
        if "binder transaction" in slice_name:
            score -= 25
        if score > best_score:
            best = ev
            best_score = score
    return best or (evidence[0] if evidence else None)


def _trim_bottom_idle_rows(filepath, scan_start_ratio=0.28, min_active_pixels=20, bottom_margin=3, min_active_streak=3):
    try:
        from PIL import Image
    except Exception:
        return

    try:
        with Image.open(str(filepath)).convert("RGB") as img:
            width, height = img.size
            if width < 200 or height < 200:
                return

            x0 = max(0, min(width - 1, int(width * scan_start_ratio)))
            y_floor = max(0, int(height * 0.45))
            sample_x0 = max(x0, int(width * 0.85))
            sample_y0 = max(0, height - 16)

            bg_samples = []
            for y in range(sample_y0, height):
                for x in range(sample_x0, width):
                    bg_samples.append(img.getpixel((x, y)))
            if not bg_samples:
                return

            bg_r = sum(pixel[0] for pixel in bg_samples) / len(bg_samples)
            bg_g = sum(pixel[1] for pixel in bg_samples) / len(bg_samples)
            bg_b = sum(pixel[2] for pixel in bg_samples) / len(bg_samples)

            def is_active(pixel):
                return (
                    abs(pixel[0] - bg_r) + abs(pixel[1] - bg_g) + abs(pixel[2] - bg_b)
                ) >= 52

            last_active_y = None
            step = 2 if width >= 1200 else 1
            active_streak = 0
            active_block_end = None
            for y in range(height - 1, y_floor - 1, -1):
                active = 0
                for x in range(x0, width, step):
                    if is_active(img.getpixel((x, y))):
                        active += 1
                        if active >= min_active_pixels:
                            break
                if active >= min_active_pixels:
                    if active_streak == 0:
                        active_block_end = y
                    active_streak += 1
                    if active_streak >= min_active_streak:
                        last_active_y = active_block_end
                        break
                else:
                    active_streak = 0
                    active_block_end = None

            if last_active_y is None:
                return

            crop_bottom = min(height, max(last_active_y + bottom_margin, y_floor + 40))
            if crop_bottom >= height - 8:
                return

            trimmed = img.crop((0, 0, width, crop_bottom))
            trimmed.save(str(filepath))
    except Exception:
        return


def _image_has_signal(filepath, scan_start_ratio=0.20, ignore_top_ratio=0.12, min_ratio=0.02):
    try:
        from PIL import Image
    except Exception:
        return True

    try:
        with Image.open(str(filepath)).convert("RGB") as img:
            width, height = img.size
            if width < 200 or height < 200:
                return True

            x0 = max(0, min(width - 1, int(width * scan_start_ratio)))
            y0 = max(0, min(height - 1, int(height * ignore_top_ratio)))
            x_step = max(1, (width - x0) // 36)
            y_step = max(1, (height - y0) // 24)
            bg = img.getpixel((width - 10, height - 10))

            total = 0
            active = 0
            for y in range(y0, height, y_step):
                for x in range(x0, width, x_step):
                    total += 1
                    pixel = img.getpixel((x, y))
                    if sum(abs(pixel[i] - bg[i]) for i in range(3)) >= 48:
                        active += 1
            if total == 0:
                return True
            return (active / total) >= min_ratio
    except Exception:
        return True


def _take_validated_screenshot(page, filepath, anchor_track, track_hints=None, height_ratio=0.72, prefer_focus=True, retry_wait_sec=0):
    strategies = []
    if prefer_focus:
        strategies.append(("focus", lambda: _take_focus_screenshot(
            page, filepath, anchor_track, track_hints=track_hints, height_ratio=height_ratio
        )))
    strategies.append(("clip", lambda: _take_clipped_screenshot(page, filepath, anchor_track)))
    if not prefer_focus:
        strategies.append(("focus", lambda: _take_focus_screenshot(
            page, filepath, anchor_track, track_hints=track_hints, height_ratio=height_ratio
        )))

    rounds = 2 if retry_wait_sec > 0 else 1
    for round_idx in range(rounds):
        if round_idx:
            time.sleep(retry_wait_sec)
            _force_hide_ui_noise(page)
            print(f"         waiting {retry_wait_sec}s and retrying screenshot...")
        for name, fn in strategies:
            fn()
            if _image_has_signal(filepath):
                return
            print(f"         [{name}] captured low-signal image, retrying...")

    page.screenshot(path=str(filepath))
    _trim_bottom_idle_rows(filepath)


def _annotate_detail(filepath, target_ts, vis_start, vis_end, jank_type, evidence, focus_track="", track_hints=None):
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
        half_w = max(int(track_w * 0.008), 6)

        x_left = max(gutter, center_x - half_w)
        x_right = min(w - 10, center_x + half_w)

        bar_h = 40
        rect_top = bar_h + 10
        rect_bottom = h - 10

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        draw.rectangle(
            [x_left, rect_top, x_right, rect_bottom],
            fill=(255, 50, 50, 30),
        )
        draw.line(
            [(center_x, rect_top), (center_x, rect_bottom)],
            fill=(255, 70, 70, 220),
            width=3,
        )

        # Title bar at very top
        top_ev = _pick_title_evidence(evidence or [], focus_track=focus_track, track_hints=track_hints or [])
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
        label = "target_ts"
        label_x = max(gutter, center_x - 60)
        draw.text((label_x, bar_h + 6), label, fill=(255, 80, 80, 200), font=small_font)

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


def _take_focus_screenshot(page, filepath, anchor_track, track_hints=None, height_ratio=0.72):
    try:
        clip = page.evaluate(
            """(payload) => {
                const anchorName = String(payload.anchorName || '').trim().toLowerCase();
                const rawHints = payload.trackHints || [];
                const trackHints = rawHints
                    .map(item => String(item || '').trim().toLowerCase())
                    .filter(Boolean)
                    .slice(0, 24);
                const heightRatio = Number(payload.heightRatio || 0.72);
                const vw = window.innerWidth;
                const vh = window.innerHeight;

                let canvas = null;
                let maxArea = 0;
                document.querySelectorAll('canvas').forEach(c => {
                    const r = c.getBoundingClientRect();
                    const area = r.width * r.height;
                    if (area > maxArea) { maxArea = area; canvas = c; }
                });

                let cx = 0, cw = vw;
                if (canvas) {
                    const cr = canvas.getBoundingClientRect();
                    cx = Math.max(0, Math.floor(cr.x));
                    cw = Math.max(100, Math.floor(cr.width));
                }

                let panelTop = vh;
                const keywords = ['current selection', 'ftrace events', 'nothing selected'];
                document.querySelectorAll('div, section').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.top < vh * 0.45 || r.height < 20) return;
                    const text = (el.textContent || '').toLowerCase().trim();
                    if (text.length > 300) return;
                    for (const kw of keywords) {
                        if (text.includes(kw) && r.top < panelTop) {
                            panelTop = r.top;
                        }
                    }
                });

                let anchorY = Number.POSITIVE_INFINITY;
                const matches = [];
                const seenTextTops = new Map();
                const els = document.querySelectorAll(
                    '[class*="track"] [class*="title"], [class*="track"] [class*="name"],' +
                    '[class*="shell"], [class*="pf-track"]'
                );
                for (const el of els) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (!text || text.length > 180) continue;
                    const r = el.getBoundingClientRect();
                    if (r.height < 8 || r.bottom < 0 || r.top > panelTop) continue;

                    if (anchorName && text.includes(anchorName)) {
                        anchorY = Math.min(anchorY, Math.max(0, r.top));
                    }

                    let matched = false;
                    for (const hint of trackHints) {
                        if (hint && (text.includes(hint) || hint.includes(text))) {
                            matched = true;
                            break;
                        }
                    }
                    if (!matched && anchorName) {
                        matched = text.includes(anchorName);
                    }
                    if (!matched) continue;
                    if (seenTextTops.has(text) && Math.abs(r.top - seenTextTops.get(text)) > 32) {
                        continue;
                    }
                    seenTextTops.set(text, r.top);
                    matches.push({top: r.top, bottom: r.bottom});
                }

                matches.sort((a, b) => a.top - b.top || a.bottom - b.bottom);
                const clusters = [];
                const gapPx = 48;
                for (const item of matches) {
                    const last = clusters[clusters.length - 1];
                    if (last && item.top - last.bottom <= gapPx) {
                        last.bottom = Math.max(last.bottom, item.bottom);
                    } else {
                        clusters.push({top: item.top, bottom: item.bottom});
                    }
                }

                let relevantTop = vh;
                let relevantBottom = 0;
                if (clusters.length) {
                    let chosen = clusters[0];
                    if (anchorY) {
                        let bestScore = Number.POSITIVE_INFINITY;
                        for (const cluster of clusters) {
                            const center = (cluster.top + cluster.bottom) / 2;
                            const score = Math.abs(center - anchorY);
                            if (score < bestScore) {
                                bestScore = score;
                                chosen = cluster;
                            }
                        }
                    }
                    relevantTop = chosen.top;
                    relevantBottom = chosen.bottom;
                }

                if (!Number.isFinite(anchorY)) {
                    anchorY = 0;
                }

                if (!anchorY && relevantBottom > relevantTop) {
                    anchorY = relevantBottom;
                }
                const clipY = Math.max(
                    0,
                    Math.floor((relevantBottom > relevantTop ? relevantTop : Math.max(0, anchorY - vh * 0.18)) - 8)
                );
                let requestedBottom = Math.floor(clipY + vh * heightRatio);
                if (relevantBottom > relevantTop) {
                    requestedBottom = Math.floor(relevantBottom + Math.max(8, vh * 0.012));
                } else if (anchorY) {
                    requestedBottom = Math.max(requestedBottom, Math.floor(anchorY + vh * 0.12));
                }
                const clipBottom = Math.min(vh, panelTop - 4, requestedBottom);
                const clipH = Math.max(220, clipBottom - clipY);
                return {x: cx, y: clipY, width: cw, height: clipH};
            }""",
            {
                "anchorName": anchor_track,
                "trackHints": track_hints or [],
                "heightRatio": height_ratio,
            },
        )
        if clip and clip.get("width", 0) > 100 and clip.get("height", 0) > 100:
            page.screenshot(path=str(filepath), clip=clip)
            _trim_bottom_idle_rows(filepath)
            return
    except Exception as e:
        print(f"         [focus-clip] fallback: {e}")

    _take_clipped_screenshot(page, filepath, anchor_track)


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
            _trim_bottom_idle_rows(filepath)
            return
    except Exception as e:
        print(f"         [clip] fallback: {e}")

    # Fallback: full viewport
    page.screenshot(path=str(filepath))
    _trim_bottom_idle_rows(filepath)


# ─── File I/O ─────────────────────────────────────────────────────────

def _load(path):
    return json.loads(Path(path).read_text())

def _write(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
