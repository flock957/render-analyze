#!/usr/bin/env python3
"""Cleanup Trace Processor service."""

import argparse
import json
import os
import signal
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Cleanup Trace Processor")
    parser.add_argument("--output-dir", default="/workspace/perf_analysis_output")
    args = parser.parse_args()

    state_file = os.path.join(args.output_dir, "tp_state.json")
    killed = False

    if os.path.isfile(state_file):
        with open(state_file) as f:
            state = json.load(f)
        pid = state.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
                print(f"Killed trace_processor_shell (PID: {pid})", file=sys.stderr)
            except ProcessLookupError:
                pass

    # Ensure no remaining instances
    subprocess.run(["pkill", "-f", "trace_processor_shell"], capture_output=True)

    output = {
        "status": "cleaned",
        "killed_pid": killed,
        "output_dir": args.output_dir,
        "files": os.listdir(args.output_dir) if os.path.isdir(args.output_dir) else [],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
