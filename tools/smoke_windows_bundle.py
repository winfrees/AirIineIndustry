"""
Smoke-test an airlinesim install: scenarios, the demo, and the GUI server.

Two modes, same checks, so a plain `pip install` and the portable Windows
bundle are held to one bar:

    python tools/smoke_windows_bundle.py                       # this interpreter
    python tools/smoke_windows_bundle.py --bundle dist/AirlineSim-0.1.0-win-amd64

The scenarios never exit non-zero — they print "ALL CHECKS PASS" or
"SOME CHECKS FAILED" (see scenarios/integration.py). So the pass condition is
the text, not the return code, and a scenario that crashes half way through
fails here on both counts.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Every scenario that self-checks and is safe offline.
CHECKED_SCENARIOS = ["integration", "routedata", "btsdata", "databuilt", "refresh_cx"]
# These only print a report; the bar is "runs without raising".
SMOKE_SCENARIOS = ["competitive", "crew", "deadhead", "roster", "route", "finance"]

PASS_MARKER = "ALL CHECKS PASS"


class Failures(list):
    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            if detail:
                print(detail.rstrip()[-2000:])
            self.append(label)
        return ok


def child_env() -> dict:
    """Match what the shipped .bat launchers do.

    The engine prints em dashes, arrows and R² in its reports. Captured output
    is a pipe, so on Windows the child encodes with the legacy code page and
    raises UnicodeEncodeError unless UTF-8 mode is on. The bundle's launchers
    set PYTHONUTF8=1 for exactly this reason; a bare `pip install` run has to
    set it here so both paths are tested under the same conditions.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    return env


def run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, env=child_env(),
                          timeout=timeout, encoding="utf-8", errors="replace")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_tree(proc: subprocess.Popen) -> None:
    """A .bat launcher means cmd.exe is the parent of python.exe; terminating
    the .bat alone leaves the server running and the job hanging."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


# --------------------------------------------------------------------------

def check_gui(base_cmd: list[str], fails: Failures) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"\n[smoke] GUI server on {url}")
    proc = subprocess.Popen(base_cmd + ["gui", "--port", str(port), "--no-browser"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=child_env())
    try:
        state = None
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                status, body = get(f"{url}/api/state")
                if status == 200:
                    state = json.loads(body)
                    break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                time.sleep(1.0)

        if not fails.check(state is not None, "GUI server answered /api/state",
                           (proc.stdout.read() if proc.poll() is not None and proc.stdout
                            else "server never became ready")):
            return

        fails.check(isinstance(state, dict) and bool(state),
                    "/api/state returned a snapshot")

        status, body = get(f"{url}/api/catalog")
        fails.check(status == 200 and json.loads(body) is not None,
                    "/api/catalog served")

        status, body = get(f"{url}/")
        fails.check(status == 200 and b"<html" in body.lower(),
                    "web UI index.html served")

        status, body = get(f"{url}/manifest.json")
        fails.check(status == 200, "web UI static assets served")
    finally:
        kill_tree(proc)


def check_bundle_is_self_contained(bundle: Path, fails: Failures) -> None:
    """The whole point of the bundle is that it does not use the machine's
    Python. Prove it rather than assume it."""
    python_exe = bundle / "python" / "python.exe"
    fails.check(python_exe.is_file(), f"bundled interpreter present ({python_exe})")
    if not python_exe.is_file():
        return
    if os.name != "nt":
        print("  [SKIP] not on Windows — cannot execute the bundled interpreter")
        return
    res = run([str(python_exe), "-c",
               "import sys, airlinesim, airlinesim.routedata as rd, airlinesim.server as sv;"
               "print(sys.executable);print(airlinesim.__version__);"
               "print(rd.DATA_DIR);print(sv.WEBUI_DIR)"])
    lines = res.stdout.strip().splitlines()
    fails.check(res.returncode == 0 and len(lines) >= 4,
                "bundled interpreter imports airlinesim", res.stdout + res.stderr)
    if res.returncode != 0 or len(lines) < 4:
        return
    exe, version, data_dir, webui_dir = lines[:4]
    root = str(bundle.resolve()).lower()
    fails.check(exe.lower().startswith(root), f"runs its own interpreter ({exe})")
    fails.check(str(Path(data_dir).resolve()).lower().startswith(root),
                f"route corpus resolves inside the bundle ({data_dir})")
    fails.check(Path(data_dir).is_dir() and any(Path(data_dir).iterdir()),
                "route corpus is populated")
    fails.check((Path(webui_dir) / "index.html").is_file(),
                "web UI files present")
    print(f"  [info] bundled airlinesim version: {version}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bundle", type=Path, default=None,
                   help="portable bundle directory to test (default: this interpreter)")
    p.add_argument("--skip-gui", action="store_true",
                   help="skip the HTTP server checks")
    p.add_argument("--demo-days", type=int, default=30)
    args = p.parse_args(argv)

    fails = Failures()

    if args.bundle:
        bundle = args.bundle.resolve()
        if not bundle.is_dir():
            raise SystemExit(f"--bundle is not a directory: {bundle}")
        launcher = bundle / "airlinesim.bat"
        if not launcher.is_file():
            raise SystemExit(f"no airlinesim.bat in {bundle}")
        print(f"[smoke] testing portable bundle: {bundle}")
        for expected in ("AirlineSim-GUI.bat", "AirlineSim-Demo.bat",
                         "Run-Checks.bat", "README-WINDOWS.txt", "BUILD-INFO.txt"):
            fails.check((bundle / expected).is_file(), f"bundle contains {expected}")
        check_bundle_is_self_contained(bundle, fails)
        if os.name != "nt":
            print("[smoke] not on Windows — cannot run the .bat launchers; "
                  "layout checks only")
            return report(fails)
        base_cmd = [str(launcher)]
    else:
        print(f"[smoke] testing installed package with {sys.executable}")
        base_cmd = [sys.executable, "-m", "airlinesim.cli"]

    print("\n[smoke] self-checking scenarios")
    for name in CHECKED_SCENARIOS:
        res = run(base_cmd + ["run", name])
        ok = res.returncode == 0 and PASS_MARKER in res.stdout
        fails.check(ok, f"scenario {name}: {PASS_MARKER}", res.stdout + res.stderr)

    print("\n[smoke] report-only scenarios (must not raise)")
    for name in SMOKE_SCENARIOS:
        res = run(base_cmd + ["run", name])
        fails.check(res.returncode == 0 and "Traceback" not in res.stderr,
                    f"scenario {name} ran", res.stdout + res.stderr)

    print("\n[smoke] CLI surface")
    res = run(base_cmd + ["list"])
    fails.check(res.returncode == 0 and "integration" in res.stdout,
                "list names the scenarios", res.stdout + res.stderr)

    res = run(base_cmd + ["demo", "--days", str(args.demo_days)])
    fails.check(res.returncode == 0, f"demo --days {args.demo_days}",
                res.stdout + res.stderr)

    res = run(base_cmd + ["demo", "--data", "--hub", "ORD", "--days", "10"])
    fails.check(res.returncode == 0, "data-driven demo (uses the shipped corpus)",
                res.stdout + res.stderr)

    res = run(base_cmd + ["refresh", "--check-only"])
    # --check-only reports staleness; a stale corpus is a real state, not a
    # build failure, so only a crash counts against us here.
    fails.check("Traceback" not in (res.stdout + res.stderr),
                "refresh --check-only ran", res.stdout + res.stderr)

    if not args.skip_gui:
        check_gui(base_cmd, fails)

    return report(fails)


def report(fails: Failures) -> int:
    print()
    if fails:
        print(f"SMOKE TEST FAILED — {len(fails)} check(s):")
        for label in fails:
            print(f"  - {label}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
