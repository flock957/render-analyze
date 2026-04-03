#!/usr/bin/env python3
"""Prepare precise screenshot targets by querying trace_processor.

Runs SQL queries against trace_processor to find exact:
- Slice timestamps and durations for each jank frame
- Thread states during jank (Running/Sleeping/Blocked)
- Related events (binder calls, GC, lock contention)
- The "interesting" time window where activity is densest

Outputs screenshot_targets.json for the screenshot script to use.

Must be run AFTER analysis (Phase 5-6) and BEFORE screenshots (Phase 7).
trace_processor must be running.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from tp_query import query_tp, parse_columns

OUTPUT_DIR = os.environ.get("RENDER_OUTPUT", "/workspace/render_output")


def _query(port, sql):
    """Query trace_processor and return parsed rows."""
    try:
        return parse_columns(query_tp(port, sql))
    except Exception:
        return []


def find_jank_context(port, issue_name, ts, dur, jank_category):
    """Query trace_processor for detailed context of a jank frame.

    Finds:
    1. Key slices during jank (doFrame, composite, presentFence etc.)
    2. Thread states (Running/Runnable/Sleeping/Blocked) with durations
    3. Blocking chain: which thread blocked which, with priorities
    4. Related events (binder, GC, lock)
    5. Optimal zoom range based on actual problem duration
    """
    ts_end = ts + dur
    context = {
        "issue_name": issue_name,
        "jank_category": jank_category,
        "ts": ts,
        "dur": dur,
        "dur_ms": dur / 1e6,
        "description": "",
        "interesting_start": ts,
        "interesting_dur": dur,
        "slices": [],
        "thread_states": [],
        "related_events": [],
        "tracks_to_show": [],
    }

    # 1. Find the main slice(s) during this jank period
    if jank_category in ("app_deadline", "buffer_stuffing", "dropped"):
        # App-side: look for Choreographer#doFrame
        slices = _query(port, f"""
            SELECT s.ts, s.dur, s.name, t.name AS thread_name, t.tid, p.name AS proc_name
            FROM slice s
            JOIN thread_track tt ON s.track_id = tt.id
            JOIN thread t ON tt.utid = t.utid
            JOIN process p ON t.upid = p.upid
            WHERE s.ts >= {ts - 5000000} AND s.ts <= {ts_end + 5000000}
            AND s.name IN ('Choreographer#doFrame', 'DrawFrame', 'queueBuffer',
                           'dequeueBuffer', 'syncFrameState', 'draw',
                           'performTraversals', 'measure', 'layout')
            ORDER BY s.dur DESC LIMIT 10
        """)
        context["slices"] = slices

        if slices:
            # The "interesting" window is around the longest slice
            longest = slices[0]
            context["interesting_start"] = int(longest.get("ts", ts)) - 2_000_000
            context["interesting_dur"] = int(longest.get("dur", dur)) + 4_000_000
            context["tracks_to_show"].append(longest.get("proc_name", ""))
            context["tracks_to_show"].append("RenderThread")

            # Build description
            parts = []
            for s in slices[:3]:
                parts.append(f"{s['name']} ({s.get('dur', 0)/1e6:.1f}ms) on {s.get('thread_name', '?')}")
            context["description"] = "App帧超时: " + "; ".join(parts)

    elif jank_category in ("display_hal", "sf_stuffing", "prediction_error"):
        # SF-side: look for presentFence, commit, composite
        slices = _query(port, f"""
            SELECT s.ts, s.dur, s.name, t.name AS thread_name, t.tid
            FROM slice s
            JOIN thread_track tt ON s.track_id = tt.id
            JOIN thread t ON tt.utid = t.utid
            WHERE s.ts >= {ts - 10000000} AND s.ts <= {ts_end + 10000000}
            AND (s.name LIKE 'waiting for presentFence%'
                 OR s.name LIKE 'commit%'
                 OR s.name LIKE 'composite%'
                 OR s.name = 'onMessageRefresh')
            ORDER BY s.dur DESC LIMIT 10
        """)
        context["slices"] = slices
        context["tracks_to_show"].append("surfaceflinger")

        if slices:
            longest = slices[0]
            context["interesting_start"] = int(longest.get("ts", ts)) - 2_000_000
            context["interesting_dur"] = int(longest.get("dur", dur)) + 4_000_000

            parts = []
            for s in slices[:3]:
                parts.append(f"{s['name'][:40]} ({s.get('dur', 0)/1e6:.1f}ms)")
            if jank_category == "display_hal":
                context["description"] = "DisplayHAL延迟: " + "; ".join(parts)
            elif jank_category == "sf_stuffing":
                context["description"] = "SF帧堆积: " + "; ".join(parts)
            else:
                context["description"] = "VSync预测错误: " + "; ".join(parts)

    elif jank_category in ("sf_cpu", "sf_gpu"):
        slices = _query(port, f"""
            SELECT s.ts, s.dur, s.name, t.name AS thread_name, t.tid
            FROM slice s
            JOIN thread_track tt ON s.track_id = tt.id
            JOIN thread t ON tt.utid = t.utid
            WHERE s.ts >= {ts - 10000000} AND s.ts <= {ts_end + 10000000}
            AND (s.name LIKE 'onMessageRefresh%' OR s.name LIKE 'composite%'
                 OR s.name LIKE 'RenderEngine%' OR s.name LIKE 'GLES%'
                 OR s.name LIKE 'commit%')
            ORDER BY s.dur DESC LIMIT 10
        """)
        context["slices"] = slices
        context["tracks_to_show"].append("surfaceflinger")

        if slices:
            longest = slices[0]
            context["interesting_start"] = int(longest.get("ts", ts)) - 2_000_000
            context["interesting_dur"] = int(longest.get("dur", dur)) + 4_000_000

            label = "SF CPU超时" if jank_category == "sf_cpu" else "SF GPU超时"
            parts = [f"{s['name'][:40]} ({s.get('dur',0)/1e6:.1f}ms)" for s in slices[:3]]
            context["description"] = f"{label}: " + "; ".join(parts)

    # 2. Find thread states during jank (for all types)
    thread_states = _query(port, f"""
        SELECT ts.state, SUM(ts.dur) / 1000000.0 AS total_ms, t.name AS thread_name
        FROM thread_state ts
        JOIN thread t ON ts.utid = t.utid
        WHERE ts.ts >= {ts} AND ts.ts + ts.dur <= {ts_end}
        AND t.name IN ('surfaceflinger', 'RenderThread', 'main')
        GROUP BY ts.state, t.name
        ORDER BY total_ms DESC LIMIT 20
    """)
    context["thread_states"] = thread_states

    # Add thread state info to description
    blocked_states = [s for s in thread_states
                      if s.get("state") in ("D", "S") and s.get("total_ms", 0) > 1]
    if blocked_states:
        state_desc = "; ".join(
            f"{s['thread_name']} {s['state']}={s['total_ms']:.1f}ms"
            for s in blocked_states[:3]
        )
        context["description"] += f" | 线程状态: {state_desc}"

    # 3. Find related events (binder, GC, lock)
    related = _query(port, f"""
        SELECT s.ts, s.dur, s.name, t.name AS thread_name
        FROM slice s
        JOIN thread_track tt ON s.track_id = tt.id
        JOIN thread t ON tt.utid = t.utid
        WHERE s.ts >= {ts} AND s.ts <= {ts_end}
        AND (s.name LIKE 'binder%' OR s.name LIKE 'GC%'
             OR s.name LIKE 'lock%' OR s.name LIKE 'monitor%'
             OR s.name LIKE 'Compiling%' OR s.name LIKE 'JIT%')
        AND s.dur > 1000000
        ORDER BY s.dur DESC LIMIT 5
    """)
    context["related_events"] = related
    if related:
        rel_desc = "; ".join(f"{r['name'][:30]} ({r.get('dur',0)/1e6:.1f}ms)" for r in related[:2])
        context["description"] += f" | 关联: {rel_desc}"

    # 4. Find blocking chain: threads that were Runnable/Blocked during jank
    # This identifies the critical path (which thread blocked which)
    blocking_chain = _query(port, f"""
        SELECT ts.state, ts.dur / 1000000.0 AS dur_ms,
               t.name AS thread_name, t.tid,
               ts.dur AS dur_ns
        FROM thread_state ts
        JOIN thread t ON ts.utid = t.utid
        JOIN process p ON t.upid = p.upid
        WHERE ts.ts >= {ts - 5000000} AND ts.ts + ts.dur <= {ts_end + 5000000}
        AND ts.state IN ('R', 'R+', 'D', 'DK')
        AND ts.dur > 3000000
        AND (p.name LIKE '%surfaceflinger%' OR p.name LIKE '%composer%'
             OR p.name LIKE '%aweme%' OR p.name LIKE '%RenderEngine%')
        ORDER BY ts.dur DESC LIMIT 10
    """)
    context["blocking_chain"] = blocking_chain

    # Add blocking threads to tracks_to_show
    for b in blocking_chain[:3]:
        tname = b.get("thread_name", "")
        if tname and tname not in context["tracks_to_show"]:
            context["tracks_to_show"].append(tname)

    # Add blocking info to description
    if blocking_chain:
        block_desc = "; ".join(
            f"{b['thread_name']} {b['state']}={b['dur_ms']:.1f}ms"
            for b in blocking_chain[:3]
        )
        context["description"] += f" | 阻塞链: {block_desc}"

    # Fallback description
    if not context["description"]:
        context["description"] = f"{jank_category} jank: {dur/1e6:.1f}ms"

    # 5. Dynamic zoom: use actual problem duration, not fixed range
    # Short janks (< 50ms): show 50-80ms for context
    # Medium janks (50-200ms): show the full jank + 20% padding
    # Long janks (> 200ms): show 200ms centered on the densest activity
    if dur < 50_000_000:
        context["interesting_dur"] = max(context["interesting_dur"], 50_000_000)
        context["interesting_dur"] = min(context["interesting_dur"], 80_000_000)
    elif dur < 200_000_000:
        context["interesting_dur"] = int(dur * 1.2)
        context["interesting_start"] = int(ts - dur * 0.1)
    else:
        # For very long janks, find the densest 200ms window
        context["interesting_dur"] = 200_000_000
        if context["slices"]:
            # Center on the first slice cluster
            first_slice_ts = int(context["slices"][0].get("ts", ts))
            context["interesting_start"] = first_slice_ts - 20_000_000
        else:
            context["interesting_start"] = ts

    return context


def main():
    parser = argparse.ArgumentParser(description="Prepare screenshot targets from trace data")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    # Read analysis results
    app_jank = {}
    sf_jank = {}
    try:
        app_jank = json.loads(open(os.path.join(output_dir, "app_jank.json")).read())
    except Exception:
        pass
    try:
        sf_jank = json.loads(open(os.path.join(output_dir, "sf_jank.json")).read())
    except Exception:
        pass

    # Collect all issue regions
    all_regions = []
    for src in [app_jank, sf_jank]:
        for region in src.get("issue_regions", []):
            ts = int(region.get("ts", 0))
            dur = int(region.get("dur", 0))
            if ts > 0 and dur > 0:
                all_regions.append(region)

    if not all_regions:
        print("No issue regions found")
        result = {"targets": [], "status": "no_issues"}
        with open(os.path.join(output_dir, "screenshot_targets.json"), "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(json.dumps(result))
        return

    # Classify and sort
    from capture_trace_screenshot import _classify_jank_category, select_top_issues, IssueRegion
    issues = []
    for r in all_regions:
        name = r.get("name", "")
        cat = _classify_jank_category(name, "")
        issues.append(IssueRegion(
            name=name, description=r.get("desc", ""),
            start_ns=int(r["ts"]), end_ns=int(r["ts"]) + int(r["dur"]),
            severity=r.get("severity", "medium"),
            source_file="", jank_category=cat,
        ))

    selected = select_top_issues(issues, args.top_n)

    # Query trace_processor for context per issue
    targets = []
    for issue in selected:
        print(f"  Querying context for: {issue.name} ({issue.jank_category})")
        ctx = find_jank_context(
            args.port, issue.name, issue.start_ns,
            issue.end_ns - issue.start_ns, issue.jank_category
        )
        targets.append(ctx)

    result = {"targets": targets, "status": "ok", "count": len(targets)}
    out_path = os.path.join(output_dir, "screenshot_targets.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nPrepared {len(targets)} screenshot targets -> {out_path}")
    for t in targets:
        print(f"  [{t['jank_category']}] {t['issue_name']}: {t['description'][:80]}")

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
