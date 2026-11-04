"""Ratchet baselines shared by the engineering gates (archcheck, ruff, mypy).

A baseline maps a violation key to how many times it occurs today. The gate fails on

* a NEW violation: a key that is absent from the baseline, or occurs more often than recorded;
* a STALE entry: a key that occurs less often than recorded (or no longer occurs). The fix is to delete or lower
  the entry, so the baseline only ever shrinks.

``--update-baseline`` rewrites the file from the current state. It refuses to add or raise an entry unless
``--allow-add`` is given as well; use that only in a reviewed change whose diff you expect to grow the debt.
"""

from __future__ import annotations

import json
from pathlib import Path


def load(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in data.items()}


def dump(path: Path, entries: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: entries[k] for k in sorted(entries)}
    path.write_text(json.dumps(ordered, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def diff(current: dict[str, int], baseline: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Return (new, stale): key -> excess count over the baseline, key -> count the baseline can drop."""
    new = {k: n - baseline.get(k, 0) for k, n in current.items() if n > baseline.get(k, 0)}
    stale = {k: b - current.get(k, 0) for k, b in baseline.items() if b > current.get(k, 0)}
    return new, stale


def report(label: str, current: dict[str, int], baseline: dict[str, int], baseline_path: str) -> int:
    """Print the result for one gate; return the process exit code (0 = clean)."""
    new, stale = diff(current, baseline)
    if new:
        print(f"{label}: {len(new)} NEW violation(s) not in the baseline (fix them; do not add them):")
        for key in sorted(new):
            print(f"  + {key}" + (f"  (x{new[key]} more than recorded)" if key in baseline else ""))
    if stale:
        print(f"{label}: {len(stale)} baseline entr(y/ies) no longer occur: delete or lower them in {baseline_path}:")
        for key in sorted(stale):
            now = current.get(key, 0)
            hint = "delete" if now == 0 else f"lower to {now}"
            print(f"  - {key}  ({hint}; run with --update-baseline)")
    if not new and not stale:
        print(f"{label}: ok ({sum(current.values())} known violation(s) in the baseline, none new)")
        return 0
    return 1


def update(label: str, path: Path, current: dict[str, int], *, allow_add: bool) -> int:
    """Rewrite the baseline from the current state; refuse to add or raise entries without ``allow_add``."""
    new, _ = diff(current, load(path))
    if new and not allow_add:
        print(f"{label}: refusing to update the baseline, it would ADD {len(new)} entr(y/ies):")
        for key in sorted(new):
            print(f"  + {key}")
        print("Fix the violations. Only a reviewed change may add debt: rerun with --update-baseline --allow-add.")
        return 1
    dump(path, current)
    print(f"{label}: baseline written to {path} ({len(current)} entries)")
    return 0
