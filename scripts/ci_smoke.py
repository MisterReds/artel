#!/usr/bin/env python3
"""First-run smoke check of an installed Artel, the same on Linux, macOS and Windows (used by CI).

  python scripts/ci_smoke.py [--artel PATH] [--port 8799] [--timeout 20] [--logs DIR]

1. `artel run "любая цель" --provider scripted` must exit 0 and report `status: done`;
2. `artel web --port PORT` must answer GET /api/ready within --timeout seconds; it is stopped afterwards.

`--artel` is the installed command (default: `artel` on PATH). Output of both steps goes to --logs (default: a
temporary directory) so a failing CI job can upload it. Standard library only; exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

GOAL = "любая цель"


def say(msg: str) -> None:
    print(f"[ci_smoke] {msg}", flush=True)


def child_env() -> dict:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def first_run(artel: str, logs: Path, timeout: float) -> bool:
    say(f"{artel} run {GOAL!r} --provider scripted")
    t0 = time.time()
    try:
        p = subprocess.run([artel, "run", GOAL, "--provider", "scripted"], capture_output=True, timeout=timeout,
                           env=child_env())
    except subprocess.TimeoutExpired as e:
        (logs / "first-run.log").write_bytes((e.stdout or b"") + b"\n--- stderr ---\n" + (e.stderr or b""))
        say(f"FAIL: the scripted run did not finish in {timeout:.0f} s")
        return False
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    (logs / "first-run.log").write_text(out + "\n--- stderr ---\n" + err, encoding="utf-8")
    status = next((ln.split(":", 1)[1].strip() for ln in reversed(out.splitlines()) if ln.startswith("status:")), "")
    say(f"exit {p.returncode}, status {status or '?'} in {time.time() - t0:.1f} s")
    if p.returncode != 0 or status != "done":
        say("FAIL: the scripted first run must end with status done\n" + (out + err)[-3000:])
        return False
    return True


def dashboard(artel: str, port: int, logs: Path, timeout: float) -> bool:
    url = f"http://127.0.0.1:{port}/api/ready"
    say(f"{artel} web --port {port}; waiting for {url}")
    log = open(logs / "web.log", "wb")
    proc = subprocess.Popen([artel, "web", "--port", str(port)], stdout=log, stderr=subprocess.STDOUT,
                            env=child_env())
    t0 = time.time()
    ok = False
    try:
        while time.time() - t0 < timeout:
            if proc.poll() is not None:
                say(f"FAIL: the dashboard exited with {proc.returncode}")
                break
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    body = json.loads(r.read())
                say(f"ready in {time.time() - t0:.1f} s: {body}")
                ok = True
                break
            except (OSError, ValueError):
                time.sleep(0.5)
        else:
            say(f"FAIL: no answer from /api/ready in {timeout:.0f} s")
    finally:
        if proc.poll() is None:
            if sys.platform == "win32":  # the console-script launcher has the Python process as a child
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        log.close()
        say("dashboard stopped")
    if not ok:
        print((logs / "web.log").read_text(encoding="utf-8", errors="replace")[-3000:])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artel", default=os.environ.get("ARTEL_CMD") or shutil.which("artel"))
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds for the dashboard to answer")
    ap.add_argument("--run-timeout", type=float, default=600.0, help="seconds for the scripted run")
    ap.add_argument("--logs", default="")
    a = ap.parse_args()
    if not a.artel:
        say("FAIL: `artel` is not on PATH; pass --artel")
        return 2
    logs = Path(a.logs) if a.logs else Path(tempfile.mkdtemp(prefix="artel-smoke-"))
    logs.mkdir(parents=True, exist_ok=True)
    say(f"artel: {a.artel}; logs: {logs}")
    ok = first_run(a.artel, logs, a.run_timeout) and dashboard(a.artel, a.port, logs, a.timeout)
    say("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
