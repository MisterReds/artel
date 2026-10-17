"""Model tiers: the expensive model only manages, cheap models do the work (docs/ORCHESTRATION.md).

Every role belongs to a tier — ``expensive`` (the Manager: plan/decompose, decide on escalations, final acceptance),
``medium`` (Executor and Critic: code, review) or ``cheap`` (Curator and support roles: context, skills, diagnosis,
routine). A runtime turns a tier into its own model name (Claude Code: opus / sonnet / haiku).

Long horizon: the expensive tier has an explicit token budget per project. Near it (``degrade_at``) the expensive
roles move to the medium tier and the work goes on — quality may drop, the run never stops for it. Expensive usage
is counted from the project's own events, so a resumed project keeps its account.
"""

from __future__ import annotations

from typing import Optional

TIERS = ("cheap", "medium", "expensive")

DEFAULTS = {
    "enabled": True,
    # Manager = expensive; Architect/Executor/Critic = medium; Curator and routine = cheap
    "roles": {"planner": "expensive", "manager": "expensive", "auditor": "expensive",
              "worker": "medium", "reviewer": "medium", "tester": "medium", "absorber": "medium", "evolver": "medium",
              "librarian": "cheap", "refiner": "cheap", "doctor": "cheap", "curator": "cheap", "intake": "cheap"},
    # model names per runtime; a missing name keeps the runtime's own configured model
    "models": {"claude_code": {"cheap": "haiku", "medium": "sonnet", "expensive": "opus"},
               "native": {},             # set in artel.yaml: tiers.models.native.{cheap,medium,expensive}
               "codex": {}},
    "expensive_budget_tokens": 600_000,   # per project: input + output of every expensive call
    "degrade_at": 0.9,                    # share of that budget after which expensive roles run on medium
    "simple_planner_on_medium": True,     # a request the intake judged simple is planned on the medium tier
}


def settings(config: dict) -> dict:
    raw = config.get("tiers") or {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k not in ("roles", "models")}}
    out["roles"] = {**DEFAULTS["roles"], **(raw.get("roles") or {})}
    out["models"] = {rt: {**DEFAULTS["models"].get(rt, {}), **((raw.get("models") or {}).get(rt) or {})}
                     for rt in {*DEFAULTS["models"], *(raw.get("models") or {})}}
    return out


def expensive_used(project) -> int:
    """Tokens the expensive tier has spent in this project (from its agent/end events)."""
    if project is None:
        return 0
    used = (project.meta.get("tiers") or {}).get("expensive_tokens")
    if used is not None:
        return int(used)
    total = 0
    for e in project.log.read():
        if e.get("kind") == "agent/end" and e.get("tier") == "expensive":
            u = e.get("usage") or {}
            total += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
    return total


def simple_request(project) -> bool:
    """The intake judged the request simple — trusted only when the request is short and the contract small (the
    cheap model's verdict comes from the request text, which could simply claim it)."""
    c = project.meta.get("contract") or {}
    return (c.get("complexity") == "low" and len(project.goal or "") <= 600
            and len(c.get("deliverables") or []) <= 3 and len(c.get("constraints") or []) <= 3)


def tier_for(config: dict, role: str, project=None, budget: bool = True) -> str:
    """The tier a role runs on now: its configured tier; the planner on medium for a simple request; medium for the
    expensive roles once the expensive budget is nearly spent (``budget=False`` leaves that last rule out)."""
    s = settings(config)
    tier = s["roles"].get(role, "medium")
    if role == "planner" and tier == "expensive" and project is not None and s["simple_planner_on_medium"] \
            and simple_request(project):
        tier = "medium"  # the architect's tier is not needed for a simple request
    if not budget:
        return tier
    if tier == "expensive" and project is not None and s["expensive_budget_tokens"]:
        if expensive_used(project) >= float(s["degrade_at"]) * float(s["expensive_budget_tokens"]):
            return "medium"
    return tier


def model_for(config: dict, runtime: str, role: str, project=None) -> Optional[str]:
    """The model name for this role on this runtime, or None to keep the runtime's own model."""
    s = settings(config)
    if not s["enabled"]:
        return None
    return (s["models"].get(runtime) or {}).get(tier_for(config, role, project)) or None


def record(project, tier: str, usage: dict) -> None:
    """Add a finished run's tokens to the project's per-tier account (kept in project meta)."""
    if project is None or not tier:
        return
    t = dict(project.meta.get("tiers") or {})
    tokens = int((usage or {}).get("input_tokens") or 0) + int((usage or {}).get("output_tokens") or 0)
    t[f"{tier}_tokens"] = int(t.get(f"{tier}_tokens") or 0) + tokens
    t[f"{tier}_calls"] = int(t.get(f"{tier}_calls") or 0) + 1
    project.set_meta(tiers=t)


def efficiency(project) -> dict:
    """The long-horizon metrics of a project: expensive share of tokens, tokens per accepted task, escalation rate
    (manager decisions per task), success rate, and how long it ran before the first provider limit."""
    t = project.meta.get("tiers") or {}
    tokens = {k: int(t.get(f"{k}_tokens") or 0) for k in TIERS}
    calls = {k: int(t.get(f"{k}_calls") or 0) for k in TIERS}
    total = sum(tokens.values())
    first = last = limited = None
    replans = degraded = 0
    for e in project.log.read():
        first = e["ts"] if first is None else first
        last = e["ts"]
        k = e.get("kind")
        if k == "provider/limited" and limited is None:
            limited = e["ts"]
        elif k == "agent/start" and e.get("purpose") == "replan":
            replans += 1
        elif k == "tier/usage" and e.get("degraded"):
            degraded += 1
    tasks = [x for x in project.board.list() if x["kind"] != "goal"]
    done = sum(1 for x in tasks if x["status"] == "done")
    closed = sum(1 for x in tasks if x["status"] in ("done", "cancelled", "blocked"))
    s = settings(project.config)
    return {"tokens": tokens, "calls": calls,
            "expensive_share": round(tokens["expensive"] / total, 3) if total else None,
            "expensive_call_share": round(calls["expensive"] / sum(calls.values()), 3) if sum(calls.values()) else None,
            "expensive_budget": s["expensive_budget_tokens"], "degraded_runs": degraded,
            "tokens_per_done": round(total / done) if done else None,
            "escalation_rate": round(replans / len(tasks), 3) if tasks else None,
            "success_rate": round(done / closed, 3) if closed else None,
            "hours_to_limit": round(((limited or last or 0) - (first or 0)) / 3600, 2) if first else None,
            "hit_limit": limited is not None}


__all__ = ["TIERS", "settings", "tier_for", "model_for", "record", "expensive_used", "efficiency"]
