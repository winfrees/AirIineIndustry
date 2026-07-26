"""
Build a self-contained Windows bundle of airlinesim.

The output is a folder (and a zip of it) that runs on a Windows machine with
**no Python installed**: CPython's official "Windows embeddable package" plus
this package installed beside it, plus .bat launchers.

Why not PyInstaller / a single .exe:
  * this project is pure standard library, and the embeddable distribution keeps
    the build that way — no third-party runtime *or* build dependency;
  * the package reads real files off disk (`routedata.DATA_DIR` and
    `server.WEBUI_DIR` are both `__file__`-relative), so a frozen one-file exe
    or a zipapp would need those lookups rewritten to importlib.resources.
    Shipping an ordinary directory tree keeps `__file__` honest.

Usage (normally called from .github/workflows/windows-release.yml):

    python tools/build_windows_bundle.py --version 0.1.0 --out dist
    python tools/build_windows_bundle.py --version 0.1.0 --wheel dist/foo.whl

Runs on any OS (it only downloads and unzips), but the smoke test that proves
the bundle actually works has to run on Windows — see smoke_windows_bundle.py.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Bumping this is a deliberate act: it is the interpreter every Windows tester
# will actually run. Keep it inside pyproject's requires-python range.
DEFAULT_PYTHON = "3.12.10"
EMBED_URL = ("https://www.python.org/ftp/python/{v}/python-{v}-embed-{arch}.zip")


# --------------------------------------------------------------------------
# embeddable CPython

def fetch_embeddable(version: str, arch: str, cache: Path,
                     expected_sha256: str | None) -> Path:
    """Download (or reuse) the official embeddable zip and verify its digest."""
    url = EMBED_URL.format(v=version, arch=arch)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"python-{version}-embed-{arch}.zip"

    if not dest.exists():
        print(f"[bundle] downloading {url}")
        with urllib.request.urlopen(url, timeout=180) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
    else:
        print(f"[bundle] reusing cached {dest}")

    digest = sha256_of(dest)
    print(f"[bundle] sha256 {digest}  {dest.name}")
    if expected_sha256:
        if digest.lower() != expected_sha256.lower():
            raise SystemExit(
                f"embeddable zip digest mismatch\n  expected {expected_sha256}\n"
                f"  got      {digest}")
        print("[bundle] digest matches --sha256")
    else:
        # Honest about what is and isn't verified: the transport is HTTPS to
        # python.org, but nothing pins the artifact unless a maintainer passes
        # the digest above. Printed so it can be pinned.
        print("[bundle] NOTE: no --sha256 given; artifact is trusted on HTTPS alone")
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def patch_pth(python_dir: Path) -> Path:
    """Teach the embeddable interpreter about Lib\\site-packages.

    The embeddable distribution ships a `pythonXY._pth` that pins sys.path and
    leaves `import site` commented out, so a plain `pip --target` install next
    to it is invisible. Both lines have to change.
    """
    candidates = sorted(python_dir.glob("python*._pth"))
    if not candidates:
        raise SystemExit(f"no python*._pth in {python_dir} — not an embeddable dist?")
    pth = candidates[0]
    lines = pth.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    for line in lines:
        if line.strip() in ("#import site", "# import site"):
            out.append("import site")
        else:
            out.append(line)
    if "import site" not in out:
        out.append("import site")
    if r"Lib\site-packages" not in out:
        # after "." so the bundle's own directory still wins
        insert_at = out.index(".") + 1 if "." in out else len(out)
        out.insert(insert_at, r"Lib\site-packages")

    pth.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[bundle] patched {pth.name}: {out}")
    return pth


# --------------------------------------------------------------------------
# the package itself

def install_package(site_packages: Path, wheel: Path | None) -> None:
    """Install airlinesim into the bundle's site-packages.

    Uses the *host* pip, which is fine because the package is pure Python with
    no dependencies — there is nothing to resolve per-platform. `--no-compile`
    keeps host bytecode out of the bundle; the embedded interpreter compiles on
    first run (and the workflow pre-compiles with the bundled python.exe).
    """
    source = str(wheel) if wheel else str(REPO_ROOT)
    site_packages.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install",
           "--no-deps", "--no-compile", "--upgrade",
           "--target", str(site_packages), source]
    print(f"[bundle] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    # pip --target drops console-script wrappers carrying the *host* interpreter
    # path. They would be broken on the tester's machine; the .bat launchers
    # below are the supported entry points.
    for junk in ("bin", "Scripts"):
        d = site_packages / junk
        if d.is_dir():
            shutil.rmtree(d)

    pkg = site_packages / "airlinesim"
    if not (pkg / "cli.py").is_file():
        raise SystemExit(f"install did not produce {pkg}/cli.py")
    # The data corpus and the web UI are package data; if they are missing the
    # bundle looks fine and then fails on the tester's machine.
    for required in (pkg / "data", pkg / "webui" / "index.html",
                     pkg / "btsdata" / "fixtures"):
        if not required.exists():
            raise SystemExit(f"package data missing from bundle: {required}")


# --------------------------------------------------------------------------
# launchers and docs

def write_text_crlf(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\r\n")


# Every launcher forces UTF-8. The engine prints em dashes, arrows and R² in
# its reports; under the legacy console code pages (437/1252) those raise
# UnicodeEncodeError the moment output is piped or redirected to a file —
# `airlinesim.bat run databuilt > log.txt` crashes without this. PYTHONUTF8
# fixes the encoding, `chcp 65001` makes the console render it.
UTF8_PREAMBLE = """chcp 65001 >nul 2>&1
set PYTHONUTF8=1
"""

LAUNCHERS: dict[str, str] = {
    # Generic passthrough: `airlinesim.bat run integration`, `airlinesim.bat --help`
    "airlinesim.bat": """@echo off
setlocal
{utf8}"%~dp0python\\python.exe" -m airlinesim.cli %*
exit /b %errorlevel%
""",
    # Double-click target for a tester: starts the local server and opens a browser.
    "AirlineSim-GUI.bat": """@echo off
setlocal
{utf8}title AirlineSim
echo Starting AirlineSim...
echo A browser tab will open. Close this window to stop the game server.
echo.
echo (Windows may ask to allow network access: the server binds to your LAN so
echo  a phone or tablet on the same network can play too. "Cancel" still leaves
echo  it reachable on this machine at http://127.0.0.1:8765 )
echo.
"%~dp0python\\python.exe" -m airlinesim.cli gui %*
if errorlevel 1 (
  echo.
  echo AirlineSim exited with an error ^(code %errorlevel%^).
  pause
)
exit /b %errorlevel%
""",
    # Headless sanity run, for "does this build work at all".
    "AirlineSim-Demo.bat": """@echo off
setlocal
{utf8}title AirlineSim demo
"%~dp0python\\python.exe" -m airlinesim.cli demo --days 60 %*
echo.
pause
exit /b %errorlevel%
""",
    # The integration scenario is this project's test suite; give testers a
    # one-click way to report "green/red on my machine".
    "Run-Checks.bat": """@echo off
setlocal
{utf8}title AirlineSim checks
set FAILED=0
for %%S in (integration routedata btsdata databuilt refresh_cx) do (
  echo === %%S ===
  "%~dp0python\\python.exe" -m airlinesim.cli run %%S || set FAILED=1
  echo.
)
if "%FAILED%"=="1" (
  echo One or more scenarios failed to run.
) else (
  echo All scenarios ran. Check that each printed ALL CHECKS PASS above.
)
pause
exit /b %FAILED%
""",
}


README_TXT = """AirlineSim {version} — portable Windows build
================================================================

Self-contained. Nothing to install, no Python needed: this folder carries its
own CPython {pyver} ({arch}) runtime under python\\.

Quick start
-----------
  AirlineSim-GUI.bat     play in your browser (starts a local server, opens a tab)
  AirlineSim-Demo.bat    run the 60-day two-carrier demo in a console window
  Run-Checks.bat         run the bundled scenarios and print PASS/FAIL
  airlinesim.bat ...     the full command line, e.g.:
                           airlinesim.bat list
                           airlinesim.bat run integration
                           airlinesim.bat demo --days 120
                           airlinesim.bat demo --data --hub ORD
                           airlinesim.bat gui --port 9000 --no-browser

Things to expect
----------------
* SmartScreen. This build is not code-signed, so Windows may show
  "Windows protected your PC" the first time. More info -> Run anyway.
* Firewall prompt. The GUI serves on 0.0.0.0 so another device on your
  network can connect. Declining the prompt still leaves it working locally
  at http://127.0.0.1:8765 .
* Saves go to %USERPROFILE%\\.airlinesim_save.pkl (not inside this folder),
  so they survive replacing this folder with a newer build.
* Unpack before running. Running the .bat files from inside Explorer's
  zip preview does not work — extract the folder first.
* Everything is offline. No network access is needed or attempted; the
  route corpus ships inside the bundle.

What is in here
---------------
  python\\                       embedded CPython {pyver}
  python\\Lib\\site-packages\\    the airlinesim package + its data corpus
  BUILD-INFO.txt                exact version, commit and build time
  LICENSE                       MIT

Report problems with the contents of BUILD-INFO.txt attached.
"""


def write_docs(root: Path, version: str, pyver: str, arch: str, commit: str) -> None:
    write_text_crlf(root / "README-WINDOWS.txt",
                    README_TXT.format(version=version, pyver=pyver, arch=arch))

    build_info = (
        f"airlinesim {version}\n"
        f"python:     {pyver} ({arch}, official embeddable distribution)\n"
        f"commit:     {commit}\n"
        f"built:      {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"built on:   {sys.platform} / python {sys.version.split()[0]}\n"
    )
    write_text_crlf(root / "BUILD-INFO.txt", build_info)

    license_src = REPO_ROOT / "LICENSE"
    if license_src.is_file():
        shutil.copy2(license_src, root / "LICENSE")


# --------------------------------------------------------------------------

def build(version: str, python_version: str, arch: str, out_dir: Path,
          wheel: Path | None, cache: Path, sha256: str | None,
          commit: str) -> tuple[Path, Path]:
    name = f"AirlineSim-{version}-win-{arch}"
    root = out_dir / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    python_dir = root / "python"
    embed_zip = fetch_embeddable(python_version, arch, cache, sha256)
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(python_dir)
    patch_pth(python_dir)

    install_package(python_dir / "Lib" / "site-packages", wheel)

    for filename, body in LAUNCHERS.items():
        write_text_crlf(root / filename, body.format(utf8=UTF8_PREAMBLE))
    write_docs(root, version, python_version, arch, commit)

    archive = out_dir / f"{name}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            zf.write(path, Path(name) / path.relative_to(root))

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"[bundle] wrote {root}")
    print(f"[bundle] wrote {archive}  ({size_mb:.1f} MB)")
    print(f"[bundle] sha256 {sha256_of(archive)}  {archive.name}")
    return root, archive


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", required=True,
                   help="version string for the bundle name and BUILD-INFO.txt")
    p.add_argument("--python-version", default=DEFAULT_PYTHON,
                   help=f"embeddable CPython to bundle (default {DEFAULT_PYTHON})")
    p.add_argument("--arch", default="amd64", choices=["amd64", "win32", "arm64"],
                   help="embeddable distribution architecture (default amd64)")
    p.add_argument("--out", default="dist", type=Path, help="output directory")
    p.add_argument("--wheel", type=Path, default=None,
                   help="install this wheel instead of building from the repo")
    p.add_argument("--cache", type=Path, default=Path(".embed-cache"),
                   help="where to keep the downloaded embeddable zip")
    p.add_argument("--sha256", default=os.environ.get("EMBED_SHA256") or None,
                   help="expected sha256 of the embeddable zip (recommended)")
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "unknown"),
                   help="commit recorded in BUILD-INFO.txt")
    args = p.parse_args(argv)

    if args.wheel and not args.wheel.is_file():
        raise SystemExit(f"--wheel not found: {args.wheel}")
    args.out.mkdir(parents=True, exist_ok=True)

    build(args.version, args.python_version, args.arch, args.out,
          args.wheel, args.cache, args.sha256, args.commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
