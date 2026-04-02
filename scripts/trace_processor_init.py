#!/usr/bin/env python3
"""Initialize Trace Processor HTTP service and load a trace file.

Starts trace_processor_shell as a daemon process (survives parent exit),
waits for HTTP readiness, and saves state to output directory.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import shutil
import urllib.request


def _is_real_binary(path: str) -> bool:
    """Check if file is a real binary (not a Python wrapper script)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b'\x7fELF'
    except Exception:
        return False


def _kill_existing(port: int):
    """Kill any existing trace_processor on the given port."""
    # Method 1: Read PID from state file
    # (handled by caller if needed)

    # Method 2: Find process listening on port via /proc/net/tcp
    try:
        hex_port = f"{port:04X}"
        with open("/proc/net/tcp") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 10:
                    continue
                local_addr = parts[1]
                if local_addr.endswith(f":{hex_port}"):
                    inode = parts[9]
                    # Find PID by inode
                    for pid_dir in os.listdir("/proc"):
                        if not pid_dir.isdigit():
                            continue
                        try:
                            fd_dir = f"/proc/{pid_dir}/fd"
                            for fd in os.listdir(fd_dir):
                                link = os.readlink(f"{fd_dir}/{fd}")
                                if f"socket:[{inode}]" in link:
                                    os.kill(int(pid_dir), signal.SIGTERM)
                                    print(f"Killed existing process on port {port} (PID {pid_dir})",
                                          file=sys.stderr)
                                    time.sleep(1)
                                    return
                        except (PermissionError, FileNotFoundError):
                            continue
    except Exception:
        pass

    # Method 3: Try connecting and if something responds, warn
    try:
        import urllib.request
        urllib.request.urlopen(f"http://localhost:{port}/status", timeout=1)
        print(f"WARNING: Port {port} is in use but could not kill the process",
              file=sys.stderr)
    except Exception:
        pass  # Port is free


def find_or_install_tp() -> str:
    """Find or install trace_processor_shell."""
    prebuilt = os.path.expanduser("~/.local/share/perfetto/prebuilts/trace_processor_shell")
    if os.path.isfile(prebuilt) and os.access(prebuilt, os.X_OK):
        return prebuilt

    for path in [
        "/usr/local/bin/trace_processor_shell",
        "/tmp/trace_processor_shell",
    ]:
        if os.path.isfile(path) and os.access(path, os.X_OK) and _is_real_binary(path):
            return path

    tp = shutil.which("trace_processor_shell")
    if tp and _is_real_binary(tp):
        return tp

    # Download
    print("Downloading trace_processor_shell...", file=sys.stderr)
    wrapper = "/tmp/trace_processor_wrapper.py"
    url = "https://get.perfetto.dev/trace_processor"
    try:
        urllib.request.urlretrieve(url, wrapper)
        os.chmod(wrapper, 0o755)
        subprocess.run([sys.executable, wrapper, "--version"],
                       capture_output=True, timeout=120)
        if os.path.isfile(prebuilt) and os.access(prebuilt, os.X_OK):
            print(f"Installed to {prebuilt}", file=sys.stderr)
            return prebuilt
        print(f"Using wrapper script at {wrapper}", file=sys.stderr)
        return wrapper
    except Exception as e:
        print(f"ERROR: Failed to download trace_processor: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Initialize Trace Processor")
    parser.add_argument("--trace", required=True, help="Path to trace file")
    parser.add_argument("--port", type=int, default=9001, help="HTTP port")
    parser.add_argument("--output-dir", default="/workspace/render_output")
    args = parser.parse_args()

    if not os.path.isfile(args.trace):
        print(f"ERROR: Trace file not found: {args.trace}", file=sys.stderr)
        sys.exit(1)

    # Kill existing instances on this port (without pkill)
    _kill_existing(args.port)

    tp_bin = find_or_install_tp()
    print(f"Using trace_processor: {tp_bin}", file=sys.stderr)

    # Build command
    cmd = [tp_bin, "-D", "--http-port", str(args.port), args.trace]
    if not _is_real_binary(tp_bin):
        cmd = [sys.executable] + cmd

    # Start as independent daemon (survives parent exit)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for HTTP readiness
    import requests
    for i in range(90):
        try:
            resp = requests.get(f"http://localhost:{args.port}/status", timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        # Check if process died
        if proc.poll() is not None:
            print("ERROR: trace_processor exited unexpectedly", file=sys.stderr)
            sys.exit(1)
        time.sleep(1)
    else:
        print("ERROR: Trace Processor failed to start within 90s", file=sys.stderr)
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
