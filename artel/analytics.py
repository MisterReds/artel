"""Portfolio and agent analytics across every project.

Everything is derived from durable state (board.db + events.jsonl), so the
numbers are reproducible and match what the dashboard's event feed shows.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .project import Project, artel_home
from .runtimes import runtime_for

# USD per million tokens (input, output). Unknown models report no cost.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0), "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "scripted": (0.0, 0.0),
}

STAGES = ("planning", "execution", "review", "audit", "done", "waiting_human", "stopped", "paused_budget",
          "needs_review", "error")

_cache: dict = {}


def cost_usd(model: Optional[str], tokens_in: int, tokens_out: int) -> Optional[float]:
    p = PRICES.get(model or "")
    if p is None:
        return None
    return round(tokens_in / 1e6 * p[0] + tokens_out / 1e6 * p[1], 4)


def _events(project: Project) -> list[dict]:
    """Events, cached by log size so polling dashboards stay cheap."""
    path = project.log.path
    size = path.stat().st_size if path.exists() else 0
    key = str(path)
    hit = _cache.get(key)
    if hit and hit[0] == size:
        return hit[1]
    events = project.log.read()
    _cache[key] = (size, events)
    return events


def stage_of(project: Project, tasks: list[dict]) -> str:
    status = project.meta["status"]
    if status != "running":
        return status
    work = [t for t in tasks if t["kind"] == "work"]
    if not work:
        return "planning"
    if any(t["status"] in ("running", "ready", "todo") for t in work):
        return "execution"
    if any(t["status"] == "review" for t in work):
        return "review"
    if any(t["status"] == "blocked" and t["needs_human"] for t in work):
        return "waiting_human"
    return "audit"


def task_timelines(events: list[dict]) -> dict[str, list]:
    tl: dict[str, list] = defaultdict(list)
    for e in events:
        if e["kind"] == "board/task_created":
            tl[e["task_id"]].append({"ts": e["ts"], "to": "todo", "actor": e.get("created_by")})
        elif e["kind"] == "board/task_moved":
            tl[e["task_id"]].append({"ts": e["ts"], "frm": e["frm"], "to": e["to"], "actor": e.get("actor")})
    return tl


def _summary(e: dict) -> str:
    k = e["kind"]
    if k == "tool/call":
        return f"{e.get('actor')}: {e['tool']}"
    if k == "board/task_moved":
        return f"{e['task_id']} {e['frm']} → {e['to']}"
    if k == "review/verdict":
        return ("принято " if e["passed"] else "отклонено ") + e["task_id"]
    if k == "audit/verdict":
        return "аудит: " + ("цель достигнута" if e["complete"] else "не готово")
    if k == "board/comment":
        return f"{e['author']}: {e['body'][:80]}"
    if k == "agent/end":
        return f"{e.get('actor')}: {e.get('purpose')} — {e.get('status')}"
    if k == "manager/finished":
        return f"менеджер завершил: {e.get('status')}"
    if k == "project/status":
        return f"статус: {e.get('status')}"
    if k == "run/finished":
        return f"запуск #{e.get('run_id')}: {e.get('status')}"
    if k == "skills/reviewed":
        return "обновлена библиотека скиллов"
    if k in ("harness/refined", "harness/edited"):
        return "refine харнесса"
    return k


def project_stats(project: Project, live: bool = False) -> dict:
    meta = project.meta
    tasks = project.board.list()
    runs = project.board.runs()
    events = _events(project)
    work = [t for t in tasks if t["kind"] == "work" and t["status"] != "cancelled"]
    counts: dict[str, int] = defaultdict(int)
    for t in work:
        counts[t["status"]] += 1
    done = counts["done"]
    personas = {p.name: p for p in project.personas()}
    scripted = project.config.get("provider") == "scripted"
    default_model = "scripted" if scripted else project.config["models"]["default"]

    # per-task review history from the log
    rejections: dict[str, int] = defaultdict(int)
    for e in events:
        if e["kind"] == "review/verdict" and not e["passed"]:
            rejections[e["task_id"]] += 1

    workers = []
    for name, p in personas.items():
        mine = [t for t in work if t["assignee"] == name]
        my_runs = [r for r in runs if r["worker"] == name]
        accepted = [t for t in mine if t["status"] == "done"]
        first_pass = [t for t in accepted if rejections[t["id"]] == 0
                      and sum(1 for r in my_runs if r["task_id"] == t["id"]) == 1]
        tin = sum(r["tokens_in"] for r in my_runs)
        tout = sum(r["tokens_out"] for r in my_runs)
        recorded = sum(r.get("cost_usd") or 0 for r in my_runs)
        if scripted:
            model = "scripted"
        elif runtime_for(project.config, "worker") == "claude_code":
            model = "claude-code:" + (project.config.get("claude_code") or {}).get("model", "sonnet")
        elif runtime_for(project.config, "worker") == "codex":
            model = "codex:" + ((project.config.get("codex") or {}).get("model") or "default")
        elif runtime_for(project.config, "worker").split(":")[0] == "acp":
            rt = runtime_for(project.config, "worker")
            model = "acp:" + (rt.split(":", 1)[1] if ":" in rt else (project.config.get("acp") or {}).get("agent", "gemini"))
        else:
            model = p.model or project.config["models"].get("worker", default_model)
        workers.append({
            "name": name, "role": p.role, "model": model,
            "tasks": len(mine), "done": len(accepted), "first_pass": len(first_pass),
            "rejections": sum(rejections[t["id"]] for t in mine),
            "runs": len(my_runs),
            "stalls": sum(1 for r in my_runs if r["status"] in ("stalled", "turn_limit", "error", "refused")),
            "blocked": sum(1 for r in my_runs if r["status"] == "blocked"),
            "tokens_in": tin, "tokens_out": tout,
            "cost_usd": round(recorded, 4) if recorded else cost_usd(model, tin, tout),
            "busy_on": next((t["title"] for t in mine if t["status"] == "running"), None),
        })

    usage = meta["usage"]
    last = next((e for e in reversed(events) if e["kind"] not in ("llm/response", "tool/result", "agent/start",
                                                                       "manager/job_started", "manager/job_finished")),
                None)
    attention = [{"task_id": t["id"], "title": t["title"], "reason": "нужен человек" if t["needs_human"] else
                  "заблокирована", "assignee": t["assignee"]}
                 for t in work if t["status"] == "blocked"]
    return {
        "slug": project.slug, "name": meta["name"], "goal": meta["goal"], "status": meta["status"],
        "stage": stage_of(project, tasks), "live": live, "created_at": meta["created_at"],
        "updated_at": project.log.path.stat().st_mtime if project.log.path.exists() else meta["created_at"],
        "tasks_total": len(work), "tasks_done": done, "counts": dict(counts),
        "progress": round(done / len(work), 3) if work else 0.0,
        "audit_rounds": meta.get("audit_rounds", 0),
        "usage": usage, "cost_usd": round(usage["cost_usd"], 4) if usage.get("cost_usd") else
        cost_usd(default_model, usage["input_tokens"], usage["output_tokens"]),
        "workers": workers, "active_workers": sum(1 for w in workers if w["busy_on"]),
        "attention": attention,
        "last_event": {"ts": last["ts"], "text": _summary(last)} if last else None,
        "budget": _budget_state(project),
    }


def _budget_state(project: Project) -> Optional[dict]:
    b = project.config.get("budgets", {})
    cap = b.get("max_total_tokens") or 0
    if not cap:
        return None
    u = project.meta["usage"]
    used = u["input_tokens"] + u["output_tokens"]
    frac = used / cap
    return {"used": used, "cap": cap, "fraction": round(frac, 3),
            "alert": "exhausted" if frac >= 1 else "warning" if frac >= 0.8 else None}


def portfolio(config: dict, live: set = frozenset(), home: Optional[Path] = None) -> list[dict]:
    base = (home or artel_home()) / "projects"
    out = []
    if not base.exists():
        return out
    for d in base.iterdir():
        if (d / "project.json").exists():
            try:
                out.append(project_stats(Project(d, config), live=d.name in live))
            except Exception as exc:  # noqa: BLE001 - one broken project must not hide the rest
                out.append({"slug": d.name, "name": d.name, "status": "error", "stage": "error", "error": repr(exc),
                            "progress": 0, "tasks_total": 0, "tasks_done": 0, "workers": [], "attention": [],
                            "updated_at": time.time()})
    out.sort(key=lambda p: (not p.get("live"), -p.get("updated_at", 0)))
    return out


def agent_analytics(projects: list[dict]) -> dict:
    """Aggregate worker performance across projects by model and by role."""
    def bucket() -> dict:
        return {"workers": 0, "projects": set(), "tasks": 0, "done": 0, "first_pass": 0, "rejections": 0,
                "runs": 0, "stalls": 0, "tokens": 0, "cost_usd": 0.0, "cost_known": True}

    by_model: dict[str, dict] = defaultdict(bucket)
    by_role: dict[str, dict] = defaultdict(bucket)
    rows = []
    for p in projects:
        for w in p.get("workers", []):
            role_key = (w["role"] or "—").strip().lower()
            for agg in (by_model[w["model"]], by_role[role_key]):
                agg["workers"] += 1
                agg["projects"].add(p["slug"])
                for k in ("tasks", "done", "first_pass", "rejections", "runs", "stalls"):
                    agg[k] += w[k]
                agg["tokens"] += w["tokens_in"] + w["tokens_out"]
                if w["cost_usd"] is None:
                    agg["cost_known"] = False
                else:
                    agg["cost_usd"] += w["cost_usd"]
            rows.append({**w, "project": p["slug"], "project_name": p["name"]})

    def finish(d: dict) -> list[dict]:
        out = []
        for key, a in d.items():
            done = a["done"] or 0
            out.append({"key": key, "workers": a["workers"], "projects": len(a["projects"]), "tasks": a["tasks"],
                        "done": done, "first_pass_rate": round(a["first_pass"] / done, 3) if done else None,
                        "rejections": a["rejections"], "stall_rate": round(a["stalls"] / a["runs"], 3) if a["runs"] else None,
                        "tokens_per_done": round(a["tokens"] / done) if done else None,
                        "cost_per_done": round(a["cost_usd"] / done, 4) if done and a["cost_known"] else None})
        return sorted(out, key=lambda r: (-(r["done"] or 0), r["key"]))

    rows.sort(key=lambda r: (-r["done"], r["project"]))
    return {"by_model": finish(by_model), "by_role": finish(by_role), "workers": rows}
