"""Benchmark suite: run Artel (any code revision) on tasks with hidden checks and measure it.

A task lives in ``evals/tasks/<id>/``:
    task.yaml   id, goal, optional seed dir, timeout_s, hidden: [shell commands]
    seed/       files copied into the deliverables directory before planning (optional)
    hidden/     checker files copied to ``.artel_eval/`` only after the run — agents never see them

Each task runs in its own ARTEL_HOME as a subprocess of the code under test
(``PYTHONPATH=<code>``), so a candidate branch is measured with its own code while
the measuring stays in the champion.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_TASKS = Path(__file__).resolve().parents[2] / "evals" / "tasks"
METRICS = ("pass", "partial", "cost_usd", "tokens", "duration_s", "attempts", "rejections", "stalls", "audit_rounds")


def load_tasks(tasks_dir: Path = DEFAULT_TASKS, only: Optional[list] = None) -> list[dict]:
    tasks = []
    for f in sorted(Path(tasks_dir).glob("*/task.yaml")):
        t = yaml.safe_load(f.read_text())
        t["dir"] = str(f.parent)
        if not only or t["id"] in only:
            tasks.append(t)
    return tasks


def _project_dir(home: Path) -> Optional[Path]:
    base = home / "projects"
    dirs = [d for d in base.iterdir() if (d / "project.json").exists()] if base.exists() else []
    return dirs[0] if dirs else None


def collect_metrics(pdir: Path) -> dict:
    meta = json.loads((pdir / "project.json").read_text())
    events = [json.loads(ln) for ln in (pdir / "events.jsonl").read_text().splitlines() if ln.strip()]
    db = sqlite3.connect(str(pdir / "board.db"))
    db.row_factory = sqlite3.Row
    tasks = [dict(r) for r in db.execute("SELECT status, attempts, kind FROM tasks")]
    runs = [dict(r) for r in db.execute("SELECT status FROM runs")]
    db.close()
    u = meta["usage"]
    return {
        "status": meta["status"],
        "cost_usd": round(u.get("cost_usd", 0.0), 4),
        "tokens": u["input_tokens"] + u["output_tokens"],
        "requests": u["requests"],
        "duration_s": round(events[-1]["ts"] - events[0]["ts"], 1) if events else 0,
        "attempts": sum(t["attempts"] for t in tasks if t["kind"] == "work"),
        "rejections": sum(1 for e in events if e["kind"] == "review/verdict" and not e["passed"]),
        "stalls": sum(1 for r in runs if r["status"] in ("stalled", "turn_limit", "error", "refused")),
        "audit_rounds": meta.get("audit_rounds", 0),
        "tasks": len([t for t in tasks if t["kind"] == "work"]),
    }


def run_hidden(task: dict, workdir: Path) -> dict:
    hidden_src = Path(task["dir"]) / "hidden"
    dest = workdir / ".artel_eval"
    if hidden_src.exists():
        shutil.copytree(hidden_src, dest, dirs_exist_ok=True)
    results = []
    for cmd in task.get("hidden", []):
        try:
            p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, text=True, timeout=300)
            results.append({"command": cmd, "exit": p.returncode, "output": (p.stdout + p.stderr)[-1500:]})
        except subprocess.TimeoutExpired:
            results.append({"command": cmd, "exit": -1, "output": "timeout"})
    if dest.exists():
        shutil.rmtree(dest)
    ok = sum(1 for r in results if r["exit"] == 0)
    return {"pass": bool(results) and ok == len(results), "partial": ok / len(results) if results else 0.0,
            "checks": results}


def run_task(task: dict, code: Path, config: dict, out_dir: Path) -> dict:
    home = out_dir / task["id"]
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "artel.yaml"  # picked up as ARTEL_HOME/artel.yaml
    cfg_path.write_text(yaml.safe_dump(config, allow_unicode=True))
    env = {**os.environ, "ARTEL_HOME": str(home), "PYTHONPATH": str(code)}
    cmd = [sys.executable, "-m", "artel.cli", "run", task["goal"], "--name", f"bench-{task['id']}"]
    if task.get("seed"):
        cmd += ["--seed", str(Path(task["dir"]) / task["seed"])]
    started = time.time()
    try:
        p = subprocess.run(cmd, cwd=home, env=env, capture_output=True, text=True,
                           timeout=int(task.get("timeout_s", 1800)))
        exit_code, tail = p.returncode, (p.stdout + p.stderr)[-3000:]
    except subprocess.TimeoutExpired as exc:
        exit_code, tail = -1, f"timeout after {task.get('timeout_s')}s\n{str(exc.stdout or '')[-1500:]}"
    pdir = _project_dir(home)
    result: dict = {"id": task["id"], "kind": task.get("kind"), "exit": exit_code, "wall_s": round(time.time() - started, 1)}
    if pdir is None:
        result.update({"pass": False, "partial": 0.0, "error": tail})
        return result
    result.update(collect_metrics(pdir))
    meta = json.loads((pdir / "project.json").read_text())
    workdir = Path(meta["workdir"]) if meta.get("workdir") else pdir / "shared"
    result.update(run_hidden(task, workdir))
    result["project"] = str(pdir)
    if exit_code not in (0, 2):
        result["error"] = tail
    return result


def summarize(results: list[dict]) -> dict:
    n = len(results) or 1
    return {
        "tasks": len(results),
        "pass_rate": round(sum(1 for r in results if r.get("pass")) / n, 3),
        "partial_mean": round(sum(r.get("partial", 0) for r in results) / n, 3),
        "cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 4),
        "tokens": sum(r.get("tokens", 0) for r in results),
        "duration_mean_s": round(sum(r.get("duration_s", 0) for r in results) / n, 1),
        "attempts": sum(r.get("attempts", 0) for r in results),
        "rejections": sum(r.get("rejections", 0) for r in results),
        "stalls": sum(r.get("stalls", 0) for r in results),
        "audit_rounds": sum(r.get("audit_rounds", 0) for r in results),
    }


def run_suite(code: Path, config: dict, out_dir: Path, tasks: Optional[list] = None, parallel: int = 2,
              label: str = "", repeats: int = 1) -> dict:
    tasks = tasks or load_tasks()
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(t, out_dir / (f"r{r}" if repeats > 1 else "")) for r in range(repeats) for t in tasks]
    with ThreadPoolExecutor(max(1, parallel)) as ex:
        results = list(ex.map(lambda j: run_task(j[0], code, config, j[1]), jobs))
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=code, capture_output=True, text=True).stdout.strip()
    suite = {"label": label, "code": str(code), "commit": commit, "ts": time.time(), "repeats": repeats,
             "config": {k: config.get(k) for k in ("provider", "runtime", "claude_code", "models")},
             "results": results, "summary": summarize(results)}
    (out_dir / "suite.json").write_text(json.dumps(suite, ensure_ascii=False, indent=1))
    return suite


def compare(base: dict, cand: dict) -> dict:
    """Metric and per-task deltas, candidate minus base."""
    b, c = base["summary"], cand["summary"]
    delta = {k: round(c[k] - b[k], 4) for k in b if isinstance(b[k], (int, float)) and k in c}
    per_task = {}
    bt = {r["id"]: r for r in base["results"]}
    for r in cand["results"]:
        o = bt.get(r["id"])
        if o:
            per_task[r["id"]] = {"pass": [o.get("pass"), r.get("pass")],
                                 "partial": round(r.get("partial", 0) - o.get("partial", 0), 3),
                                 "cost_usd": round(r.get("cost_usd", 0) - o.get("cost_usd", 0), 4),
                                 "duration_s": round(r.get("duration_s", 0) - o.get("duration_s", 0), 1),
                                 "rejections": r.get("rejections", 0) - o.get("rejections", 0)}
    improved = [t for t, d in per_task.items() if d["pass"] == [False, True] or d["partial"] > 0]
    regressed = [t for t, d in per_task.items() if d["pass"] == [True, False] or d["partial"] < 0]
    return {"base": b, "candidate": c, "delta": delta, "per_task": per_task, "improved": improved,
            "regressed": regressed}
