#!/usr/bin/env python3
"""Phase 2: Analyze jank frames from a Perfetto trace using SQL.

Outputs: target_process.json, app_jank.json, sf_jank.json, jank_types.json,
         thread_map.json, tp_state.json
"""
import argparse
import json
import sys
from pathlib import Path

KEYWORDS_BY_JANK_TYPE = {
    "App Deadline Missed": [
        "doFrame", "performTraversals", "DrawFrame", "DrawFrames",
        "RenderThread", "queueBuffer",
        # 渲染管线关键 slice
        "renderFrameImpl", "flush commands", "Waiting for GPU",
        "syncFrameState", "nSyncAndDrawFrame", "eglSwapBuffers",
        "measure", "layout", "draw",
        # 常见阻塞源
        "Binder", "GC", "JIT",
    ],
    "Buffer Stuffing": [
        "dequeueBuffer", "queueBuffer", "acquireBuffer", "latchBuffer",
        "DrawFrames", "renderFrameImpl", "flush commands", "Waiting for GPU",
    ],
    "SurfaceFlinger CPU Deadline Missed": [
        "onMessageRefresh", "commit", "composite", "RenderEngine",
        "handleTransaction", "handleComposition", "postComposition",
    ],
    "Display HAL": [
        "presentFence", "presentDisplay", "composer", "hwc",
        "crtc_commit", "waiting for presentFence",
    ],
    "Prediction Error": ["Expected Timeline", "Actual Timeline", "VSync"],
    "SurfaceFlinger Scheduling": ["surfaceflinger", "onMessageRefresh", "sched"],
}

FOCUS_TRACK_BY_JANK_TYPE = {
    "App Deadline Missed": "RenderThread",
    "Buffer Stuffing": "dequeueBuffer",
    "SurfaceFlinger CPU Deadline Missed": "surfaceflinger",
    "Display HAL": "presentFence",
    "Prediction Error": "Actual Timeline",
    "SurfaceFlinger Scheduling": "surfaceflinger",
}


def main():
    parser = argparse.ArgumentParser(description="Analyze jank frames in a Perfetto trace")
    parser.add_argument("--trace", required=True, help="Path to .perfetto-trace file")
    parser.add_argument("--output-dir", required=True, help="Output directory for JSON results")
    args = parser.parse_args()

    trace_path = Path(args.trace)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    if not trace_path.exists():
        print(f"[analyze] ERROR: Trace not found: {trace_path}")
        sys.exit(1)

    print(f"[Phase 1] Analyzing jank: {trace_path.name} ({trace_path.stat().st_size // 1024 // 1024}MB)")

    try:
        from perfetto.trace_processor import TraceProcessor
    except ImportError:
        print("[analyze] ERROR: perfetto module not found. Install: pip install perfetto")
        sys.exit(1)

    tp = TraceProcessor(file_path=str(trace_path))

    # --- Step 1: Trace time range ---
    print("  [1.1] Reading trace time range...")
    q = tp.query("SELECT MIN(ts) as start_ts, MAX(ts+dur) as end_ts FROM sched")
    for r in q:
        trace_start, trace_end = r.start_ts, r.end_ts
    trace_dur_ms = (trace_end - trace_start) / 1e6
    print(f"        Duration: {trace_dur_ms / 1000:.1f}s")

    tp_state = {
        "trace_file": str(trace_path),
        "trace_start": trace_start,
        "trace_end": trace_end,
        "trace_duration_ms": trace_dur_ms,
    }
    _write(output / "tp_state.json", tp_state)

    # --- Step 2: Find target process ---
    print("  [1.2] Finding target process...")
    q = tp.query("""
        SELECT p.name, p.pid, SUM(dur)/1e6 as total_ms
        FROM sched s JOIN thread t ON s.utid=t.utid JOIN process p ON t.upid=p.upid
        WHERE p.name IS NOT NULL AND p.name != ''
          AND p.name NOT IN ('ps', 'grep', 'head', 'cat', 'sh', 'logcat')
          AND p.name NOT LIKE '/vendor/%' AND p.name NOT LIKE '/system/%'
          AND p.name NOT LIKE 'kworker%'
        GROUP BY p.pid ORDER BY total_ms DESC LIMIT 10
    """)
    candidates = [{"process_name": r.name, "pid": r.pid, "total_running_ms": r.total_ms} for r in q]
    target = candidates[0] if candidates else {"process_name": "unknown", "pid": 0, "total_running_ms": 0}
    print(f"        Target: {target['process_name']} (pid={target['pid']}, {target['total_running_ms']:.0f}ms)")

    _write(output / "target_process.json", {
        "method": "running_time",
        "process_name": target["process_name"],
        "pid": target["pid"],
        "total_running_ms": target["total_running_ms"],
        "candidates": candidates,
    })

    # --- Step 3: Jank frame analysis ---
    print("  [1.3] Analyzing jank frames...")
    q_total = tp.query("SELECT COUNT(*) as n FROM actual_frame_timeline_slice")
    total_frames = next(iter(q_total)).n

    q_jank_all = tp.query("""
        SELECT aft.id, aft.ts, aft.dur, aft.jank_type,
               p.name as process_name, p.pid
        FROM actual_frame_timeline_slice aft
        LEFT JOIN process_track pt ON aft.track_id = pt.id
        LEFT JOIN process p ON pt.upid = p.upid
        WHERE aft.jank_type != 'None'
        ORDER BY aft.dur DESC
        LIMIT 200
    """)
    all_janks = [{
        "id": r.id, "ts": r.ts, "dur": r.dur,
        "actual_dur_ms": r.dur / 1e6,
        "jank_type": r.jank_type,
        "process_name": r.process_name, "pid": r.pid,
    } for r in q_jank_all]

    jank_count = len(all_janks)
    jank_rate = jank_count / total_frames if total_frames > 0 else 0
    print(f"        Frames: {total_frames} total, {jank_count} jank ({jank_rate*100:.1f}%)")

    # Top frame per jank type (diverse)
    by_type = {}
    for j in all_janks:
        jt = j["jank_type"]
        if jt not in by_type or j["dur"] > by_type[jt]["dur"]:
            by_type[jt] = j
    top_frames = sorted(by_type.values(), key=lambda x: -x["dur"])[:5]
    for frame in top_frames:
        _enrich_top_frame(tp, frame, trace_start, trace_end)

    # Per-type statistics: count, avg duration, top 3 frames
    type_stats = {}
    for j in all_janks:
        jt = j["jank_type"]
        if jt not in type_stats:
            type_stats[jt] = {"count": 0, "total_dur": 0, "frames": []}
        type_stats[jt]["count"] += 1
        type_stats[jt]["total_dur"] += j["actual_dur_ms"]
        type_stats[jt]["frames"].append(j)

    type_summary = {}
    type_details = {}
    for jt, st in type_stats.items():
        type_summary[jt] = st["count"]
        top3 = sorted(st["frames"], key=lambda x: -x["dur"])[:3]
        type_details[jt] = {
            "count": st["count"],
            "avg_dur_ms": round(st["total_dur"] / st["count"], 1) if st["count"] else 0,
            "max_dur_ms": round(top3[0]["actual_dur_ms"], 1) if top3 else 0,
            "top_frames": top3,
        }

    print(f"        Jank types: {len(by_type)}, top-5 selected for screenshots")
    for i, f in enumerate(top_frames):
        print(f"          {i}: [{f['jank_type']}] {f['actual_dur_ms']:.1f}ms")

    _write(output / "app_jank.json", {
        "has_issue": jank_count > 0,
        "severity": "high" if jank_rate > 0.1 else "medium" if jank_rate > 0.05 else "low",
        "total_frames": total_frames,
        "jank_frames": jank_count,
        "jank_rate": jank_rate,
        "top_frames": top_frames,
        "jank_type_summary": type_summary,
        "jank_type_details": type_details,
    })

    sf_janks = [j for j in all_janks if "SurfaceFlinger" in j["jank_type"]]
    _write(output / "sf_jank.json", {
        "has_issue": len(sf_janks) > 0,
        "severity": "medium" if sf_janks else "normal",
        "sf_jank_frames": len(sf_janks),
        "top_frames": sorted(sf_janks, key=lambda x: -x["dur"])[:5],
    })

    _write(output / "jank_types.json", {
        "has_issue": True,
        "types": type_summary,
        "top_frames": top_frames,
    })

    # --- Step 4: Thread mapping for screenshot pinning ---
    print("  [1.4] Building thread map for screenshot pinning...")
    target_pid = target["pid"]

    # Target app main thread (same tid as pid, pick the one with a real name)
    q_app_main = tp.query(f"""
        SELECT t.name, t.tid FROM thread t
        JOIN process p ON t.upid = p.upid
        WHERE p.pid = {target_pid} AND t.tid = {target_pid}
          AND t.name IS NOT NULL AND t.name != 'None'
        LIMIT 1
    """)
    app_main = [{"name": r.name, "tid": r.tid} for r in q_app_main]

    # App's RenderThread
    q_app_render = tp.query(f"""
        SELECT t.name, t.tid FROM thread t
        JOIN process p ON t.upid = p.upid
        WHERE p.pid = {target_pid} AND t.name LIKE '%RenderThread%'
        ORDER BY t.tid LIMIT 3
    """)
    app_render = [{"name": r.name, "tid": r.tid} for r in q_app_render]

    # surfaceflinger main thread (tid = pid of sf process)
    q_sf = tp.query("""
        SELECT t.name, t.tid, p.pid
        FROM thread t JOIN process p ON t.upid = p.upid
        WHERE p.name = 'surfaceflinger' OR (t.name = 'surfaceflinger' AND t.tid = p.pid)
        ORDER BY t.tid
    """)
    sf_all = [{"name": r.name, "tid": r.tid, "pid": r.pid} for r in q_sf]
    sf_main_tid = None
    sf_pid = None
    for s in sf_all:
        if s["tid"] == s["pid"]:
            sf_main_tid = s["tid"]
            sf_pid = s["pid"]
            break
    if not sf_main_tid and sf_all:
        sf_main_tid = sf_all[0]["tid"]
        sf_pid = sf_all[0].get("pid")

    # SF RenderEngine thread
    q_sf_re = tp.query(f"""
        SELECT t.name, t.tid FROM thread t
        JOIN process p ON t.upid = p.upid
        WHERE p.pid = {sf_pid or 0} AND t.name = 'RenderEngine'
        LIMIT 1
    """)
    sf_render_engine = [{"name": r.name, "tid": r.tid} for r in q_sf_re]

    # SF GPU completion thread
    q_sf_gpu = tp.query(f"""
        SELECT t.name, t.tid FROM thread t
        JOIN process p ON t.upid = p.upid
        WHERE p.pid = {sf_pid or 0} AND t.name = 'GPU completion'
        LIMIT 1
    """)
    sf_gpu = [{"name": r.name, "tid": r.tid} for r in q_sf_gpu]

    # SF binder threads (most active ones)
    q_sf_binder = tp.query(f"""
        SELECT t.name, t.tid, SUM(s.dur)/1e6 as total_ms
        FROM sched s JOIN thread t ON s.utid = t.utid
        JOIN process p ON t.upid = p.upid
        WHERE p.pid = {sf_pid or 0}
          AND (t.name LIKE 'binder:%' OR t.name LIKE 'HwBinder:%')
        GROUP BY t.tid ORDER BY total_ms DESC LIMIT 2
    """)
    sf_binder = [{"name": r.name, "tid": r.tid} for r in q_sf_binder]

    # HWC/Composer service threads
    q_hwc = tp.query("""
        SELECT t.name, t.tid, p.pid, p.name as pname
        FROM thread t LEFT JOIN process p ON t.upid = p.upid
        WHERE t.name LIKE '%composer%' OR t.name LIKE '%HWC%'
        ORDER BY t.tid LIMIT 5
    """)
    hwc_threads = [{"name": r.name, "tid": r.tid} for r in q_hwc]

    # CrtcCommit / display kernel threads
    q_crtc = tp.query("""
        SELECT t.name, t.tid FROM thread t
        WHERE t.name LIKE '%crtc_commit%' OR t.name LIKE '%crtc_event%'
        ORDER BY t.tid LIMIT 3
    """)
    crtc_threads = [{"name": r.name, "tid": r.tid} for r in q_crtc]

    # Build pin patterns for the full rendering pipeline
    pin_patterns = _build_pin_patterns(
        target, app_main, app_render, sf_main_tid, sf_pid,
        sf_render_engine, sf_gpu, sf_binder, hwc_threads, crtc_threads
    )

    thread_map = {
        "target_process": target["process_name"],
        "target_pid": target_pid,
        "app_main_thread": app_main,
        "app_render_threads": app_render,
        "sf_main_tid": sf_main_tid,
        "sf_pid": sf_pid,
        "sf_render_engine": sf_render_engine,
        "sf_gpu_completion": sf_gpu,
        "sf_binder_threads": sf_binder,
        "hwc_threads": hwc_threads,
        "crtc_threads": crtc_threads,
        "pin_patterns": pin_patterns,
    }
    _write(output / "thread_map.json", thread_map)

    has_render = len(app_render) > 0
    print(f"        App main: tid={app_main[0]['tid'] if app_main else 'N/A'}")
    print(f"        App RenderThread: {'tid=' + str(app_render[0]['tid']) if has_render else 'NOT FOUND'}")
    print(f"        SF main: tid={sf_main_tid} (pid={sf_pid})")
    print(f"        SF RenderEngine: {'tid=' + str(sf_render_engine[0]['tid']) if sf_render_engine else 'N/A'}")
    print(f"        SF binder: {[t['tid'] for t in sf_binder]}")
    print(f"        HWC: {[t['name'] for t in hwc_threads]}")
    print(f"        CrtcCommit: {[t['name'] for t in crtc_threads]}")
    print(f"        Pin patterns ({len(pin_patterns)}): {pin_patterns}")

    tp.close()
    print(f"\n[Phase 1] Complete -> {output}/")


def _enrich_top_frame(tp, frame, trace_start, trace_end):
    ts = int(frame["ts"])
    dur = int(frame["dur"])
    jank_type = frame["jank_type"]

    around = max(int(dur * 2), 200_000_000)
    region_start = max(int(trace_start), ts - around)
    region_end = min(int(trace_end), ts + dur + around)

    focus_track = _pick_focus_track(jank_type)
    keywords = _pick_keywords(jank_type)

    target_ts = _find_target_ts(tp, ts, dur, keywords)
    evidence = _collect_evidence(tp, region_start, region_end, keywords)

    frame["target_ts"] = int(target_ts)
    frame["focus_track"] = focus_track
    frame["evidence_slices"] = evidence
    frame["keywords_hit"] = sorted(
        {
            kw
            for ev in evidence
            for kw in keywords
            if kw.lower() in (ev.get("name") or "").lower()
        }
    )
    frame["region_range"] = {
        "start_ts": int(region_start),
        "end_ts": int(region_end),
        "window_ms": round((region_end - region_start) / 1e6, 1),
    }
    frame["problem_description"] = _build_problem_description(frame)
    frame["screenshot_reasoning"] = _build_screenshot_reasoning(frame)


def _pick_focus_track(jank_type):
    for key, focus in FOCUS_TRACK_BY_JANK_TYPE.items():
        if key in jank_type:
            return focus
    return "Actual Timeline"


def _pick_keywords(jank_type):
    for key, words in KEYWORDS_BY_JANK_TYPE.items():
        if key in jank_type:
            return words
    return ["doFrame", "RenderThread", "surfaceflinger", "presentFence"]


def _find_target_ts(tp, frame_ts, frame_dur, keywords):
    frame_end = frame_ts + frame_dur
    where_kw = _sql_keyword_where("s.name", keywords)
    q = tp.query(f"""
        SELECT s.ts, s.dur, s.name
        FROM slice s
        WHERE s.ts >= {frame_ts}
          AND s.ts <= {frame_end}
          AND s.dur > 0
          AND ({where_kw})
        ORDER BY s.dur DESC
        LIMIT 1
    """)
    rows = list(q)
    if rows:
        return int(rows[0].ts)
    return int(frame_ts)


def _collect_evidence(tp, start_ts, end_ts, keywords):
    where_kw = _sql_keyword_where("s.name", keywords)
    q = tp.query(f"""
        SELECT
            s.name as slice_name,
            s.ts as ts,
            s.dur as dur,
            t.name as thread_name,
            t.tid as tid
        FROM slice s
        LEFT JOIN thread_track tt ON s.track_id = tt.id
        LEFT JOIN thread t ON tt.utid = t.utid
        WHERE s.ts >= {start_ts}
          AND s.ts <= {end_ts}
          AND s.dur > 0
          AND ({where_kw})
        ORDER BY s.dur DESC
        LIMIT 8
    """)
    out = []
    for r in q:
        out.append(
            {
                "name": r.slice_name,
                "ts": int(r.ts),
                "dur_ns": int(r.dur),
                "dur_ms": round(r.dur / 1e6, 3),
                "thread": r.thread_name or "unknown",
                "tid": r.tid,
            }
        )
    return out


def _sql_keyword_where(field, keywords):
    parts = []
    for kw in keywords:
        safe = kw.replace("'", "''")
        parts.append(f"{field} LIKE '%{safe}%'")
    return " OR ".join(parts) if parts else "1=1"


def _build_problem_description(frame):
    evidence = frame.get("evidence_slices", [])
    first = evidence[0] if evidence else None
    ev_text = (
        f"关键证据为 {first['name']}@{first['thread']}，耗时 {first['dur_ms']}ms"
        if first
        else "未命中明确证据 slice，按帧内主时段定位"
    )
    rr = frame.get("region_range", {})
    return (
        f"问题类型为 {frame['jank_type']}，对应帧 #{frame['id']}，"
        f"目标时刻 target_ts={frame.get('target_ts')}ns，"
        f"检索窗口 {rr.get('window_ms', 0)}ms，"
        f"命中关键词 {', '.join(frame.get('keywords_hit', [])) or '无'}。"
        f"{ev_text}。"
    )


def _build_screenshot_reasoning(frame):
    focus = frame.get("focus_track", "Actual Timeline")
    rr = frame.get("region_range", {})
    return (
        f"全局图用于展示完整时间窗中的帧分布与上下文，"
        f"细节图围绕 target_ts={frame.get('target_ts')}ns 收敛，"
        f"并优先聚焦轨道 {focus}。"
        f"该轨道在 {rr.get('window_ms', 0)}ms 区间内包含问题关键证据，"
        f"可直接观察故障点前后的时序与阻塞来源。"
    )


def _build_pin_patterns(target, app_main, app_render, sf_main_tid, sf_pid,
                        sf_render_engine, sf_gpu, sf_binder, hwc_threads, crtc_threads):
    """Build pin patterns covering the full rendering pipeline.

    Order matters — Perfetto pins from top to bottom in pin order.
    We want: Timeline → App threads → SF threads → HWC → CrtcCommit
    """
    patterns = []

    # 1. Expected/Actual Timeline (frame jank indicators)
    #    Pin for both SF and target app process
    patterns.append("Expected Timeline")
    patterns.append("Actual Timeline")

    # 2. App main thread (UI thread)
    if app_main:
        patterns.append(f"{app_main[0]['name']} {app_main[0]['tid']}")

    # 3. App RenderThread
    if app_render:
        patterns.append(f"RenderThread {app_render[0]['tid']}")

    # 4. SF main thread
    if sf_main_tid:
        patterns.append(f"surfaceflinger {sf_main_tid}")

    # 5. SF RenderEngine
    if sf_render_engine:
        patterns.append(f"RenderEngine {sf_render_engine[0]['tid']}")

    # 6. SF GPU completion
    if sf_gpu:
        patterns.append(f"GPU completion {sf_gpu[0]['tid']}")

    # 7. SF binder (top 1 most active)
    if sf_binder:
        patterns.append(f"{sf_binder[0]['name']}")

    # 8. HWC/Composer
    for t in hwc_threads[:1]:
        patterns.append(f"{t['name']}")

    # 9. CrtcCommit
    for t in crtc_threads[:1]:
        patterns.append(f"{t['name']}")

    return patterns


def _write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
