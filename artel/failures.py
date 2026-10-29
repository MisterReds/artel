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
                   r"api key .*not set|missing api key|no api key|MISSING_CREDENTIAL|INVALID_CREDENTIAL|"
                   r"sandbox cannot start commands|bwrap: .*Operation not permitted", re.I)
NETWORK = re.compile(r"connection refused|connection reset|connect ?error|connect ?timeout|read ?timeout|timed out|"
                     r"proxy ?error|cannot connect to proxy|api ?connection ?error|connection error|"
                     r"name or service not known|temporary failure in name resolution|getaddrinfo failed|"
                     r"network is unreachable|no route to host|remote ?protocol ?error|server disconnected", re.I)


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
    """'limit' (wait for the provider), 'network' (proxy/provider unreachable: wait and probe), 'setup' (a person
    must fix the environment) or 'work' (retry may help)."""
    if LIMIT.search(text or ""):
        return "limit"
    if NETWORK.search(text or ""):
        return "network"
    if SETUP.search(text or ""):
        return "setup"
    return "work"


# A gate that cannot run here says nothing about the work: the tool is absent, the sandbox cannot start, the
# owner's proxy swallows loopback traffic.
ENVIRONMENT = re.compile(
    r"command not found|: not found\b|executable file not found|setting up uid map|bwrap: .*(permission denied|"
    r"operation not permitted|failed)|sandbox-exec: |sandbox_apply|sandbox cannot start commands|"
    r"proxyerror|tunnel connection failed|proxy.{0,80}(127\.0\.0\.1|localhost|::1)|"
    r"(127\.0\.0\.1|localhost|::1).{0,80}proxy", re.I)


def gate_failure_class(gates: list) -> Optional[str]:
    """None when every gate passed; 'environment' when every failing gate failed because of the machine (exit
    126/127, a missing program, a sandbox that cannot start, a proxy in front of loopback); otherwise 'work'."""
    failing = [g for g in gates if g.get("exit") != 0]
    if not failing:
        return None
    env = all(g.get("exit") in (126, 127) or ENVIRONMENT.search(g.get("output") or "") for g in failing)
    return "environment" if env else "work"
