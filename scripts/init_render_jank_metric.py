#!/usr/bin/env python3
"""Initialize Android Jank CUJ metrics in trace_processor."""
import argparse, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from tp_query import query_tp, save_result

OUTPUT_DIR = os.environ.get("RENDER_OUTPUT", "/workspace/render_output")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        query_tp(args.port, "SELECT RUN_METRIC('android/jank/android_jank_cuj_init.sql');")
        result = {"metric_initialized": True, "status": "ready"}
    except Exception as e:
        result = {"metric_initialized": False, "status": "error", "error": str(e)}

    save_result(result, "jank_metric_init.json", OUTPUT_DIR)
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
