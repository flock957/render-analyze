#!/usr/bin/env python3
"""Analyze Jank type distribution from actual_frame_timeline_slice."""
import argparse, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from tp_query import query_tp, parse_columns, save_result

OUTPUT_DIR = os.environ.get("RENDER_OUTPUT", "/workspace/render_output")

SQL_JANK_TYPES = """
SELECT
    jank_type,
    jank_tag,
    COUNT(*) AS frame_count,
    AVG(dur) / 1000000.0 AS avg_dur_ms
FROM actual_frame_timeline_slice
WHERE jank_type IS NOT NULL
    AND jank_type NOT IN ('None', 'Unspecified')
GROUP BY jank_type, jank_tag
ORDER BY frame_count DESC;
"""

SQL_TOTAL_FRAMES = """
SELECT COUNT(*) AS total FROM actual_frame_timeline_slice;
"""

SEVERITY_MAP = {
    "AppDeadlineMissed": "high",
    "BufferStuffing": "medium",
    "SurfaceFlingerCpuDeadlineMissed": "high",
    "SurfaceFlingerGpuDeadlineMissed": "high",
    "DisplayHal": "high",
    "PredictionError": "medium",
    "SurfaceFlingerScheduling": "medium",
    "SurfaceFlingerStuffing": "medium",
    "DroppedFrame": "high",
    "Unknown": "high",
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Total frames
    total_result = query_tp(args.port, SQL_TOTAL_FRAMES)
    total_rows = parse_columns(total_result)
    total_frames = total_rows[0]["total"] if total_rows else 0

    # Jank types
    jank_result = query_tp(args.port, SQL_JANK_TYPES)
    jank_rows = parse_columns(jank_result)

    jank_types = []
    jank_frame_count = 0
    detected_types = []

    for row in jank_rows:
        jt = row.get("jank_type", "Unknown")
        count = int(row.get("frame_count", 0))
        avg_ms = float(row.get("avg_dur_ms", 0))
        jank_frame_count += count
        detected_types.append(jt)
        jank_types.append({
            "jank_type": jt,
            "jank_tag": row.get("jank_tag", ""),
            "frame_count": count,
            "avg_dur_ms": round(avg_ms, 2),
            "severity": SEVERITY_MAP.get(jt, "medium"),
        })

    jank_rate = (jank_frame_count / total_frames * 100) if total_frames > 0 else 0
    has_issue = jank_frame_count > 0
    severity = "high" if jank_rate > 5 else "medium" if jank_rate > 1 else "normal"

    result = {
        "total_frames": int(total_frames),
        "jank_frame_count": jank_frame_count,
        "jank_rate_pct": round(jank_rate, 2),
        "has_issue": has_issue,
        "severity": severity,
        "detected_types": list(set(detected_types)),
        "jank_types": jank_types,
    }

    save_result(result, "jank_types.json", OUTPUT_DIR)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
