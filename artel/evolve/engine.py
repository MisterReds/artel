"""Recursive self-improvement: Artel improves Artel.

One generation:
    1. baseline   — benchmark the champion commit (cached per commit)
    2. diagnose   — code health + trajectory profile + failing hidden checks
    3. propose    — the evolver agent proposes K hypotheses from the evidence and the
                    ledger of past experiments (what helped, what hurt, and why)
    4. implement  — for each hypothesis: a git branch + worktree; the champion Artel runs a
                    project with ``workdir=<worktree>`` and implements it (Artel editing Artel)
    5. verify     — the candidate's own test suite must pass
    6. measure    — benchmark the candidate's code; compare per metric and per task
    7. select     — the best candidate that beats the champion is merged (--no-ff) and
                    tagged; every other branch stays in git with its verdict in the ledger

Absorb: point it at another harness (git URL or path). The absorber agent studies it
next to Artel's own code and emits features worth porting; each becomes a hypothesis
whose implementing team gets the source as a read-only reference.

The champion never edits itself in place: candidates live on branches until measured.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import bench as bench_mod
from . import diagnose as diag_mod

GIT_ID = ["-c", "user.name=artel-evolve", "-c", "user.email=artel-evolve@localhost"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:500]}")
    return p.stdout.strip()


def slug(text: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "change"


@dataclass
class Decision:
    accepted: bool
    reason: str
    score: float = 0.0


@dataclass
class Policy:
    """When does a candidate beat the champion?"""
    min_pass_gain: float = 0.0          # pass-rate must not drop; any gain wins
    cost_tolerance: float = 0.15        # at equal quality, accept if cost drops by at least this fraction
    max_cost_increase: float = 0.5      # reject quality gains that cost more than +50%

    def decide(self, cmp: dict, tests: dict) -> Decision:
        if not tests.get("ok"):
            return Decision(False, f"tests fail: {tests.get('summary', '')}", -1)
        b, c, d = cmp["base"], cmp["candidate"], cmp["delta"]
        if cmp["regressed"]:
            return Decision(False, f"regressed tasks: {', '.join(cmp['regressed'])}", -1)
        cost_ratio = (c["cost_usd"] / b["cost_usd"] - 1) if b["cost_usd"] else 0.0
        quality = d["pass_rate"] * 10 + d["partial_mean"]
        score = quality - max(cost_ratio, 0) * 0.5 - d["rejections"] * 0.05 - d["stalls"] * 0.1
        if d["pass_rate"] > self.min_pass_gain or d["partial_mean"] > 0:
            if cost_ratio > self.max_cost_increase:
                return Decision(False, f"quality +{d['pass_rate']:+.2f} but cost {cost_ratio:+.0%}", score)
            return Decision(True, f"quality up: pass {d['pass_rate']:+.2f}, partial {d['partial_mean']:+.3f}, "
                                  f"cost {cost_ratio:+.0%}", score)
        if d["pass_rate"] == 0 and d["partial_mean"] == 0 and cost_ratio <= -self.cost_tolerance:
            return Decision(True, f"same quality, cost {cost_ratio:+.0%}", score + 0.5)
        if d["pass_rate"] == 0 and d["partial_mean"] == 0 and (d["rejections"] < 0 or d["stalls"] < 0) \
                and cost_ratio <= 0.05:
            return Decision(True, f"same quality, fewer rejections/stalls ({d['rejections']:+}/{d['stalls']:+})",
                            score + 0.2)
        return Decision(False, f"no measurable gain (pass {d['pass_rate']:+.2f}, partial {d['partial_mean']:+.3f}, "
                               f"cost {cost_ratio:+.0%})", score)


@dataclass
class Hypothesis:
    title: str
    rationale: str
    plan: str
    expected: str = ""
    files: list = field(default_factory=list)
    refs: list = field(default_factory=list)       # read-only source dirs (absorb)
    source: str = "diagnosis"                      # diagnosis | absorb:<name> | manual
    license_note: str = ""

    @classmethod
    def from_dict(cls, d: dict, **extra) -> "Hypothesis":
        keys = cls.__dataclass_fields__
        return cls(**{k: v for k, v in {**d, **extra}.items() if k in keys})


class Evolution:
    def __init__(self, repo: Path, home: Path, config: dict, bench_config: dict, tasks: Optional[list] = None,
                 policy: Optional[Policy] = None, parallel: int = 2,
                 implement_fn: Optional[Callable] = None, bench_fn: Optional[Callable] = None,
                 agent_fn: Optional[Callable] = None):
        self.repo = Path(repo).resolve()
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.config = config                      # config for implementing / proposing (the champion's team)
        self.bench_config = bench_config          # config the benchmark runs with (candidate's team)
        self.tasks = tasks
        self.policy = policy or Policy()
        self.parallel = parallel
        self.implement_fn = implement_fn or self._implement
        self.bench_fn = bench_fn or self._bench
        self.agent_fn = agent_fn or self._agent
        import threading

        self._git_lock = threading.RLock()  # worktree add/remove and commits touch shared repo state
        state = self.home / "state.json"
        if state.exists():
            self.state = json.loads(state.read_text())
        else:
            self.state = {"main": git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "generation": 0}
        self.state["repo"] = str(self.repo)
        self._save()

    # --- persistence ----------------------------------------------------
    def _save(self) -> None:
        (self.home / "state.json").write_text(json.dumps(self.state, indent=1))

    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.jsonl"

    def ledger(self) -> list[dict]:
        p = self.ledger_path
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []

    def record(self, **entry) -> dict:
        entry = {"id": uuid.uuid4().hex[:10], "ts": time.time(), **entry}
        with self.ledger_path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def champion(self) -> str:
        return git(self.repo, "rev-parse", self.state["main"])

    # --- measurement ----------------------------------------------------

    def baseline(self, sha: str) -> dict:
        cache = self.home / "baseline" / f"{sha}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        wt = self._worktree(f"baseline-{sha[:10]}", sha, new_branch=False)
        try:
            suite = self.bench_fn(wt, f"base-{sha[:8]}")
        finally:
            self._remove_worktree(wt)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(suite, ensure_ascii=False))
        self.record(kind="baseline", commit=sha, summary=suite["summary"])
        return suite

    def run_tests(self, code: Path) -> dict:
        h = diag_mod.code_health(code)
        t = h["tests"]
        return {"ok": h["ok"], "summary": f"{t['passed']} passed, {t['failed']} failed, {t['errors']} errors",
                "tail": t["tail"][-1500:]}

    # --- git ------------------------------------------------------------
    def _worktree(self, name: str, base: str, new_branch: bool = True, branch: str = "") -> Path:
        with self._git_lock:
            return self._worktree_locked(name, base, new_branch, branch)

    def _worktree_locked(self, name: str, base: str, new_branch: bool, branch: str) -> Path:
        wt = self.home / "worktrees" / name
        if wt.exists():
            self._remove_worktree(wt)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if new_branch:
            git(self.repo, "worktree", "add", "-b", branch, str(wt), base)
        else:
            git(self.repo, "worktree", "add", "--detach", str(wt), base)
        return wt

    def _remove_worktree(self, wt: Path) -> None:
        with self._git_lock:
            self._remove_worktree_locked(wt)

    def _remove_worktree_locked(self, wt: Path) -> None:
        git(self.repo, "worktree", "remove", "--force", str(wt), check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)

    # --- agents ---------------------------------------------------------
    def _agent(self, role: str, goal_text: str, refs: list, terminal: str) -> dict:
        """Run one evolution agent (evolver / absorber) in a logged project; return its terminal payload."""
        from ..manager import new_kernel
        from ..project import Project
        from ..runtimes import run_agent
        from ..tooling import ToolContext
        from . import prompts as ep

        kernel = new_kernel(self.config)
        project = Project.create(goal_text[:200], self.config, home=self.home, name=f"{role}-{int(time.time())}",
                                 workdir=self.repo, refs=tuple(refs))
        project.set_meta(status="running", kind=f"evolution/{role}")
        ctx = ToolContext(kernel, project, role=role, workspace=self.repo)
        system = {"evolver": ep.EVOLVER, "absorber": ep.ABSORBER}[role]
        out = run_agent(kernel, project, role=role, system=system, first_user=goal_text, ctx=ctx, max_turns=60,
                        purpose=role)
        project.set_meta(status="done" if out.name == terminal else "error")
        if out.name != terminal:
            raise RuntimeError(f"{role} produced no {terminal}: {out.status} {out.error}")
        return out.payload

    def code_map(self) -> str:
        lines = []
        for p in sorted((self.repo / "artel").rglob("*.py")):
            text = p.read_text(errors="replace")
            m = re.match(r'\s*(?:"""|\'\'\')(.*?)(?:\n|""")', text, re.S)
            doc = (m.group(1).strip() if m else "")[:140]
            lines.append(f"- {p.relative_to(self.repo)} ({len(text.splitlines())} lines): {doc}")
        return "\n".join(lines)

    def history_text(self, limit: int = 30) -> str:
        rows = [e for e in self.ledger() if e["kind"] in ("candidate", "absorb_candidate")][-limit:]
        if not rows:
            return "(no experiments yet)"
        return "\n".join(f"- [{'ACCEPTED' if e.get('accepted') else 'rejected'}] {e['hypothesis']['title']} — "
                         f"{e.get('reason', '')}; delta {json.dumps(e.get('delta', {}))[:240]}" for e in rows)

    def propose(self, report: dict, n: int) -> list[Hypothesis]:
        goal = (f"Propose {n} improvement hypotheses for Artel (the codebase at {self.repo}).\n\n"
                f"<diagnosis>\n{diag_mod.render_markdown(report)}\n</diagnosis>\n\n"
                f"<code_map>\n{self.code_map()}\n</code_map>\n\n<past_experiments>\n{self.history_text()}\n"
                f"</past_experiments>\n\nRead the relevant code before proposing. Finish with propose_hypotheses.")
        payload = self.agent_fn("evolver", goal, [], "propose_hypotheses")
        return [Hypothesis.from_dict(h) for h in payload["hypotheses"][:n]]

    # --- implementation -------------------------------------------------
    def implementation_goal(self, h: Hypothesis, wt: Path) -> str:
        refs = f"\nReference sources (read-only): {', '.join(h.refs)}. {h.license_note}" if h.refs else ""
        return (f"Improve Artel — the multi-agent harness you are running in — by implementing one change in its "
                f"codebase at {wt} (a git worktree; edit files there directly).\n\n"
                f"Change: {h.title}\nWhy: {h.rationale}\nPlan: {h.plan}\nExpected effect: {h.expected}\n"
                f"Likely files: {', '.join(h.files) or 'decide after reading the code'}{refs}\n\n"
                "Constraints: keep the change focused; keep `python -m pytest -q` passing in the worktree and add "
                "tests for new behavior; keep the CLI contract `artel run GOAL --name --seed --workdir --ref` and "
                "the `scripted` provider working; do not edit evals/tasks. Do not add notes, summaries, scratch files or a "
                "shared/ directory to the repository: only source, tests and docs that belong to Artel. Every task "
                "must include the check "
                "`python -m pytest -q -x -p no:cacheprovider`.")

    def _implement(self, h: Hypothesis, wt: Path) -> dict:
        from ..manager import run_goal

        project, status = run_goal(self.implementation_goal(h, wt), self.config, name=f"evolve-{wt.name}",
                                   workdir=wt, refs=tuple(h.refs))
        meta = project.meta
        return {"project": str(project.root), "status": status, "cost_usd": meta["usage"].get("cost_usd", 0.0)}

    def _commit(self, wt: Path, h: Hypothesis) -> dict:
        with self._git_lock:
            return self._commit_locked(wt, h)

    def _commit_locked(self, wt: Path, h: Hypothesis) -> dict:
        git(wt, "add", "-A")
        if not git(wt, "status", "--porcelain"):
            return {"changed": False}
        git(wt, "commit", "-q", "-m", f"evolve: {h.title}\n\n{h.rationale}\n\nSource: {h.source}")
        stat = git(wt, "diff", "--shortstat", "HEAD~1", "HEAD")
        return {"changed": True, "commit": git(wt, "rev-parse", "HEAD"), "diffstat": stat}

    # --- generation -----------------------------------------------------
    # --- population generation ------------------------------------------
    def _bench_call(self, code: Path, label: str, tasks: Optional[list] = None) -> dict:
        try:
            return self.bench_fn(code, label, tasks=tasks) if tasks else self.bench_fn(code, label)
        except TypeError:  # bench functions without task selection
            return self.bench_fn(code, label)

    def _bench(self, code: Path, label: str, tasks: Optional[list] = None) -> dict:  
        out = self.home / "bench" / f"{label}-{int(time.time())}"
        only = tasks or self.tasks
        return bench_mod.run_suite(code, self.bench_config, out, tasks=bench_mod.load_tasks(only=only) if only else None,
                                   parallel=self.parallel, label=label)

    @staticmethod
    def _subset(suite: dict, ids: Optional[list]) -> dict:
        if not ids:
            return suite
        rows = [r for r in suite["results"] if r["id"] in ids]
        return {**suite, "results": rows, "summary": bench_mod.summarize(rows)}

    def _spent(self, entries: list) -> float:
        total = 0.0
        for e in entries:
            total += float((e.get("implementation") or {}).get("cost_usd") or 0)
            for key in ("screen", "summary"):
                total += float((e.get(key) or {}).get("cost_usd") or 0)
        return total

    def _build_candidate(self, gen: int, k: int, h: Hypothesis, champ: str, kind: str, budget_left) -> dict:
        branch = self._unique_branch(f"evolve/g{gen}-{k}-{slug(h.title)}")
        entry = {"kind": kind, "generation": gen, "branch": branch, "parent": champ, "hypothesis": h.__dict__}
        if budget_left() <= 0:
            entry.update(accepted=False, reason="generation budget exhausted before implementation", stage="skipped")
            return entry
        wt = self._worktree(branch.replace("/", "_"), champ, branch=branch)
        entry["worktree"] = str(wt)
        try:
            impl = self.implement_fn(h, wt)
            commit = self._commit(wt, h)
            entry.update(implementation=impl, commit=commit)
            if not commit["changed"]:
                entry.update(accepted=False, reason="implementation produced no changes", stage="implement")
                return entry
            tests = self.run_tests(wt)
            entry["tests"] = {k2: v for k2, v in tests.items() if k2 != "tail"}
            if not tests["ok"]:
                entry.update(accepted=False, reason=f"tests fail: {tests['summary']}", tests_tail=tests["tail"][-800:],
                             stage="tests")
                return entry
            entry["stage"] = "tested"
        except Exception as exc:  # noqa: BLE001 - a broken candidate never stops evolution
            entry.update(accepted=False, reason=f"error: {exc!r}", stage="error")
        return entry

    def generation(self, candidates: int = 2, hypotheses: Optional[list] = None, kind: str = "candidate",
                   parallel: int = 1, screen_tasks: Optional[list] = None, finalists: Optional[int] = None,
                   budget_usd: Optional[float] = None, stack: bool = True) -> dict:
        """One generation over a population of hypotheses.

        implement + test (in parallel) -> screen on a cheap task subset -> full benchmark for the best
        ``finalists`` -> stack every accepted candidate on an integration branch, re-testing after each
        merge -> merge the stack if it beats the best single candidate, else the best single candidate.
        """
        from concurrent.futures import ThreadPoolExecutor

        self.state["generation"] += 1
        gen = self.state["generation"]
        self.state["running"] = {"generation": gen, "since": time.time(), "phase": "baseline"}
        self._save()
        champ = self.champion()
        base = self.baseline(champ)
        if hypotheses is None:
            report = diag_mod.doctor(None, [Path(r["project"]) for r in base["results"] if r.get("project")],
                                     bench=base, run_tests=False)
            (self.home / f"gen{gen}-diagnosis.md").write_text(diag_mod.render_markdown(report))
            self._phase("proposing")
            hypotheses = self.propose(report, candidates)
        results: list[dict] = []

        def budget_left() -> float:
            return float("inf") if budget_usd is None else budget_usd - self._spent(results)

        # 1. implement + test, in parallel (worktrees are created inside each job)
        self._phase(f"implementing {len(hypotheses)} candidates, {parallel} at a time")
        with ThreadPoolExecutor(max(1, parallel)) as ex:
            futs = [ex.submit(self._build_candidate, gen, k, h, champ, kind, budget_left)
                    for k, h in enumerate(hypotheses, 1)]
            for f in futs:
                results.append(f.result())
        alive = [e for e in results if e.get("stage") == "tested"]

        # 2. screen on a cheap subset, rank, keep finalists
        if screen_tasks and alive:
            self._phase(f"screening {len(alive)} candidates on {', '.join(screen_tasks)}")
            base_sub = self._subset(base, screen_tasks)
            with ThreadPoolExecutor(max(1, parallel)) as ex:
                screens = list(ex.map(lambda e: self._bench_call(Path(e["worktree"]), f"g{gen}-screen-"
                                                                 f"{e['branch'].split('-')[1]}", screen_tasks), alive))
            for e, sc in zip(alive, screens):
                cmp = bench_mod.compare(base_sub, self._subset(sc, screen_tasks))
                d = self.policy.decide(cmp, {"ok": True})
                e.update(screen=cmp["candidate"], screen_delta=cmp["delta"], screen_score=d.score,
                         screen_regressed=cmp["regressed"])
            survivors = [e for e in alive if not e["screen_regressed"]]
            for e in alive:
                if e["screen_regressed"]:
                    e.update(accepted=False, reason=f"screen regressed: {', '.join(e['screen_regressed'])}",
                             stage="screen")
            survivors.sort(key=lambda e: -e["screen_score"])
            for e in survivors[finalists:] if finalists else []:
                e.update(accepted=False, reason=f"screened out (rank below top {finalists})", stage="screen")
            alive = survivors[:finalists] if finalists else survivors
        elif finalists:
            alive = alive[:finalists]

        # 3. full benchmark for finalists
        self._phase(f"benchmarking {len(alive)} finalists")
        for e in alive:
            if budget_left() <= 0:
                e.update(accepted=False, reason="generation budget exhausted before full benchmark", stage="budget")
                continue
            cand = self._bench_call(Path(e["worktree"]), f"g{gen}-{e['branch'].split('-')[1]}")
            cmp = bench_mod.compare(base, cand)
            d = self.policy.decide(cmp, {"ok": True})
            e.update(accepted=d.accepted, reason=d.reason, score=d.score, delta=cmp["delta"], per_task=cmp["per_task"],
                     improved=cmp["improved"], regressed=cmp["regressed"], summary=cmp["candidate"], stage="benchmarked")

        winners = sorted([e for e in results if e.get("accepted")], key=lambda e: -e.get("score", 0))
        merged, merged_what = None, []
        stack_entry = None
        if len(winners) >= 2 and stack:
            stack_entry = self._stack(gen, champ, base, winners, budget_left)
        git(self.repo, "checkout", "-q", self.state["main"])
        if stack_entry and stack_entry.get("accepted") and stack_entry["score"] >= winners[0]["score"]:
            git(self.repo, "merge", "--no-ff", "-q", "-m",
                f"evolve gen {gen}: stack of {len(stack_entry['included'])} improvements\n\n{stack_entry['reason']}\n"
                f"Included: {', '.join(stack_entry['included'])}\nDelta: {json.dumps(stack_entry.get('delta', {}))}",
                stack_entry["branch"])
            merged_what = stack_entry["included"]
        elif winners:
            w = winners[0]
            git(self.repo, "merge", "--no-ff", "-q", "-m",
                f"evolve gen {gen}: {w['hypothesis']['title']}\n\n{w['reason']}\nDelta: {json.dumps(w.get('delta', {}))}",
                w["branch"])
            merged_what = [w["branch"]]
        if merged_what:
            merged = git(self.repo, "rev-parse", "HEAD")
            git(self.repo, "tag", "-f", f"evolve-gen-{gen}", merged)
            # Some git proxies refuse tag pushes; a branch marks the generation everywhere.
            git(self.repo, "branch", "-f", f"evolve/gen-{gen}", merged)
        for e in winners:
            if e["branch"] not in merged_what:
                e["reason"] += " (accepted alone, not merged: " + (
                    "excluded from the stack)" if stack_entry else "a better candidate won)")
        for e in results:
            e["merged"] = e["branch"] in merged_what
            wt = e.pop("worktree", None)
            if wt:
                self._remove_worktree(Path(wt))
            self.record(**e)
        if stack_entry:
            stack_entry["merged"] = bool(merged_what) and merged_what == stack_entry.get("included")
            self.record(**stack_entry)
        summary = {"generation": gen, "champion_before": champ, "champion_after": merged or champ,
                   "candidates": len(results), "accepted": merged_what, "spent_usd": round(self._spent(results), 4),
                   "stages": {st: sum(1 for e in results if e.get("stage") == st) for st in
                              ("skipped", "implement", "tests", "error", "screen", "budget", "benchmarked")}}
        summary["published"] = self.publish()
        self.record(kind="generation", **summary)
        self.state.pop("running", None)
        self._save()
        return summary

    def _unique_branch(self, name: str) -> str:
        """Branch names must not collide with earlier runs (e.g. a fresh evolution home on the same repo)."""
        with self._git_lock:
            candidate, n = name, 2
            while git(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}", check=False):
                candidate, n = f"{name}-{n}", n + 1
            return candidate

    def _stack(self, gen: int, champ: str, base: dict, winners: list, budget_left) -> dict:
        """Merge accepted candidates one by one on an integration branch; keep those that merge cleanly and pass tests."""
        self._phase(f"stacking {len(winners)} accepted candidates")
        branch = self._unique_branch(f"evolve/g{gen}-stack")
        wt = self._worktree(branch.replace("/", "_"), champ, branch=branch)
        entry: dict = {"kind": "stack", "generation": gen, "branch": branch, "parent": champ, "included": [],
                       "excluded": {}, "hypothesis": {"title": f"stack of generation {gen}", "source": "stack"}}
        try:
            for w in winners:
                m = subprocess.run(["git", *GIT_ID, "merge", "--no-ff", "-q", "-m", f"stack: {w['hypothesis']['title']}",
                                    w["branch"]], cwd=wt, capture_output=True, text=True)
                if m.returncode != 0:
                    git(wt, "merge", "--abort", check=False)
                    entry["excluded"][w["branch"]] = "merge conflict"
                    continue
                tests = self.run_tests(wt)
                if not tests["ok"]:
                    git(wt, "reset", "--hard", "-q", "HEAD~1")
                    entry["excluded"][w["branch"]] = f"tests fail in combination: {tests['summary']}"
                    continue
                entry["included"].append(w["branch"])
            if len(entry["included"]) < 2 or budget_left() <= 0:
                entry.update(accepted=False, reason="fewer than two compatible improvements, or no budget left")
                return entry
            suite = self._bench_call(wt, f"g{gen}-stack")
            cmp = bench_mod.compare(base, suite)
            d = self.policy.decide(cmp, {"ok": True})
            entry.update(accepted=d.accepted, reason=d.reason, score=d.score, delta=cmp["delta"],
                         per_task=cmp["per_task"], summary=cmp["candidate"])
        finally:
            self._remove_worktree(wt)
        return entry

    def publish(self) -> dict:
        """Push the champion, every evolve/* branch and the generation tags to `origin` (best-effort)."""
        if not git(self.repo, "remote", check=False):
            return {"pushed": False, "reason": "no remote"}
        p = subprocess.run(["git", "push", "--quiet", "origin", self.state["main"], "refs/heads/evolve/*:refs/heads/evolve/*"],
                           cwd=self.repo, capture_output=True, text=True, timeout=600)
        tags = subprocess.run(["git", "push", "--quiet", "origin", "--tags"], cwd=self.repo, capture_output=True,
                              text=True, timeout=300)
        return {"pushed": p.returncode == 0, "tags_pushed": tags.returncode == 0,
                "detail": (p.stderr or p.stdout).strip()[-300:]}

    def _phase(self, phase: str) -> None:
        if "running" in self.state:
            self.state["running"]["phase"] = phase
            self._save()

    def run(self, generations: int = 1, candidates: int = 2, **opts) -> list[dict]:
        return [self.generation(candidates, **opts) for _ in range(generations)]

    # --- absorb ---------------------------------------------------------
    def absorb(self, source: str, focus: str = "", top: int = 2, run: bool = False) -> dict:
        src = Path(source).expanduser()
        name = slug(src.name if src.exists() else source.rstrip("/").split("/")[-1].removesuffix(".git"))
        if not src.exists():
            url = source if "://" in source or source.startswith("git@") else f"https://github.com/{source}.git"
            dest = self.home / "sources" / name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(dest)], check=True, timeout=900)
            src = dest
        lic = detect_license(src)
        goal = (f"Study the harness at {src} completely and find features worth absorbing into Artel (codebase at "
                f"{self.repo}). {('Focus: ' + focus) if focus else ''}\n\nArtel today:\n<code_map>\n{self.code_map()}"
                f"\n</code_map>\n\nAlready adopted vs planned (from docs/HARNESS_REGISTRY.md):\n"
                f"{adopted_table(self.repo)}\n\nLicense of the source: {lic}. Finish with absorb_report.")
        report = self.agent_fn("absorber", goal, [str(src)], "absorb_report")
        feats = sorted(report["features"], key=lambda f: -float(f.get("fit", 0)))
        out = {"source": source, "path": str(src), "license": lic, "summary": report.get("summary", ""),
               "features": feats}
        adir = self.home / "absorb"
        adir.mkdir(exist_ok=True)
        (adir / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        self.record(kind="absorb", source=source, license=lic, features=[f["name"] for f in feats],
                    summary=report.get("summary", ""))
        if run and feats:
            copy_ok = lic in ("MIT", "Apache-2.0", "BSD")
            hyps = [Hypothesis.from_dict({
                "title": f"Absorb from {name}: {f['name']}", "rationale": f.get("value", ""),
                "plan": f.get("port_plan", ""), "expected": f.get("expected", ""),
                "files": [f.get("target", "")] if f.get("target") else []},
                refs=[str(src)], source=f"absorb:{name}",
                license_note=("Source license allows copying with attribution; keep the notice in a comment."
                              if copy_ok else "Reimplement the idea; do not copy code (license not permissive)."))
                for f in feats[:top]]
            out["generation"] = self.generation(len(hyps), hypotheses=hyps, kind="absorb_candidate")
        return out


def detect_license(src: Path) -> str:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        p = src / name
        if p.exists():
            t = p.read_text(errors="replace")[:3000]
            if "MIT License" in t or "Permission is hereby granted, free of charge" in t:
                return "MIT"
            if "Apache License" in t:
                return "Apache-2.0"
            if "Redistribution and use in source and binary forms" in t:
                return "BSD"
            if "GNU GENERAL PUBLIC LICENSE" in t.upper():
                return "GPL"
            return "other"
    return "none"


def adopted_table(repo: Path) -> str:
    p = repo / "docs" / "HARNESS_REGISTRY.md"
    if not p.exists():
        return "(none)"
    text = p.read_text()
    m = re.search(r"## Что взято и что ещё нужно взять(.*?)\n## ", text, re.S)
    return (m.group(1) if m else text[:4000]).strip()[:6000]
