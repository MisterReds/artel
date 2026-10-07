"""Classifying run failures: the code under work, the provider, or the setup.

Used by the manager (what to do with a failed task) and by evolution (never judge a candidate on a provider
outage). Retrying helps only for the first kind.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

LIMIT = re.compile(r"session limit|usage limit|rate limit|hit your [a-z ]*limit|overloaded_error|credit balance|"
                   r"insufficient balance|quota exceeded|resource_exhausted|exhausted your capacity|too many requests|"
                   r"\b429\b|\b529\b", re.I)
SETUP = re.compile(r"не установлен|not installed|command not found|no such file or directory|not logged in|"
                   r"invalid api key|authentication_error|authentication failed|unauthorized|\b401\b|"
                   r"api key .*not set|missing api key|no api key|MISSING_CREDENTIAL|INVALID_CREDENTIAL", re.I)


def retry_at(text: str) -> Optional[float]:
    """Unix time of 'resets 4:20pm (UTC)' / 'Try again at 4:20 PM' in a provider message, if any."""
    r = re.search(r"(?:resets|try again at)\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text or "", re.I)
    if not r:
        return None
    h, mi = int(r.group(1)), int(r.group(2) or 0)
    ampm = (r.group(3) or "").lower()
    if ampm:
        h = h % 12 + (12 if ampm == "pm" else 0)
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t.timestamp()


def classify(text: str) -> str:
    """'limit' (wait for the provider), 'setup' (a person must fix the environment) or 'work' (retry may help)."""
    if LIMIT.search(text or ""):
        return "limit"
    if SETUP.search(text or ""):
        return "setup"
    return "work"
