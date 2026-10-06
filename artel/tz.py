"""User time zone (ui.timezone, an IANA name) for every time Artel shows: CLI, notifications, dashboard."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


def valid_zone(name) -> bool:
    if not isinstance(name, str) or not name.strip() or name.startswith(("/", ".")) or ".." in name:
        return False
    try:
        ZoneInfo(name.strip())
    except Exception:  # ZoneInfoNotFoundError, ValueError, OSError for malformed keys
        return False
    return True


def configured_zone(config: Optional[dict]) -> str:
    z = ((config or {}).get("ui") or {}).get("timezone")
    return z.strip() if isinstance(z, str) and valid_zone(z) else ""


def system_zone() -> str:
    """Best-effort IANA name of the machine's zone; '' when it cannot be determined."""
    for cand in (os.environ.get("TZ", "").lstrip(":"),):
        if valid_zone(cand):
            return cand
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link and valid_zone(link.split("zoneinfo/", 1)[1]):
            return link.split("zoneinfo/", 1)[1]
        name = Path("/etc/timezone").read_text().strip()
        if valid_zone(name):
            return name
    except OSError:
        pass
    return ""


def zone_name(config: Optional[dict] = None) -> str:
    return configured_zone(config) or system_zone() or time.tzname[0] or "UTC"


def get_zone(config: Optional[dict] = None) -> tzinfo:
    z = configured_zone(config) or system_zone()
    if z:
        return ZoneInfo(z)
    return datetime.now().astimezone().tzinfo or timezone.utc


def fmt_ts(ts: float, fmt: str = "%Y-%m-%d %H:%M", config: Optional[dict] = None) -> str:
    return datetime.fromtimestamp(ts, get_zone(config)).strftime(fmt)


def info(config: Optional[dict] = None) -> dict:
    return {"configured": configured_zone(config), "effective": zone_name(config)}
