"""Which runtimes are temporarily out (a provider's usage limit) — shared by every Artel process on this machine.

`auto` skips a limited runtime until its reset, so work moves to the next connected executor instead of waiting.
Stored in ARTEL_HOME/limits.json as {runtime: unix_time_until}.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


def _path() -> Path:
    from .project import artel_home

    return artel_home() / "limits.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    now = time.time()
    return {k: float(v) for k, v in data.items() if float(v) > now}


def mark(runtime: str, until: Optional[float], reason: str = "") -> float:
    until = until or time.time() + 900
    data = load()
    data[runtime] = max(until, data.get(runtime, 0))
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(p)
    return data[runtime]


def until(runtime: str) -> Optional[float]:
    return load().get(runtime)


def clear(runtime: Optional[str] = None) -> None:
    data = {} if runtime is None else {k: v for k, v in load().items() if k != runtime}
    p = _path()
    if p.exists() or data:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
