#!/usr/bin/env python3
"""
Build an offline bundle for render-analyze — a self-contained archive
that can be dropped onto an air-gapped machine and run with
`scripts/setup_offline.py` without touching the public internet.

Run this on a Linux host with outbound network. Produces one archive
per (target-os, python-minor-version) combination under ./dist/:

    dist/render-analyze-offline-linux-x64-py310.tar.gz
    dist/render-analyze-offline-linux-x64-py311.tar.gz
    dist/render-analyze-offline-linux-x64-py312.tar.gz
    dist/render-analyze-offline-windows-x64-py310.zip
    dist/render-analyze-offline-windows-x64-py311.zip
    dist/render-analyze-offline-windows-x64-py312.zip

Each archive contains the repo working tree plus:
  wheels/                       pre-downloaded Python wheels
                                for the target OS + Python version
  vendor/ms-playwright/         Chromium headless shell for the target OS
                                (revision locked to the playwright
                                version in requirements.txt)

Usage:
    # Build one specific combo
    python3 scripts/build_offline_bundle.py --target linux-x64 --python 3.12

    # Build all six combos
    python3 scripts/build_offline_bundle.py --all

    # Custom output directory
    python3 scripts/build_offline_bundle.py --all --out /tmp/bundles

Requirements on the build host:
  - Linux (tested on Ubuntu 22.04 / 24.04)
  - Python 3.10+ for orchestration
  - curl, unzip, zip, tar available on PATH
  - ~2 GB free disk (wheels + chromium for all 6 combos)
  - Outbound network to pypi.org and
    playwright.download.prss.microsoft.com
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"

# ── Target matrix ─────────────────────────────────────────────────

TARGETS = {
    "linux-x64": {
        # pip download options
        "pip_platforms": ["manylinux2014_x86_64", "manylinux_2_17_x86_64"],
        "pip_abi": None,          # use default for the version
        # Chrome-for-Testing CDN suffix (after {browserVersion}/)
        "chromium_suffix": "linux64/chrome-headless-shell-linux64.zip",
        # archive format for the final bundle
        "archive_fmt": "tar.gz",
    },
    "windows-x64": {
        "pip_platforms": ["win_amd64"],
        "pip_abi": None,
        "chromium_suffix": "win64/chrome-headless-shell-win64.zip",
        "archive_fmt": "zip",
    },
}

PYTHON_VERSIONS = ["3.10", "3.11", "3.12"]

# Chrome for Testing (CfT) CDN — this is what modern playwright uses
# for its chromium-headless-shell downloads. The path encodes the full
# browser version (e.g. "145.0.7632.6") not the playwright revision,
# even though the on-disk directory is named by revision.
CHROMIUM_URL_TEMPLATE = (
    "https://cdn.playwright.dev/chrome-for-testing-public/{browser_version}/{suffix}"
)


# ── Helpers ───────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd, check=True, **kw):
    """Run a subprocess with simple logging."""
    shown = " ".join(str(c) for c in cmd)
    log(f"$ {shown}")
    return subprocess.run(cmd, check=check, **kw)


def get_chromium_info(tmp_venv: Path) -> tuple[str, str, str]:
    """Read the chromium browser name, revision, and full browserVersion
    from the local playwright package's browsers.json. Returns
    (name, revision, browser_version).

    - `name` = 'chromium-headless-shell' (preferred — smaller, ~80 MB)
      or 'chromium' as a fallback.
    - `revision` = playwright's internal build number (e.g. '1208'),
      used for the on-disk directory name.
    - `browser_version` = the full Chromium version string
      (e.g. '145.0.7632.6'), used as the path segment in the
      Chrome-for-Testing CDN URL.
    """
    pip = tmp_venv / "bin" / "pip"
    if sys.platform == "win32":
        pip = tmp_venv / "Scripts" / "pip.exe"
    run([
        str(pip), "install", "--quiet",
        "-r", str(REPO_ROOT / "requirements.txt"),
    ])

    py = tmp_venv / "bin" / "python3"
    if sys.platform == "win32":
        py = tmp_venv / "Scripts" / "python.exe"
    code = (
        "import playwright, json, pathlib, sys\n"
        "root = pathlib.Path(playwright.__file__).parent\n"
        "candidates = list(root.rglob('browsers.json'))\n"
        "if not candidates:\n"
        "    sys.exit('browsers.json not found inside playwright package')\n"
        "data = json.loads(candidates[0].read_text())\n"
        "for name in ('chromium-headless-shell', 'chromium'):\n"
        "    for b in data.get('browsers', []):\n"
        "        if b.get('name') == name:\n"
        "            print(name)\n"
        "            print(b.get('revision'))\n"
        "            print(b.get('browserVersion') or '')\n"
        "            sys.exit(0)\n"
        "sys.exit('no chromium entry in browsers.json')\n"
    )
    result = run([str(py), "-c", code], capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    if len(lines) < 3:
        raise RuntimeError(
            f"Could not parse chromium info from playwright metadata: {result.stdout!r}"
        )
    name, rev, browser_version = lines[0].strip(), lines[1].strip(), lines[2].strip()
    if not browser_version:
        raise RuntimeError(
            f"playwright browsers.json has no browserVersion for {name}; "
            "cannot build the Chrome-for-Testing CDN URL"
        )
    log(f"chromium resolved: name={name} revision={rev} browserVersion={browser_version}")
    return name, rev, browser_version


def download_wheels(target: str, py_version: str, wheels_out: Path, orchestrator_python: Path) -> None:
    """pip download the requirements for a specific (platform, python)
    combination. We rely on --only-binary=:all: so no wheels are built
    from source on the build host; missing wheels fail loudly.

    `orchestrator_python` is the Python executable inside the temporary
    orchestration venv. We use *it*, not sys.executable, because the
    system Python on a build host may not have pip installed at all
    (e.g. Ubuntu's /usr/bin/python3 ships without pip by default).
    """
    wheels_out.mkdir(parents=True, exist_ok=True)
    py_major, py_minor = py_version.split(".")
    py_compact = f"{py_major}{py_minor}"

    spec = TARGETS[target]
    cmd = [
        str(orchestrator_python), "-m", "pip", "download",
        "-r", str(REPO_ROOT / "requirements.txt"),
        "-d", str(wheels_out),
        "--only-binary=:all:",
        "--python-version", py_compact,
    ]
    for plat in spec["pip_platforms"]:
        cmd += ["--platform", plat]
    # Also download a matching pip wheel so the air-gapped setup can
    # "pip install --upgrade pip" from local files if it wants to.
    cmd_pip = [
        str(orchestrator_python), "-m", "pip", "download",
        "pip",
        "-d", str(wheels_out),
        "--only-binary=:all:",
        "--python-version", py_compact,
    ]
    for plat in spec["pip_platforms"]:
        cmd_pip += ["--platform", plat]

    run(cmd)
    run(cmd_pip, check=False)
    wheel_count = sum(1 for _ in wheels_out.glob("*.whl"))
    log(f"downloaded {wheel_count} wheels to {wheels_out}")


def download_chromium(
    target: str,
    browser_name: str,
    revision: str,
    browser_version: str,
    vendor_out: Path,
) -> None:
    """Fetch the Chromium headless shell zip from the Chrome-for-Testing
    CDN (the source modern playwright actually uses) and unpack it under
    vendor/ms-playwright/<browser_dir>-<rev>/ in the layout playwright
    expects.

    Layout after extraction (matches what `playwright install` would
    produce at `~/.cache/ms-playwright/`):

        vendor/ms-playwright/chromium_headless_shell-<rev>/
            chrome-headless-shell-linux64/chrome-headless-shell     (linux)
            chrome-headless-shell-win64/chrome-headless-shell.exe   (win)
            INSTALLATION_COMPLETE
    """
    spec = TARGETS[target]
    url = CHROMIUM_URL_TEMPLATE.format(
        browser_version=browser_version,
        suffix=spec["chromium_suffix"],
    )
    # Playwright's on-disk directory: underscore-separated name + rev.
    subdir_name = browser_name.replace("-", "_") + f"-{revision}"

    ms_pw = vendor_out / "ms-playwright"
    target_dir = ms_pw / subdir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    log(f"downloading {url}")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        urlretrieve(url, tmp_path)
        log(f"unpacking into {target_dir}")
        # Use extract() in a loop rather than extractall() so we can
        # preserve the Unix permission bits encoded in each ZipInfo's
        # external_attr. zipfile.extractall() drops them, which makes
        # the extracted chrome-headless-shell binary non-executable.
        with zipfile.ZipFile(tmp_path, "r") as z:
            for member in z.infolist():
                extracted_path = z.extract(member, target_dir)
                # Upper 16 bits of external_attr hold the Unix mode
                # (populated by zip tools on Unix; zero on Windows zips).
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(extracted_path, mode)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Write the INSTALLATION_COMPLETE marker. Without it playwright
    # refuses to launch, claiming the download is partial.
    marker = target_dir / "INSTALLATION_COMPLETE"
    marker.write_text(" ")
    log(f"wrote installation marker {marker}")


def collect_repo_tree(bundle_dir: Path) -> None:
    """Copy the git-tracked files into the bundle directory. We avoid
    copytree() on the whole repo because it would pull in .venv,
    dist/, wheels/, vendor/, __pycache__ etc.
    """
    log(f"collecting repo tree into {bundle_dir}")
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = [line for line in result.stdout.splitlines() if line]
    for rel in files:
        src = REPO_ROOT / rel
        if not src.is_file():
            continue  # skip deleted / symlink
        dst = bundle_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    log(f"copied {len(files)} files from git working tree")


def archive_bundle(bundle_dir: Path, target: str, py_version: str, out_root: Path) -> Path:
    """Tar.gz (Linux) or zip (Windows) the assembled bundle directory."""
    py_compact = py_version.replace(".", "")
    base_name = f"render-analyze-offline-{target}-py{py_compact}"
    out_root.mkdir(parents=True, exist_ok=True)
    spec = TARGETS[target]

    if spec["archive_fmt"] == "tar.gz":
        out_path = out_root / f"{base_name}.tar.gz"
        log(f"creating {out_path}")
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(bundle_dir, arcname="render-analyze")
    else:
        out_path = out_root / f"{base_name}.zip"
        log(f"creating {out_path}")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in bundle_dir.rglob("*"):
                if p.is_file():
                    arcname = Path("render-analyze") / p.relative_to(bundle_dir)
                    zf.write(p, arcname)
    size_mb = out_path.stat().st_size // (1024 * 1024)
    log(f"bundle size: {size_mb} MB")
    return out_path


def build_one(target: str, py_version: str, out_root: Path) -> Path:
    """Build a single (target, python-version) bundle."""
    log("=" * 60)
    log(f"BUILD target={target} python={py_version}")
    log("=" * 60)

    with tempfile.TemporaryDirectory(prefix="render-bundle-") as tmp:
        tmp_path = Path(tmp)

        # 1. Spin up an orchestration venv and resolve Chromium revision
        tmp_venv = tmp_path / "orchestration-venv"
        run([sys.executable, "-m", "venv", str(tmp_venv)])
        # Bootstrap pip inside the venv (ensurepip) and upgrade it so
        # cross-platform `pip download` has the latest resolver.
        venv_python_path = tmp_venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python3")
        run([str(venv_python_path), "-m", "ensurepip", "--upgrade"], check=False)
        run([str(venv_python_path), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        browser_name, revision, browser_version = get_chromium_info(tmp_venv)

        # 2. Stage the bundle contents
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        collect_repo_tree(bundle_dir)

        download_wheels(target, py_version, bundle_dir / "wheels", venv_python_path)
        download_chromium(
            target, browser_name, revision, browser_version, bundle_dir / "vendor"
        )

        # 3. Archive it
        return archive_bundle(bundle_dir, target, py_version, out_root)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build offline bundles for render-analyze",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--target",
        choices=list(TARGETS.keys()),
        help="Build for a single OS target (linux-x64 or windows-x64).",
    )
    p.add_argument(
        "--python",
        choices=PYTHON_VERSIONS,
        dest="python_version",
        help="Build for a single Python minor version (3.10, 3.11, 3.12).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Build every target × python-version combination (6 bundles total).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DIST,
        help="Output directory for the final archives (default: ./dist)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.all:
        combos = [(t, v) for t in TARGETS for v in PYTHON_VERSIONS]
    else:
        if not args.target or not args.python_version:
            print("ERROR: pass --all, or both --target and --python.", file=sys.stderr)
            return 2
        combos = [(args.target, args.python_version)]

    log(f"building {len(combos)} bundle(s) → {args.out}")
    produced = []
    for target, py in combos:
        try:
            path = build_one(target, py, args.out)
            produced.append(path)
        except Exception as e:
            log(f"FAILED for {target} py{py}: {e}")
            raise

    log("=" * 60)
    log("BUILD SUMMARY")
    log("=" * 60)
    for p in produced:
        size_mb = p.stat().st_size // (1024 * 1024)
        log(f"  {p.name}  ({size_mb} MB)")
    log("Upload these to the GitHub release for distribution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
