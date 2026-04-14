#!/usr/bin/env python3
"""
Offline (air-gapped) installer for render-analyze.

Runs inside a freshly-extracted offline bundle. The bundle ships with:
  - wheels/     pre-downloaded Python wheels (pip download)
  - vendor/     pre-downloaded Chromium (and optionally trace_processor)

This script is pure Python, no bash, so it runs on both Linux and
Windows (and macOS, though we don't publish macOS bundles). It:

  1. Verifies Python >= 3.10
  2. Creates .venv (reuses if present)
  3. `pip install --no-index --find-links wheels/ -r requirements.txt`
  4. Launches headless Chromium from vendor/ms-playwright as a smoke test
  5. Prints the next-step command

Does NOT touch the network at any point.

Usage:
    python3 scripts/setup_offline.py            # Linux / macOS
    python scripts\\setup_offline.py            # Windows
    # or double-click scripts\\setup_offline.bat on Windows
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # repo root
VENV = HERE / ".venv"
WHEELS = HERE / "wheels"
VENDOR_PW = HERE / "vendor" / "ms-playwright"
IS_WIN = sys.platform == "win32"


def _venv_bin(name: str) -> Path:
    """Return the absolute path to an executable inside the venv."""
    if IS_WIN:
        return VENV / "Scripts" / f"{name}.exe"
    return VENV / "bin" / name


def venv_python() -> Path:
    return _venv_bin("python") if IS_WIN else _venv_bin("python3")


def venv_pip() -> Path:
    return _venv_bin("pip")


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


# ---------- steps ----------

def check_python_version() -> None:
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        err(f"Python {v.major}.{v.minor} found but >= 3.10 is required.")
        sys.exit(1)
    log(f"python {v.major}.{v.minor}.{v.micro} OK")


def check_bundle_layout() -> None:
    if not WHEELS.is_dir():
        err(f"wheels/ directory not found at {WHEELS}")
        err("You are not inside an offline bundle. Use scripts/setup.sh for online setup,")
        err("or extract the offline tarball first (see docs/quickstart.md Offline section).")
        sys.exit(1)
    if not any(WHEELS.iterdir()):
        err(f"wheels/ exists but is empty at {WHEELS}")
        sys.exit(1)
    if not VENDOR_PW.is_dir():
        err(f"vendor/ms-playwright/ not found at {VENDOR_PW}")
        err("The offline bundle is incomplete — missing the Chromium payload.")
        sys.exit(1)
    wheel_count = sum(1 for p in WHEELS.glob("*.whl"))
    log(f"bundle layout OK (wheels: {wheel_count} files, chromium at {VENDOR_PW.name}/)")


def create_venv() -> None:
    if VENV.is_dir():
        log(f".venv already exists at {VENV}, reusing")
        return
    log(f"creating venv at {VENV}")
    subprocess.run(
        [sys.executable, "-m", "venv", str(VENV)],
        check=True,
    )


def pip_install_offline() -> None:
    req = HERE / "requirements.txt"
    if not req.is_file():
        err(f"requirements.txt not found at {req}")
        sys.exit(1)

    # Upgrade pip if a newer pip wheel is bundled; ignore failure
    # (bundle may not include an updated pip wheel).
    log("upgrading pip from bundled wheels (best effort)")
    subprocess.run(
        [
            str(venv_pip()), "install",
            "--no-index", "--find-links", str(WHEELS),
            "--upgrade", "pip",
        ],
        check=False,
    )

    log("pip install (offline, --no-index --find-links wheels/)")
    subprocess.run(
        [
            str(venv_pip()), "install",
            "--no-index", "--find-links", str(WHEELS),
            "-r", str(req),
        ],
        check=True,
    )
    log("python deps installed from local wheels")


def smoke_test() -> None:
    log("smoke test: launching headless Chromium from vendor/ms-playwright")
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(VENDOR_PW)
    code = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    b = p.chromium.launch(headless=True, args=['--no-sandbox'])\n"
        "    b.close()\n"
        "print('SMOKE TEST PASSED')\n"
    )
    subprocess.run(
        [str(venv_python()), "-c", code],
        check=True,
        env=env,
    )


def print_next_step() -> None:
    print()
    log("setup complete!")
    print()
    print("   Next step — run the workflow (vendor/ is auto-detected, no env vars needed):")
    print()
    if IS_WIN:
        print(r"     .venv\Scripts\python.exe scripts\run_workflow.py ^")
        print(r"       --trace path\to\your.perfetto-trace ^")
        print(r"       --output-dir path\to\output")
    else:
        print("     .venv/bin/python3 scripts/run_workflow.py \\")
        print("       --trace /path/to/your.perfetto-trace \\")
        print("       --output-dir /path/to/output")
    print()


def main() -> None:
    print("==> render-analyze offline setup")
    print(f"==> repo root: {HERE}")
    print(f"==> platform:  {sys.platform}")
    print()
    check_python_version()
    check_bundle_layout()
    create_venv()
    pip_install_offline()
    smoke_test()
    print_next_step()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        err(f"subprocess failed (exit {e.returncode}): {' '.join(str(a) for a in e.cmd)}")
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        err("interrupted")
        sys.exit(130)
