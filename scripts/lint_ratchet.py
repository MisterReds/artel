#!/usr/bin/env python3
"""Ratchet gates for ruff and mypy (see scripts/ratchet.py for the semantics).

    python3 scripts/lint_ratchet.py ruff            # per (file, rule) counts vs scripts/ruff_baseline.json
    python3 scripts/lint_ratchet.py mypy            # per-file error counts vs scripts/mypy_baseline.json
    python3 scripts/lint_ratchet.py ruff --update-baseline [--allow-add]

mypy's strict zone (artel/contracts, artel/ports) must have zero errors and can never be baselined.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ratchet

STRICT_ZONE = ("artel/contracts/", "artel/ports/")
MYPY_LINE = re.compile(r"^(?P<file>[^:\n]+\.pyi?):\d+(?::\d+)?: error: .*$", re.M)


def rel(path: str, root: Path) -> str:
    p = Path(path)
    with contextlib.suppress(ValueError):  # outside the root: keep the path as given
        p = p.resolve().relative_to(root.resolve())
    return p.as_posix()


def ruff_counts(json_text: str, root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in json.loads(json_text or "[]"):
        key = f"{rel(item['filename'], root)}:{item['code']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def mypy_counts(text: str, root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in MYPY_LINE.finditer(text):
        key = rel(m.group("file"), root)
        counts[key] = counts.get(key, 0) + 1
    return counts


def run_tool(cmd: list[str], root: Path) -> str:
    """Run ruff or mypy and return its stdout; a missing or crashed tool is an error, never an empty result."""
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    name = cmd[2] if cmd[1] == "-m" else cmd[0]
    if "No module named" in proc.stderr:
        sys.exit(f"{name} is not installed: pip install -e '.[gates]'")
    if proc.returncode > 1:
        sys.exit(f"{name} failed with exit code {proc.returncode}:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tool", choices=["ruff", "mypy"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--allow-add", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    baseline_rel = f"scripts/{args.tool}_baseline.json"
    baseline_path = root / baseline_rel

    if args.tool == "ruff":
        current = ruff_counts(
            run_tool([sys.executable, "-m", "ruff", "check", "--output-format", "json", "--no-cache"], root), root
        )
    else:
        out = run_tool([sys.executable, "-m", "mypy", "--no-error-summary", "--no-color-output", "--no-pretty"], root)
        current = mypy_counts(out, root)
        strict = {k: n for k, n in current.items() if k.startswith(STRICT_ZONE)}
        if strict:
            print("mypy: errors in the strict zone (must be zero, never baselined):")
            for line in out.splitlines():
                if line.startswith(STRICT_ZONE) and ": error:" in line:
                    print(f"  {line}")
            return 1
    if args.update_baseline:
        return ratchet.update(args.tool, baseline_path, current, allow_add=args.allow_add)
    return ratchet.report(args.tool, current, ratchet.load(baseline_path), baseline_rel)


if __name__ == "__main__":
    sys.exit(main())
