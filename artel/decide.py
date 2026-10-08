"""System One: fast, typed decisions at the harness's choke points — the plug-in point for an external model.

Artel ships no System One model. A System One is connected in one of two ways:

- a plugin that provides the kernel service ``system_one`` — any object with ``ask(state, questions) -> answers``;
- an HTTP endpoint (``decide.backend: http``, ``decide.url``): POST ``{"state": {...}, "questions": {...}}``,
  response ``{"answers": {...}, "model": "...", "usage": {...}}``.

Questions are typed (see docs/SYSTEM_ONE.md): ``noul`` yes/no -> ``{"noul": p_yes}``; ``choice`` one of the given
options -> ``{"choice": key, "confidence": p, "probabilities": {...}}``; ``score`` ordered scale ->
``{"score": index, "confidence": p}``. Without a System One, or when it fails, every decision point falls back to
its default, so Artel behaves exactly as without it. Every decision is logged with latency and answers so its
usefulness can be measured.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx


class DecisionError(Exception):
    pass


def noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, scale: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": scale}


class HttpSystemOne:
    """A System One served over HTTP with the contract above."""

    def __init__(self, cfg: dict):
        self.url = cfg.get("url", "")
        self.key = os.environ.get(cfg.get("api_key_env") or "", "")
        self.model = cfg.get("model")
        from .tools import is_local_url

        # a local System One must not go through a system http_proxy
        self.client = httpx.Client(timeout=float(cfg.get("timeout_s", 10)), trust_env=not is_local_url(self.url))
        self.name = f"http:{self.url}"

    def ask(self, state: dict, questions: dict) -> dict:
        if not self.url:
            raise DecisionError("decide.url is not set")
        body = {"state": state, "questions": questions, **({"model": self.model} if self.model else {})}
        try:
            r = self.client.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"} if self.key else {})
        except httpx.HTTPError as exc:
            raise DecisionError(f"system one transport error: {exc}") from exc
        if r.status_code >= 400:
            raise DecisionError(f"system one HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        answers = dict(data.get("answers") or {})
        answers["_meta"] = {"model": data.get("model"), "usage": data.get("usage")}
        return answers


def system_one(kernel):
    """The connected System One, or None: a plugin's ``system_one`` service first, then the HTTP endpoint."""
    s1 = kernel.get("system_one")
    if s1 is not None:
        return s1
    cfg = kernel.config.get("decide") or {}
    if cfg.get("backend") == "http" and cfg.get("url"):
        s1 = HttpSystemOne(cfg)
        kernel.provide("system_one", s1)
        return s1
    return None


def decide(kernel, project, point: str, state: dict, questions: dict) -> Optional[dict]:
    """Ask at a named decision point; log it; return answers or None (the caller then uses its default)."""
    points = (kernel.config.get("decide") or {}).get("points") or {}
    if not points.get(point):
        return None
    s1 = system_one(kernel)
    if s1 is None:
        return None
    name = getattr(s1, "name", type(s1).__name__)
    t0 = time.time()
    try:
        answers = dict(s1.ask(state, questions))
    except Exception as exc:  # noqa: BLE001 - a failing System One must never stop the work
        project.log.append("decide/error", point=point, backend=name, error=str(exc)[:500])
        return None
    meta = answers.pop("_meta", {}) or {}
    project.log.append("decide/answer", point=point, backend=name, model=meta.get("model"),
                       latency_ms=round((time.time() - t0) * 1000), answers=answers, usage=meta.get("usage"))
    cost = (meta.get("usage") or {}).get("cost") or 0.0
    if cost:
        project.add_usage(0, 0, cost_usd=cost)
    return answers
