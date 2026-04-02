#!/usr/bin/env python3
"""Render Performance Analysis - Environment Setup.

One-click setup for all dependencies:
  1. Python packages: requests, playwright
  2. Chromium browser (headless, for Perfetto UI screenshots)
  3. trace_processor_shell (Perfetto SQL query engine)

Usage:
    python3 setup_env.py [--check-only] [--skip-browser] [--skip-trace-processor]

Exit codes:
    0: All dependencies ready
    1: Setup failed (see error output)
    2: Check-only mode, some dependencies missing
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

# ---------------------------------------------------------------------------
# Dependency spec
# ---------------------------------------------------------------------------
PYTHON_PACKAGES = ["requests", "playwright"]

TRACE_PROCESSOR_URL = "https://get.perfetto.dev/trace_processor"
TRACE_PROCESSOR_PREBUILT = os.path.expanduser(
    "~/.local/share/perfetto/prebuilts/trace_processor_shell"
)
TRACE_PROCESSOR_FALLBACK = "/tmp/trace_processor_shell"


def _print(msg: str, level: str = "info"):
    prefix = {"info": "[env]", "ok": "[env] ✓", "warn": "[env] ⚠", "err": "[env] ✗"}
    print(f"{prefix.get(level, '[env]')} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Python packages
# ---------------------------------------------------------------------------
def check_python_package(pkg: str) -> bool:
    try:
        __import__(pkg)
        return True
    except ImportError:
        return False


def install_python_packages() -> bool:
    missing = [p for p in PYTHON_PACKAGES if not check_python_package(p)]
    if not missing:
        _print("Python packages: all installed", "ok")
        return True

    _print(f"Installing Python packages: {', '.join(missing)}")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *missing, "-q"],
            check=True, capture_output=True, timeout=120,
        )
        # Verify
        still_missing = [p for p in missing if not check_python_package(p)]
        if still_missing:
            # Try with --user flag
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", *still_missing, "-q"],
                check=True, capture_output=True, timeout=120,
            )
        _print(f"Python packages installed: {', '.join(missing)}", "ok")
        return True
    except Exception as e:
        _print(f"Failed to install packages: {e}", "err")
        _print("Try manually: pip install " + " ".join(missing), "warn")
        return False


# ---------------------------------------------------------------------------
# Chromium browser (for Playwright)
# ---------------------------------------------------------------------------
def check_chromium() -> bool:
    """Check if a usable Chromium/Chrome is available."""
    # Check system Chrome
    if shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium"):
        return True
    # Check Playwright's managed browsers
    playwright_browsers = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(playwright_browsers):
        for entry in os.listdir(playwright_browsers):
            if "chromium" in entry.lower():
                return True
    return False


def install_chromium() -> bool:
    if check_chromium():
        _print("Chromium/Chrome: available", "ok")
        return True

    _print("Installing Chromium via Playwright...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True, timeout=300,
        )
        _print("Chromium installed via Playwright", "ok")
        return True
    except Exception as e:
        _print(f"Failed to install Chromium: {e}", "err")
        _print("Try manually: python3 -m playwright install chromium", "warn")
        return False


# ---------------------------------------------------------------------------
# trace_processor_shell
# ---------------------------------------------------------------------------
def _is_elf_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b'\x7fELF'
    except Exception:
        return False


def check_trace_processor() -> bool:
    """Check if trace_processor_shell is available."""
    # Check prebuilt location
    if os.path.isfile(TRACE_PROCESSOR_PREBUILT) and os.access(TRACE_PROCESSOR_PREBUILT, os.X_OK):
        return True
    # Check fallback
    if os.path.isfile(TRACE_PROCESSOR_FALLBACK) and os.access(TRACE_PROCESSOR_FALLBACK, os.X_OK):
        return True
    # Check PATH
    tp = shutil.which("trace_processor_shell")
    if tp and _is_elf_binary(tp):
        return True
    return False


def install_trace_processor() -> bool:
    if check_trace_processor():
        _print("trace_processor_shell: available", "ok")
        return True

    _print("Downloading trace_processor_shell from get.perfetto.dev ...")
    wrapper = "/tmp/trace_processor_wrapper.py"
    try:
        urllib.request.urlretrieve(TRACE_PROCESSOR_URL, wrapper)
        os.chmod(wrapper, 0o755)
        # Run --version to trigger download of the real binary
        subprocess.run(
            [sys.executable, wrapper, "--version"],
            capture_output=True, timeout=120,
        )
        if os.path.isfile(TRACE_PROCESSOR_PREBUILT):
            _print(f"trace_processor_shell installed: {TRACE_PROCESSOR_PREBUILT}", "ok")
            return True
        else:
            _print(f"Using wrapper script at {wrapper}", "warn")
            return True
    except Exception as e:
        _print(f"Failed to download trace_processor: {e}", "err")
        _print("Manual download: curl -LO https://get.perfetto.dev/trace_processor", "warn")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def check_all() -> dict:
    """Check all dependencies and return status dict."""
    status = {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.system(),
        "arch": platform.machine(),
        "packages": {},
        "chromium": check_chromium(),
        "trace_processor": check_trace_processor(),
    }
    for pkg in PYTHON_PACKAGES:
        status["packages"][pkg] = check_python_package(pkg)

    status["all_ready"] = (
        all(status["packages"].values())
        and status["chromium"]
        and status["trace_processor"]
    )
    return status


def setup_all(skip_browser: bool = False, skip_tp: bool = False) -> bool:
    """Install all dependencies. Returns True if all succeeded."""
    _print(f"Python: {sys.executable} ({sys.version_info.major}.{sys.version_info.minor})")
    _print(f"Platform: {platform.system()} {platform.machine()}")

    results = []

    # 1. Python packages
    results.append(install_python_packages())

    # 2. Chromium
    if not skip_browser:
        results.append(install_chromium())
    else:
        _print("Chromium: skipped (--skip-browser)", "warn")

    # 3. trace_processor
    if not skip_tp:
        results.append(install_trace_processor())
    else:
        _print("trace_processor: skipped (--skip-trace-processor)", "warn")

    # Summary
    all_ok = all(results)
    if all_ok:
        _print("All dependencies ready!", "ok")
    else:
        _print("Some dependencies failed to install", "err")

    return all_ok


def main():
    parser = argparse.ArgumentParser(
        description="Render Performance Analysis - Environment Setup"
    )
    parser.add_argument("--check-only", action="store_true",
                        help="Only check, do not install")
    parser.add_argument("--skip-browser", action="store_true",
                        help="Skip Chromium installation (screenshots will be disabled)")
    parser.add_argument("--skip-trace-processor", action="store_true",
                        help="Skip trace_processor download")
    args = parser.parse_args()

    if args.check_only:
        status = check_all()
        print(json.dumps(status, indent=2))
        sys.exit(0 if status["all_ready"] else 2)
    else:
        ok = setup_all(
            skip_browser=args.skip_browser,
            skip_tp=args.skip_trace_processor,
        )
        # Output status as JSON for programmatic use
        status = check_all()
        print(json.dumps(status, indent=2))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
