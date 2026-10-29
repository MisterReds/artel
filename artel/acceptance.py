"""Acceptance stand: the success metrics of docs/SPEC.md §1 and §5 computed from a project's event log.

``measure`` and ``check`` are pure (events, tasks and meta in, numbers out); ``measure_project`` reads a project,
``verify`` runs an independent final audit on the runtime the workers did not use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .manager import Manager, new_kernel
from .routing import TIERS

ENDED_BY_LIMIT = ("waiting_human", "error", "stopped")
CLOSED = ("done", "cancelled", "blocked")
TOKEN_NOTE = ("токены — как их сообщает рантайм: вход (с записью в кэш) + выход, чтения кэша отдельно (cached_tokens); если у части запусков рантайм "
              "не сообщил расход (usage_missing_runs), итог занижен")
ALL = 10 ** 9  # EventLog.read caps at 10k events by default; a long run has more
NO_INDEPENDENT = "нет независимого рантайма: подключите второй провайдер"


def _verify_event(e: dict) -> bool:
    return e.get("purpose") == "verify" or e.get("kind") == "acceptance/verdict"


def _hours(events: list[dict]) -> Optional[float]:
    """Sum of run segments: from a project/status "running" to the next status that is not "running" (or to the last
    event of the run when still open). Pauses between segments and the verification run are not counted."""
    total, start, last, seen = 0.0, None, None, False
    for e in events:
        if _verify_event(e) or e.get("ts") is None:
            continue
        if e.get("kind") == "project/status":
            if e.get("status") == "running":
                if start is None:
                    start, seen = e["ts"], True
            elif start is not None:
                total += e["ts"] - start
                start = None
        if start is not None:
            last = e["ts"]
    if start is not None and last is not None:
        total += last - start
    return round(total / 3600, 2) if seen else None


def _stopped_by_limit(events: list[dict], ended: Optional[str]) -> Optional[bool]:
    if ended is None or ended == "running":
        return None
    if ended not in ENDED_BY_LIMIT:
        return False
    limited = next((i for i in range(len(events) - 1, -1, -1) if events[i].get("kind") == "provider/limited"), None)
    if limited is None:
        return False
    for e in events[limited + 1:]:
        if e.get("kind") == "owner/stop":
            return False
        if e.get("kind") == "agent/end" and e.get("status") == "terminal" and not _verify_event(e):
            return False
    return True


def _share(part: int, parts: list) -> Optional[float]:
    total = sum(parts)
    return round(part / total, 3) if total else None


def measure(events: list[dict], tasks: list[dict], meta: dict) -> dict:
    ended = next((e.get("status") for e in reversed(events) if e.get("kind") == "project/status"), None)

    t = meta.get("tiers") or {}
    tokens = [int(t.get(f"{k}_tokens") or 0) for k in TIERS]
    calls = [int(t.get(f"{k}_calls") or 0) for k in TIERS]
    exp = TIERS.index("expensive")

    work = [x for x in tasks if x.get("kind") != "goal"]
    done = sum(1 for x in work if x.get("status") == "done")
    closed = sum(1 for x in work if x.get("status") in CLOSED)

    # One ask per transition: a task handed to the owner is a board/task_updated(needs_human); an escalation that is
    # tied to a task already produced that update, so only escalations without a task (goal audit, planning) add one.
    steps = owner_asks = owner_interventions = tokens_total = cached_total = missing_runs = 0
    verified = False
    by_runtime: dict[str, int] = {}
    for e in events:
        k = e.get("kind") or ""
        if _verify_event(e):
            verified = verified or k == "agent/end"
            continue
        if k == "agent/start":
            steps += 1
        elif k == "manager/escalated":
            owner_asks += not e.get("task_id")
        elif k == "board/task_updated":
            owner_asks += bool((e.get("fields") or {}).get("needs_human"))
        elif k == "board/comment":
            owner_interventions += e.get("author") == "human"
        elif k.startswith("owner/"):
            # confirm_intent is reserved for delivery 7 (a planned step, not an intervention); a resume right after an
            # answer is already counted through the answer's comment
            if k != "owner/confirm_intent" and not (k == "owner/resume" and e.get("after_answer")):
                owner_interventions += 1
        elif k == "agent/end":
            u = e.get("usage") or {}
            cached = int(u.get("cached") or 0)
            n = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
            cached_total += cached
            missing_runs += bool(e.get("usage_missing"))  # a run that reported no usage is counted, not taken as 0
            rt = e.get("runtime") or "native"
            by_runtime[rt] = by_runtime.get(rt, 0) + n
            tokens_total += n

    verdict = next(({f: v for f, v in e.items() if f not in ("seq", "ts", "kind")}
                    for e in reversed(events) if e.get("kind") == "acceptance/verdict"), None)
    return {
        "duration_h": _hours(events),
        "ended": ended,
        "stopped_by_limit": _stopped_by_limit(events, ended),
        "limit_waits": sum(1 for e in events if e.get("kind") == "provider/limited"),
        "expensive_token_share": _share(tokens[exp], tokens),
        "expensive_call_share": _share(calls[exp], calls),
        "tokens_total": tokens_total,
        "cached_tokens": cached_total,
        "usage_missing_runs": missing_runs,
        "tokens_incomplete": missing_runs > 0,
        "token_note": TOKEN_NOTE,
        "tasks_done": done,
        "tokens_per_accepted": round(tokens_total / done) if done else None,
        "success_rate": round(done / closed, 3) if closed else None,
        "steps": steps,
        "owner_asks": owner_asks,
        "owner_interventions": owner_interventions,
        "autonomy_share": round(1 - owner_asks / steps, 3) if steps else None,
        "tokens_by_runtime": by_runtime,
        "cost_usd": (meta.get("usage") or {}).get("cost_usd"),
        "cost_note": "includes verification" if verified else None,
        "decision_log_coverage": None,  # delivery 7
        "verdict": verdict,
    }


def check(m: dict, thresholds: dict) -> dict:
    """Compare metrics with thresholds: per threshold key ``{"value", "limit", "ok"}`` (ok None = no data)."""
    def cmp(metric: str, limit, below: bool) -> dict:
        v = m.get(metric)
        return {"value": v, "limit": limit, "ok": None if v is None else (v <= limit if below else v >= limit)}

    specs = {"expensive_token_share_max": ("expensive_token_share", True),
             "expensive_call_share_max": ("expensive_call_share", True),
             "autonomy_min": ("autonomy_share", False),
             "owner_interventions_max": ("owner_interventions", True),
             "hours_min": ("duration_h", False)}
    out = {}
    for key, limit in thresholds.items():
        if key in specs:
            out[key] = cmp(specs[key][0], limit, specs[key][1])
        elif key == "no_limit_stop":
            stopped = m.get("stopped_by_limit")
            out[key] = {"value": stopped, "limit": limit,
                        "ok": None if stopped is None else (not stopped if limit else True)}
    return out


def measure_project(project) -> dict:
    return measure(project.log.read(limit=ALL), project.board.list(), project.meta)


def load_scenario(name: str) -> dict:
    import yaml

    path = Path(__file__).parents[1] / "bench" / "acceptance" / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"сценарий приёмки {name!r} не найден: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


VERIFY_INSTRUCTION = (
    "\n\nThis is an independent verification by a runtime other than the one that did the work: do not trust the "
    "claims in the brief, check the deliverables against the goal yourself, and report only what you confirmed. "
    "Do not add tasks; list gaps in `missing`.")


def worker_runtimes(events: list[dict]) -> set:
    """Runtimes that did the work (workers and testers) in this log; a run without a runtime field is native."""
    return {e.get("runtime") or "native" for e in events
            if e.get("kind") == "agent/end" and e.get("role") in ("worker", "tester") and not _verify_event(e)}


def verify(project, config: dict) -> dict:
    """Independent final check: the auditor role on a connected runtime the workers did not use. Logs
    ``acceptance/verdict``; leaves the board and the project status alone."""
    from . import prompts
    from .runtimes import connected_runtimes, run_agent
    from .tooling import ToolContext

    events = project.log.read(limit=ALL)
    used = worker_runtimes(events)
    order = (config.get("acceptance") or {}).get("verifier_runtimes") or connected_runtimes(config)
    candidates = [r for r in order if r not in used]
    if not candidates:
        return {"error": NO_INDEPENDENT, "independent": False}
    # failover off: a verifier that is out on a limit must wait or fail, never hand the check to a worker's runtime
    cfg = {**config, "failover": False,
           "runtime_by_role": {**(config.get("runtime_by_role") or {}), "auditor": candidates[0]}}
    kernel = new_kernel(cfg)
    manager = Manager(kernel, project)
    seq = events[-1]["seq"] if events else 0
    try:
        brief = manager.audit_brief()
        ctx = ToolContext(kernel, project, role="auditor", workspace=project.shared)
        out = run_agent(kernel, project, role="auditor", system=prompts.AUDITOR + VERIFY_INSTRUCTION,
                        first_user=brief + "\n\nVerify independently now.", ctx=ctx, max_turns=10, purpose="verify")
    finally:
        manager.pool.shutdown(wait=False)
        kernel.shutdown(reason="verify_end")
    ran = [e for e in project.log.read(after=seq, limit=ALL) if e.get("kind") == "agent/end" and e.get("purpose") == "verify"]
    runtime = (ran[-1].get("runtime") or "native") if ran else candidates[0]
    independent = runtime not in used
    if out.name != "audit_verdict":
        return {"error": out.error or f"no verdict ({out.status})", "runtime": runtime, "independent": independent}
    v = out.payload
    result = {"runtime": runtime, "independent": independent, "complete": bool(v.get("complete")),
              "summary": v.get("summary", ""), "missing": v.get("missing") or []}
    project.log.append("acceptance/verdict", usage=out.usage, **result)
    return result


__all__ = ["measure", "check", "measure_project", "load_scenario", "verify"]
