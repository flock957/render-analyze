#!/usr/bin/env python3
"""Initialize Trace Processor HTTP service and load a trace file."""

import argparse
import json
import os
import subprocess
import sys
import time
import shutil
import urllib.request


def _is_real_binary(path: str) -> bool:
    """Check if file is a real binary (not a Python wrapper script)."""
    try:
        with open(path, "rb") as f:
            header = f.read(4)
            # ELF binary starts with \x7fELF
            return header == b'\x7fELF'
    except Exception:
        return False


def find_or_install_tp() -> str:
    """Find or install trace_processor_shell."""
    # Check for prebuilt binary (downloaded by the Python wrapper)
    prebuilt = os.path.expanduser("~/.local/share/perfetto/prebuilts/trace_processor_shell")
    if os.path.isfile(prebuilt) and os.access(prebuilt, os.X_OK):
        return prebuilt

    # Check common locations, prefer real binaries
    for path in [
        "/usr/local/bin/trace_processor_shell",
        "/tmp/trace_processor_shell",
    ]:
        if os.path.isfile(path) and os.access(path, os.X_OK) and _is_real_binary(path):
            return path

    # Check PATH
    tp = shutil.which("trace_processor_shell")
    if tp and _is_real_binary(tp):
        return tp

    # Download using the Python wrapper script (which fetches the real binary)
    print("Downloading trace_processor_shell...", file=sys.stderr)
    wrapper = "/tmp/trace_processor_wrapper.py"
    url = "https://get.perfetto.dev/trace_processor"
    try:
        urllib.request.urlretrieve(url, wrapper)
        os.chmod(wrapper, 0o755)
        # Run --version to trigger the wrapper to download the real binary
        subprocess.run([sys.executable, wrapper, "--version"],
                       capture_output=True, timeout=120)
        # Now the real binary should be in ~/.local/share/perfetto/prebuilts/
        if os.path.isfile(prebuilt) and os.access(prebuilt, os.X_OK):
            print(f"Installed to {prebuilt}", file=sys.stderr)
            return prebuilt
        # Fallback: use the wrapper itself
        print(f"Using wrapper script at {wrapper}", file=sys.stderr)
        return wrapper
    except Exception as e:
        print(f"ERROR: Failed to download trace_processor: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Initialize Trace Processor")
    parser.add_argument("--trace", required=True, help="Path to trace file")
    parser.add_argument("--port", type=int, default=9001, help="HTTP port")
    parser.add_argument("--output-dir", default="/workspace/perf_analysis_output")
    args = parser.parse_args()

    if not os.path.isfile(args.trace):
        print(f"ERROR: Trace file not found: {args.trace}", file=sys.stderr)
        sys.exit(1)

    # Kill existing instances
    subprocess.run(["pkill", "-f", "trace_processor_shell"], capture_output=True)
    time.sleep(1)

    tp_bin = find_or_install_tp()
    print(f"Using trace_processor: {tp_bin}", file=sys.stderr)

    # Build command - use --http-port (newer) with -D flag
    cmd = [tp_bin, "-D", "--http-port", str(args.port), args.trace]
    # If tp_bin is a Python wrapper, prepend python3
    if not _is_real_binary(tp_bin):
        cmd = [sys.executable] + cmd

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for ready
    import requests
    for i in range(60):
        try:
            resp = requests.get(f"http://localhost:{args.port}/status", timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        print("ERROR: Trace Processor failed to start within 60s", file=sys.stderr)
        proc.kill()
        sys.exit(1)

    # Save state
    os.makedirs(args.output_dir, exist_ok=True)
    state = {
        "port": args.port,
        "pid": proc.pid,
        "trace_file": os.path.abspath(args.trace),
        "file_size_mb": round(os.path.getsize(args.trace) / 1024 / 1024, 2),
    }
    state_path = os.path.join(args.output_dir, "tp_state.json")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    result = {"status": "ready", **state}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
