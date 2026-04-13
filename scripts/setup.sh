#!/usr/bin/env bash
# setup.sh — one-command environment setup for render-analyze.
#
# Creates .venv, installs the Python dependencies from requirements.txt,
# downloads Playwright's Chromium (with CN mirror fallback), and runs a
# smoke test to confirm the browser can actually launch. Safe to re-run.
#
# Usage:
#     ./scripts/setup.sh
#
# Prerequisites: python3 >= 3.10, git, ~800 MB free disk.

set -e

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

echo "==> render-analyze environment setup"
echo "==> working dir: $HERE"
echo

# ---- 1. Python version check ----
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH. Install Python 3.10+ first." >&2
    exit 1
fi

PYMAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYMINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 10 ]; }; then
    echo "ERROR: Python ${PYMAJOR}.${PYMINOR} found but >= 3.10 is required." >&2
    exit 1
fi
echo "==> python3 ${PYMAJOR}.${PYMINOR} OK"

# ---- 2. Create venv (idempotent) ----
if [ ! -d ".venv" ]; then
    echo "==> creating venv at .venv"
    python3 -m venv .venv
else
    echo "==> .venv already exists, reusing"
fi

# ---- 3. Install Python deps ----
echo "==> installing Python deps from requirements.txt"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "==> python deps OK"

# ---- 4. Download Chromium (with CN mirror fallback) ----
# Playwright's own `install` is already idempotent: if the matching
# Chromium revision is already on disk it just prints a "already
# downloaded" message and exits 0. So we don't pre-check, we just
# always call it and let the CLI handle caching.
echo "==> installing Chromium (trying official host first)"
if .venv/bin/playwright install chromium; then
    echo "==> chromium install OK (official host)"
else
    echo "==> official host failed, retrying via Alibaba npmmirror..."
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
        .venv/bin/playwright install chromium --force
    echo "==> chromium install OK (npmmirror)"
fi

# ---- 5. Smoke test: can we actually launch it? ----
echo "==> smoke test: launching headless Chromium"
.venv/bin/python3 - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    browser.close()
print("==> smoke test PASSED")
PY

echo
echo "==> setup complete!"
echo "   Next step:"
echo "     .venv/bin/python3 scripts/run_workflow.py \\"
echo "       --trace /path/to/your.perfetto-trace \\"
echo "       --output-dir /path/to/output"
