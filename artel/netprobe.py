"""Is the network path to the model reachable? A TCP connect to the proxy (when one carries the traffic) or to the
provider host. Pure and cheap: the manager calls it while a project waits out a network outage."""

from __future__ import annotations

import os
import socket
from typing import Any
from urllib.parse import urlparse

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def _provider_url(config: dict) -> str:
    name = config.get("provider", "anthropic")
    spec = (config.get("providers") or {}).get(name)
    if spec:
        return spec.get("base_url", "")
    if name == "openai_compat":
        return (config.get("openai_compat") or {}).get("base_url", "https://api.deepseek.com/v1")
    return "https://api.anthropic.com"


def target(config: dict) -> tuple[str, int]:
    """(host, port) the traffic to the provider goes through: its own host if local, else the proxy, else the host."""
    from .tools import is_local_url

    url = _provider_url(config)
    if not is_local_url(url):
        for var in PROXY_VARS:
            if os.environ.get(var):
                p = urlparse(os.environ[var] if "//" in os.environ[var] else "//" + os.environ[var])
                if p.hostname:
                    return p.hostname, p.port or (443 if p.scheme == "https" else 8080 if p.scheme in ("", "http") else 1080)
    p = urlparse(url)
    return p.hostname or "", p.port or (80 if p.scheme == "http" else 443)


def reachable(config: dict, timeout: float = 5.0) -> tuple[bool, str]:
    host, port = target(config)
    if not host:
        return True, "no provider host to probe"
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True, f"{host}:{port}"
    except OSError as exc:
        return False, f"{host}:{port}: {exc}"
