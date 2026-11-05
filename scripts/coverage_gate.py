#!/usr/bin/env python3
"""Coverage ratchet: total (line + branch) coverage must not fall below scripts/coverage_threshold.txt, and the
threshold itself may only go up.

    python3 scripts/coverage_gate.py check coverage.json        # fail below the stored threshold
    python3 scripts/coverage_gate.py raise coverage.json        # store floor(total) - 1 when that is higher
    python3 scripts/coverage_gate.py monotonic [--base REF]     # fail if the threshold is lower than at REF

``coverage.json`` comes from ``coverage run --branch -m pytest && coverage json``.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THRESHOLD_FILE = ROOT / "scripts" / "coverage_threshold.txt"
MARGIN = 1  # percentage points kept below the measured total


def read_threshold(text: str) -> float:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return float(line)
    raise ValueError("no threshold in the file")


def total(path: str) -> float:
    return float(json.loads(Path(path).read_text())["totals"]["percent_covered"])


def check(measured: float, threshold: float) -> int:
    if measured + 1e-9 < threshold:
        print(f"coverage: {measured:.2f}% is below the threshold {threshold:.2f}%")
        return 1
    print(f"coverage: ok, {measured:.2f}% (threshold {threshold:.2f}%)")
    headroom = math.floor(measured) - MARGIN
    if headroom > threshold:
        print(
            f"coverage: the threshold can go up to {headroom}: python3 scripts/coverage_gate.py raise <coverage.json>"
        )
    return 0


def monotonic(current: float, base: float) -> int:
    if current + 1e-9 < base:
        print(f"coverage: the threshold fell from {base} to {current}; it may only go up")
        return 1
    print(f"coverage: threshold {current} is not below the base {base}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["check", "raise", "monotonic"])
    ap.add_argument("coverage", nargs="?", default="coverage.json")
    ap.add_argument("--base", default="origin/master")
    args = ap.parse_args(argv)
    threshold = read_threshold(THRESHOLD_FILE.read_text())
    if args.action == "check":
        return check(total(args.coverage), threshold)
    if args.action == "raise":
        new = float(math.floor(total(args.coverage)) - MARGIN)
        if new <= threshold:
            print(f"coverage: threshold stays {threshold} (measured floor - {MARGIN} = {new})")
            return 0
        lines = THRESHOLD_FILE.read_text().splitlines()
        out = [str(int(new)) if ln.strip() and not ln.lstrip().startswith("#") else ln for ln in lines]
        THRESHOLD_FILE.write_text("\n".join(out) + "\n")
        print(f"coverage: threshold raised {threshold} -> {new}")
        return 0
    proc = subprocess.run(
        ["git", "show", f"{args.base}:scripts/coverage_threshold.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        print(f"coverage: no threshold at {args.base} yet; nothing to compare")
        return 0
    return monotonic(threshold, read_threshold(proc.stdout))


if __name__ == "__main__":
    sys.exit(main())
