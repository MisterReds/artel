"""System One decisions: fast, typed, probabilistic answers at harness choke points.

Backend ``jev`` calls TypeSafe Jev through OpenRouter's Decisions API: you send a
``state`` dict and named questions (``noul`` yes/no, ``choice`` one of options,
``score`` ordered scale); it answers all of them in one parallel pass with
probabilities, in ~70-500 ms, billing input tokens only. Jev returns no text and
cannot invent options, which is exactly what routing/gating needs.

Backend ``local`` approximates the same contract with a small local model
(llama.cpp); ``none`` returns the configured defaults. Every decision is logged
with its probabilities so its usefulness can be measured by ``artel evolve``.

Thresholds belong to the decision point and should reflect the cost of each
mistake (e.g. skipping a skill review wrongly is cheap; skipping a reviewer is not).
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"


class DecisionError(Exception):
    pass


def noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, scale: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": scale}


class Decider:
    def __init__(self, config: dict):
        cfg = config.get("decide") or {}
        self.backend = cfg.get("backend", "none")
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.url = cfg.get("url", DECISIONS_URL)
        self.key = os.environ.get(cfg.get("api_key_env", "OPENROUTER_API_KEY"), "")
        self.timeout = float(cfg.get("timeout_s", 10))
        self.client = httpx.Client(timeout=self.timeout)

    @property
    def available(self) -> bool:
        if self.backend == "jev":
            return bool(self.key)
        if self.backend == "local":
            from . import local

            return local.healthy()
        return False

    def ask(self, state: dict, questions: dict) -> dict:
        """Answers keyed like ``questions``; raises DecisionError when the backend fails."""
        if self.backend == "jev":
            return self._jev(state, questions)
        if self.backend == "local":
            return self._local(state, questions)
        raise DecisionError("no decision backend configured")

    def _jev(self, state: dict, questions: dict) -> dict:
        if not self.key:
            raise DecisionError("OPENROUTER_API_KEY is not set")
        body = {"model": self.model, "state": state, "questions": questions}
        try:
            r = self.client.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError as exc:
            raise DecisionError(f"jev transport error: {exc}") from exc
        if r.status_code >= 400:
            raise DecisionError(f"jev HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        answers = data.get("answers") or {}
        answers["_meta"] = {"model": data.get("model"), "usage": data.get("usage"), "id": data.get("id")}
        return answers

    def _local(self, state: dict, questions: dict) -> dict:
        """Approximate typed answers with a small local model (one call per question)."""
        from . import local

        ctx = "\n".join(f"{k}: {str(v)[:4000]}" for k, v in state.items())
        out: dict[str, Any] = {}
        for name, q in questions.items():
            crit = q["criteria"]
            if q["type"] == "noul":
                raw = local.ask(f"{q['instructions']}\nYES means: {crit['true']}\nNO means: {crit['false']}\n\n"
                                f"{ctx}\n\nAnswer YES or NO.", local.GATE_SYSTEM, max_tokens=3)
                if raw is None:
                    raise DecisionError("local model unavailable")
                out[name] = {"type": "noul", "noul": 0.15 if raw.strip().upper().startswith("NO") else 0.85}
            elif q["type"] == "choice":
                opts = "\n".join(f"- {k}: {v}" for k, v in crit.items())
                raw = local.ask(f"{q['instructions']}\nOptions:\n{opts}\n\n{ctx}\n\nAnswer with one option key only.",
                                max_tokens=8)
                if raw is None:
                    raise DecisionError("local model unavailable")
                pick = next((k for k in crit if k in raw.lower()), next(iter(crit)))
                out[name] = {"type": "choice", "choice": pick, "confidence": 0.5,
                             "probabilities": {k: (0.7 if k == pick else 0.3 / max(1, len(crit) - 1)) for k in crit}}
            else:
                scale = "\n".join(f"{i}: {c}" for i, c in enumerate(crit))
                raw = local.ask(f"{q['instructions']}\nScale:\n{scale}\n\n{ctx}\n\nAnswer with the number only.",
                                max_tokens=3)
                if raw is None:
                    raise DecisionError("local model unavailable")
                digits = [int(c) for c in raw if c.isdigit()]
                idx = min(digits[0], len(crit) - 1) if digits else len(crit) // 2
                out[name] = {"type": "score", "score": float(idx), "confidence": 0.5,
                             "legend": {str(i): c for i, c in enumerate(crit)}}
        out["_meta"] = {"model": "local"}
        return out


def decide(kernel, project, point: str, state: dict, questions: dict) -> Optional[dict]:
    """Ask at a named decision point; log it; return answers or None (caller uses its default)."""
    points = (kernel.config.get("decide") or {}).get("points") or {}
    if not points.get(point):
        return None
    decider = kernel.get("decider")
    if decider is None:
        decider = Decider(kernel.config)
        kernel.provide("decider", decider)
    t0 = time.time()
    try:
        answers = decider.ask(state, questions)
    except DecisionError as exc:
        project.log.append("decide/error", point=point, backend=decider.backend, error=str(exc))
        return None
    meta = answers.pop("_meta", {})
    project.log.append("decide/answer", point=point, backend=decider.backend, model=meta.get("model"),
                       latency_ms=round((time.time() - t0) * 1000), answers=answers, usage=meta.get("usage"))
    cost = ((meta.get("usage") or {}).get("cost")) or 0.0
    if cost:
        project.add_usage(0, 0, cost_usd=cost)
    return answers
