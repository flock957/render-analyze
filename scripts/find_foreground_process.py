#!/usr/bin/env python3
"""Find foreground/target process from trace."""

import argparse
import json
import sys
sys.path.insert(0, __import__('os').path.dirname(__file__))
from tp_query import query_tp, parse_columns, save_result, add_common_args


def main():
    parser = argparse.ArgumentParser(description="Find foreground process")
    add_common_args(parser)
    args = parser.parse_args()

    # Method 1: FocusedApp
    sql_focused = """
    SELECT
      s.ts, s.dur, s.name AS slice_name,
      EXTRACT_ARG(s.arg_set_id, 'debug.str') AS focused_app
    FROM slice s
    JOIN track t ON s.track_id = t.id
    WHERE s.name = 'FocusedApp'
    ORDER BY s.ts DESC
    LIMIT 5
    """
    result = query_tp(args.port, sql_focused)
    rows = parse_columns(result)
    focused = [r for r in rows if r.get("focused_app")]

    if focused:
        app = focused[0]["focused_app"]
        # Extract package name (may have window info after /)
        process_name = app.split("/")[0] if "/" in app else app
        output = {
            "method": "FocusedApp",
            "process_name": process_name,
            "raw_focused_app": app,
        }
        save_result(output, "target_process.json", args.output_dir)
        print(json.dumps(output, indent=2))
        return

    # Method 2: Fallback - top processes by Running time
    sql_fallback = """
    SELECT
      p.name AS process_name,
      p.pid,
      SUM(CASE WHEN ts.state = 'Running' THEN ts.dur ELSE 0 END) AS total_running_ns,
      CAST(SUM(CASE WHEN ts.state = 'Running' THEN ts.dur ELSE 0 END) AS REAL) / 1e6 AS total_running_ms
    FROM process p
    JOIN thread t ON p.upid = t.upid
    JOIN thread_state ts ON t.utid = ts.utid
    WHERE p.name IS NOT NULL
      AND p.name NOT LIKE 'system_%'
      AND p.name NOT LIKE '/system/%'
      AND p.name NOT LIKE '/vendor/%'
      AND p.name NOT IN ('init', 'zygote', 'zygote64', 'surfaceflinger', 'logd', 'servicemanager', 'hwservicemanager', 'lmkd', 'vold')
    GROUP BY p.upid
    ORDER BY total_running_ns DESC
    LIMIT 10
    """
    result = query_tp(args.port, sql_fallback)
    rows = parse_columns(result)

    if rows:
        top = rows[0]
        output = {
            "method": "fallback_running_time",
            "process_name": top["process_name"],
            "pid": top["pid"],
            "total_running_ms": top.get("total_running_ms", 0),
            "candidates": rows[:5],
        }
    else:
        output = {"method": "none", "process_name": None, "error": "No processes found"}

    save_result(output, "target_process.json", args.output_dir)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
