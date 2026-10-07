"""Diagnostics: code health plus a failure/efficiency profile mined from trajectories.

``doctor(code, projects)`` returns a JSON-able report and renders Markdown. The
report is the evidence the evolver uses to propose improvements.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional


def code_health(code: Path, timeout: int = 900) -> dict:
    env = {**os.environ, "PYTHONPATH": str(code)}
    env.pop("ARTEL_HOME", None)
    comp = subprocess.run([sys.executable, "-m", "compileall", "-q", "artel"], cwd=code, capture_output=True,
                          text=True, env=env, encoding="utf-8", errors="replace")
    try:
        t = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                           cwd=code, capture_output=True, text=True, env=env, timeout=timeout, encoding="utf-8", errors="replace")
        tail = (t.stdout + t.stderr)[-2500:]
        m_pass = re.search(r"(\d+) passed", tail)
        m_fail = re.search(r"(\d+) failed", tail)
        m_err = re.search(r"(\d+) error", tail)
        tests = {"exit": t.returncode, "passed": int(m_pass.group(1)) if m_pass else 0,
                 "failed": int(m_fail.group(1)) if m_fail else 0, "errors": int(m_err.group(1)) if m_err else 0,
                 "tail": tail}
    except subprocess.TimeoutExpired:
        tests = {"exit": -1, "passed": 0, "failed": 0, "errors": 1, "tail": "pytest timed out"}
    loc = sum(len(p.read_text(errors="replace", encoding="utf-8").splitlines()) for p in (code / "artel").rglob("*.py"))
    return {"compile_ok": comp.returncode == 0, "compile_errors": comp.stdout[-1500:], "tests": tests,
            "python_loc": loc, "ok": comp.returncode == 0 and tests["exit"] == 0}


def _events(pdir: Path) -> list[dict]:
    p = pdir / "events.jsonl"
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()] if p.exists() else []


def trajectory_profile(project_dirs: Iterable[Path]) -> dict:
    stalls, errors, refusals = Counter(), Counter(), 0
    rejections: list[str] = []
    gate_fail = Counter()
    tool_err = Counter()
    tool_err_samples: dict = defaultdict(list)
    tool_use = Counter()
    continuations = Counter()
    decisions = Counter()
    role_cost: dict = defaultdict(float)
    role_turns: dict = defaultdict(int)
    audits_incomplete = []
    projects = 0
    status = Counter()
    for pdir in project_dirs:
        pdir = Path(pdir)
        if not (pdir / "project.json").exists():
            continue
        projects += 1
        status[json.loads((pdir / "project.json").read_text(encoding="utf-8"))["status"]] += 1
        for e in _events(pdir):
            k = e["kind"]
            if k == "agent/end":
                role_cost[e.get("role", "?")] += float(e.get("cost_usd") or 0)
                role_turns[e.get("role", "?")] += int(e.get("turns") or 0)
                if e.get("status") in ("stalled", "turn_limit"):
                    stalls[e.get("role", "?")] += 1
                elif e.get("status") == "error":
                    errors[(e.get("error") or "")[:120]] += 1
                elif e.get("status") == "refused":
                    refusals += 1
            elif k == "review/verdict" and not e["passed"]:
                rejections.append(e["feedback"][:400])
            elif k == "gate/result" and e["exit"] != 0:
                gate_fail[e["command"][:120]] += 1
            elif k == "tool/call":
                tool_use[e["tool"]] += 1
            elif k == "tool/result" and e["is_error"]:
                tool_err[e["tool"]] += 1
                if len(tool_err_samples[e["tool"]]) < 3:
                    tool_err_samples[e["tool"]].append(e["output"][:200])
            elif k == "agent/continuation":
                continuations[e.get("actor", "?")] += 1
            elif k == "manager/decision":
                decisions[e.get("action", "?")] += 1
            elif k == "audit/verdict" and not e["complete"]:
                audits_incomplete.append(e.get("summary", "")[:300])
    words = Counter(w for f in rejections for w in re.findall(r"[a-zа-яё]{5,}", f.lower()))
    total_cost = sum(role_cost.values()) or 0.0
    return {
        "projects": projects, "status": dict(status),
        "stalls_by_role": dict(stalls), "errors": dict(errors.most_common(10)), "refusals": refusals,
        "rejections": len(rejections), "rejection_samples": rejections[:8],
        "rejection_terms": words.most_common(15),
        "gate_failures": dict(gate_fail.most_common(10)),
        "tool_errors": dict(tool_err.most_common(10)), "tool_error_samples": dict(tool_err_samples),
        "tool_use": dict(tool_use.most_common(15)),
        "continuations": dict(continuations), "replan_decisions": dict(decisions),
        "audits_incomplete": audits_incomplete[:5],
        "cost_by_role": {r: round(c, 4) for r, c in sorted(role_cost.items(), key=lambda x: -x[1])},
        "cost_share": {r: round(c / total_cost, 3) for r, c in role_cost.items()} if total_cost else {},
        "turns_by_role": dict(role_turns),
    }


def findings(health: Optional[dict], prof: dict, bench: Optional[dict] = None) -> list[dict]:
    """Ranked, human-readable problems with the evidence behind each."""
    out = []
    if health and not health["ok"]:
        out.append({"severity": "critical", "area": "code", "title": "tests or compilation fail",
                    "evidence": health["tests"]["tail"][-600:] or health["compile_errors"]})
    if bench:
        for r in bench["results"]:
            if not r.get("pass"):
                failing = [c["command"] + " → " + c["output"][-200:] for c in r.get("checks", []) if c["exit"] != 0]
                out.append({"severity": "high", "area": "outcome", "title": f"benchmark task {r['id']} failed hidden checks",
                            "evidence": "; ".join(failing) or r.get("error", "")[-400:]})
    if prof["stalls_by_role"]:
        out.append({"severity": "medium", "area": "loop", "title": "runs stalled or hit the turn limit",
                    "evidence": json.dumps(prof["stalls_by_role"], ensure_ascii=False)})
    if prof["errors"]:
        out.append({"severity": "high", "area": "runtime", "title": "agent runs ended in errors",
                    "evidence": json.dumps(prof["errors"], ensure_ascii=False)[:600]})
    if prof["rejections"]:
        out.append({"severity": "medium", "area": "quality", "title": f"{prof['rejections']} review rejections",
                    "evidence": " | ".join(prof["rejection_samples"][:4])[:900]})
    if prof["tool_errors"]:
        out.append({"severity": "low", "area": "tools", "title": "tool calls returned errors",
                    "evidence": json.dumps(prof["tool_error_samples"], ensure_ascii=False)[:900]})
    share = prof.get("cost_share") or {}
    overhead = sum(v for r, v in share.items() if r not in ("worker",))
    if share and overhead > 0.5:
        out.append({"severity": "medium", "area": "cost",
                    "title": f"coordination roles take {round(overhead * 100)}% of spend",
                    "evidence": json.dumps(prof["cost_by_role"], ensure_ascii=False)})
    if prof["audits_incomplete"]:
        out.append({"severity": "medium", "area": "planning", "title": "goal audits found missing work",
                    "evidence": " | ".join(prof["audits_incomplete"])[:900]})
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(out, key=lambda f: order[f["severity"]])


def doctor(code: Optional[Path], project_dirs: Iterable[Path], bench: Optional[dict] = None,
           run_tests: bool = True) -> dict:
    health = code_health(code) if code and run_tests else None
    prof = trajectory_profile(project_dirs)
    return {"health": health, "profile": prof, "findings": findings(health, prof, bench),
            "bench": bench["summary"] if bench else None}


def render_markdown(rep: dict) -> str:
    lines = ["# Artel doctor", ""]
    h = rep.get("health")
    if h:
        t = h["tests"]
        lines += [f"**Код:** компиляция {'ok' if h['compile_ok'] else 'FAIL'}, тесты {t['passed']} passed / "
                  f"{t['failed']} failed / {t['errors']} errors, {h['python_loc']} строк Python.", ""]
    if rep.get("bench"):
        b = rep["bench"]
        lines += [f"**Бенчмарк:** pass {b['pass_rate']:.0%}, partial {b['partial_mean']:.2f}, ${b['cost_usd']}, "
                  f"{b['duration_mean_s']} с на задачу, отказов {b['rejections']}, срывов {b['stalls']}.", ""]
    p = rep["profile"]
    lines += [f"**Траектории:** проектов {p['projects']} {p['status']}, стоимость по ролям {p['cost_by_role']}.", "",
              "## Находки", ""]
    for f in rep["findings"] or [{"severity": "info", "area": "-", "title": "проблем не найдено", "evidence": ""}]:
        lines.append(f"- **[{f['severity']}] {f['area']}: {f['title']}** — {f['evidence'][:400]}")
    return "\n".join(lines) + "\n"
