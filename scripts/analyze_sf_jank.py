#!/usr/bin/env python3
"""Analyze SurfaceFlinger-layer Jank: CPU/GPU/HAL/Scheduling/Stuffing/Dropped."""
import argparse, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from tp_query import query_tp, parse_columns, save_result

OUTPUT_DIR = os.environ.get("RENDER_OUTPUT", "/workspace/render_output")

# --- SQL templates ---

def sql_jank_frames(jank_type_value: str) -> str:
    return f"""
SELECT display_frame_token, jank_type, ts, dur, dur / 1000000.0 AS dur_ms
FROM actual_frame_timeline_slice
WHERE jank_type = '{jank_type_value}'
ORDER BY dur DESC LIMIT 30;
"""

SQL_SF_MAIN_THREAD = """
SELECT name, ts, dur, dur / 1000000.0 AS dur_ms
FROM slice
WHERE (name GLOB '*SurfaceFlinger*' OR name GLOB '*onMessageReceived*')
    AND dur > 5000000
ORDER BY dur DESC LIMIT 20;
"""

SQL_LOCK_CONTENTION = """
SELECT name, ts, dur, dur / 1000000.0 AS dur_ms
FROM slice
WHERE name GLOB '*lock*' OR name GLOB '*mutex*'
ORDER BY dur DESC LIMIT 20;
"""

SQL_LAYER_COUNT = """
SELECT name, COUNT(*) AS layer_count
FROM slice WHERE name GLOB '*Layer*'
GROUP BY name ORDER BY layer_count DESC LIMIT 20;
"""

SQL_GPU_COMPOSE = """
SELECT name, ts, dur, dur / 1000000.0 AS dur_ms
FROM slice
WHERE name GLOB '*RenderEngine*' OR name GLOB '*GLES*' OR name GLOB '*Skia*'
ORDER BY dur DESC LIMIT 20;
"""

SQL_LAYER_UNIQUE = """
SELECT name, COUNT(*) AS unique_layers
FROM slice WHERE name GLOB '*Layer*' OR name GLOB '*layer*'
GROUP BY name ORDER BY unique_layers DESC LIMIT 10;
"""

SQL_HWC = """
SELECT name, ts, dur, dur / 1000000.0 AS dur_ms
FROM slice
WHERE name GLOB '*HWC*' OR name GLOB '*present*'
ORDER BY dur DESC LIMIT 20;
"""

SQL_PREV_FRAME_STUFFING = """
SELECT display_frame_token, dur / 1000000.0 AS dur_ms
FROM actual_frame_timeline_slice
WHERE display_frame_token IN (
    SELECT display_frame_token - 1
    FROM actual_frame_timeline_slice
    WHERE jank_type LIKE '%SurfaceFlinger Stuffing%'
)
ORDER BY dur DESC LIMIT 20;
"""

# Jank type to trace_processor value mapping
JANK_TYPE_MAP = {
    "SurfaceFlinger CPU Deadline Missed": "sf_cpu",
    "SurfaceFlinger GPU Deadline Missed": "sf_gpu",
    "Display HAL": "display_hal",
    "Prediction Error": "prediction_error",
    "SurfaceFlinger Scheduling": "sf_scheduling",
    "SurfaceFlinger Stuffing": "sf_stuffing",
    "Dropped Frame": "dropped",
}

SEVERITY_MAP = {
    "sf_cpu": "high",
    "sf_gpu": "high",
    "display_hal": "high",
    "prediction_error": "medium",
    "sf_scheduling": "medium",
    "sf_stuffing": "medium",
    "dropped": "high",
}


def _make_regions(frames: list, label: str, severity: str, max_n: int = 5) -> list:
    regions = []
    for f in frames[:max_n]:
        ts = int(f.get("ts", 0))
        dur = int(f.get("dur", 0))
        if ts > 0 and dur > 0:
            regions.append({
                "name": f"{label} #{f.get('display_frame_token', '?')}",
                "ts": ts, "dur": dur,
                "desc": f"{label} {f.get('dur_ms', 0):.1f}ms",
                "severity": severity,
            })
    return regions


def analyze(port: int, jank_types: list[str]):
    results = {
        "has_issue": False,
        "severity": "normal",
        "sf_cpu": None,
        "sf_gpu": None,
        "display_hal": None,
        "prediction_error": None,
        "sf_scheduling": None,
        "sf_stuffing": None,
        "dropped": None,
        "issue_regions": [],
    }

    def _match(target):
        return any(target in jt or jt in target for jt in jank_types)

    # 4.1 SF CPU
    if _match("SurfaceFlinger CPU Deadline Missed"):
        frames = parse_columns(query_tp(port, sql_jank_frames("SurfaceFlinger CPU Deadline Missed")))
        sf_main = parse_columns(query_tp(port, SQL_SF_MAIN_THREAD))
        locks = parse_columns(query_tp(port, SQL_LOCK_CONTENTION))
        layers = parse_columns(query_tp(port, SQL_LAYER_COUNT))

        results["sf_cpu"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
            "sf_main_thread_slow": len(sf_main), "top_sf_main": sf_main[:5],
            "lock_contention": len(locks), "top_locks": locks[:5],
            "max_layer_count": layers[0]["layer_count"] if layers else 0,
        }
        results["has_issue"] = True
        results["severity"] = "high"
        results["issue_regions"].extend(_make_regions(frames, "SF CPU Jank", "high"))

    # 4.2 SF GPU
    if _match("SurfaceFlinger GPU Deadline Missed"):
        frames = parse_columns(query_tp(port, sql_jank_frames("SurfaceFlinger GPU Deadline Missed")))
        gpu = parse_columns(query_tp(port, SQL_GPU_COMPOSE))
        ulayers = parse_columns(query_tp(port, SQL_LAYER_UNIQUE))

        results["sf_gpu"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
            "gpu_compose_slow": len(gpu), "top_gpu": gpu[:5],
            "max_unique_layers": ulayers[0]["unique_layers"] if ulayers else 0,
        }
        results["has_issue"] = True
        if results["severity"] != "high":
            results["severity"] = "high"
        results["issue_regions"].extend(_make_regions(frames, "SF GPU Jank", "high"))

    # 4.3 Display HAL
    if _match("Display HAL"):
        frames = parse_columns(query_tp(port, sql_jank_frames("Display HAL")))
        hwc = parse_columns(query_tp(port, SQL_HWC))

        results["display_hal"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
            "hwc_events": len(hwc), "top_hwc": hwc[:5],
        }
        results["has_issue"] = True
        results["severity"] = "high"
        results["issue_regions"].extend(_make_regions(frames, "Display HAL Jank", "high"))

    # 4.4 Prediction Error
    if _match("Prediction Error"):
        frames = parse_columns(query_tp(port, sql_jank_frames("Prediction Error")))
        results["prediction_error"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
        }
        results["has_issue"] = True
        if results["severity"] == "normal":
            results["severity"] = "medium"
        results["issue_regions"].extend(_make_regions(frames, "Prediction Error", "medium", 3))

    # 4.5 SF Scheduling
    if _match("SurfaceFlinger Scheduling"):
        frames = parse_columns(query_tp(port, sql_jank_frames("SurfaceFlinger Scheduling")))
        results["sf_scheduling"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
        }
        results["has_issue"] = True
        if results["severity"] == "normal":
            results["severity"] = "medium"
        results["issue_regions"].extend(_make_regions(frames, "SF Scheduling", "medium", 3))

    # 4.6 SF Stuffing
    if _match("SurfaceFlinger Stuffing"):
        frames = parse_columns(query_tp(port, sql_jank_frames("SurfaceFlinger Stuffing")))
        prev = parse_columns(query_tp(port, SQL_PREV_FRAME_STUFFING))
        results["sf_stuffing"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
            "prev_frame_slow": prev[:5],
        }
        results["has_issue"] = True
        if results["severity"] == "normal":
            results["severity"] = "medium"
        results["issue_regions"].extend(_make_regions(frames, "SF Stuffing", "medium", 3))

    # 4.7 Dropped
    if _match("Dropped Frame"):
        frames = parse_columns(query_tp(port, sql_jank_frames("Dropped Frame")))
        results["dropped"] = {
            "jank_frames": len(frames), "top_frames": frames[:10],
        }
        results["has_issue"] = True
        results["severity"] = "high"
        results["issue_regions"].extend(_make_regions(frames, "Dropped Frame", "high"))

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--jank-types", type=str, default="")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    jank_types = [t.strip() for t in args.jank_types.split(",") if t.strip()]

    result = analyze(args.port, jank_types)
    save_result(result, "sf_jank.json", OUTPUT_DIR)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
