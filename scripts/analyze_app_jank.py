#!/usr/bin/env python3
"""Analyze application-layer Jank: APP_DEADLINE_MISSED and BUFFER_STUFFING."""
import argparse, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from tp_query import query_tp, parse_columns, save_result

OUTPUT_DIR = os.environ.get("RENDER_OUTPUT", "/workspace/render_output")

# --- SQL Queries ---

SQL_APP_DEADLINE = """
SELECT
    id,
    dur / 1000000.0 AS actual_dur_ms,
    ts,
    dur,
    jank_type
FROM actual_frame_timeline_slice
WHERE jank_type LIKE '%App Deadline Missed%'
ORDER BY dur DESC
LIMIT 50;
"""

SQL_DOFRAME = """
SELECT
    s.ts, s.dur,
    s.dur / 1000000.0 AS do_frame_ms
FROM slice s
WHERE s.name GLOB '*Choreographer#doFrame*'
    AND s.dur > 16000000
ORDER BY s.dur DESC
LIMIT 20;
"""

SQL_DRAW_FRAMES = """
SELECT
    name,
    ts, dur,
    dur / 1000000.0 AS draw_ms
FROM slice
WHERE name GLOB '*DrawFrame*'
    AND dur > 16000000
ORDER BY dur DESC
LIMIT 20;
"""

SQL_GPU_WAIT = """
SELECT
    name,
    ts, dur,
    dur / 1000000.0 AS gpu_wait_ms
FROM slice
WHERE name GLOB '*GPU*wait*'
ORDER BY dur DESC
LIMIT 20;
"""

SQL_BUFFER_STUFFING = """
SELECT
    id,
    jank_type,
    ts, dur,
    dur / 1000000.0 AS dur_ms
FROM actual_frame_timeline_slice
WHERE jank_type LIKE '%Buffer Stuffing%'
ORDER BY dur DESC
LIMIT 50;
"""

SQL_DEQUEUE_BUFFER = """
SELECT
    name,
    ts, dur,
    dur / 1000000.0 AS dur_ms
FROM slice
WHERE name GLOB '*dequeueBuffer*'
    AND dur > 5000000
ORDER BY dur DESC
LIMIT 20;
"""

SQL_BUFFER_QUEUE = """
SELECT
    id,
    COUNT(*) AS buffer_count
FROM slice
WHERE name GLOB '*queueBuffer*'
GROUP BY id
HAVING buffer_count > 3
ORDER BY buffer_count DESC
LIMIT 20;
"""


def analyze(port: int, jank_types: list[str]):
    results = {
        "has_issue": False,
        "severity": "normal",
        "app_deadline_missed": None,
        "buffer_stuffing": None,
        "issue_regions": [],
    }

    # Match: support compound types like "App Deadline Missed, Buffer Stuffing"
    def _match(target: str) -> bool:
        return any(target in jt or jt in target for jt in jank_types)

    # --- 3.1 APP_DEADLINE_MISSED ---
    if _match("App Deadline Missed"):
        frames = parse_columns(query_tp(port, SQL_APP_DEADLINE))
        doframe = parse_columns(query_tp(port, SQL_DOFRAME))
        draw = parse_columns(query_tp(port, SQL_DRAW_FRAMES))
        gpu_wait = parse_columns(query_tp(port, SQL_GPU_WAIT))

        results["app_deadline_missed"] = {
            "jank_frames": len(frames),
            "top_frames": frames[:10],
            "doframe_over_16ms": len(doframe),
            "draw_over_16ms": len(draw),
            "gpu_wait_events": len(gpu_wait),
            "top_doframe": doframe[:5],
            "top_draw": draw[:5],
            "top_gpu_wait": gpu_wait[:5],
        }
        results["has_issue"] = True
        results["severity"] = "high"

        # Generate issue_regions from top jank frames
        for f in frames[:5]:
            ts = int(f.get("ts", 0))
            dur = int(f.get("dur", 0))
            if ts > 0 and dur > 0:
                results["issue_regions"].append({
                    "name": f"App Jank Frame #{f.get('id', '?')}",
                    "ts": ts,
                    "dur": dur,
                    "desc": f"应用侧超时 {f.get('actual_dur_ms', 0):.1f}ms",
                    "severity": "high",
                })

    # --- 3.2 BUFFER_STUFFING ---
    if _match("Buffer Stuffing"):
        frames = parse_columns(query_tp(port, SQL_BUFFER_STUFFING))
        dequeue = parse_columns(query_tp(port, SQL_DEQUEUE_BUFFER))
        queue = parse_columns(query_tp(port, SQL_BUFFER_QUEUE))

        results["buffer_stuffing"] = {
            "jank_frames": len(frames),
            "top_frames": frames[:10],
            "dequeue_blocked": len(dequeue),
            "queue_overflow": len(queue),
            "top_dequeue": dequeue[:5],
        }
        if not results["has_issue"]:
            results["has_issue"] = True
            results["severity"] = "medium"

        for f in frames[:3]:
            ts = int(f.get("ts", 0))
            dur = int(f.get("dur", 0))
            if ts > 0 and dur > 0:
                results["issue_regions"].append({
                    "name": f"Buffer Stuffing #{f.get('id', '?')}",
                    "ts": ts,
                    "dur": dur,
                    "desc": f"Buffer 塞满，延迟 {f.get('dur_ms', 0):.1f}ms",
                    "severity": "medium",
                })

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--jank-types", type=str, default="")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    jank_types = [t.strip() for t in args.jank_types.split(",") if t.strip()]

    result = analyze(args.port, jank_types)
    save_result(result, "app_jank.json", OUTPUT_DIR)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
