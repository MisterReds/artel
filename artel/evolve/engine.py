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


from ..failures import LIMIT as INFRA_PATTERNS  # noqa: E402 - provider limits/outages
from ..failures import retry_at as _retry_at  # noqa: E402


class InfraError(RuntimeError):
    """The model provider, not the candidate, failed (limits, auth, outage). The generation pauses with its plan
    saved; ``resume`` continues it. ``retry_at`` is a unix time when the provider said the limit resets."""

    def __init__(self, message: str, retry_at: Optional[float] = None):
        super().__init__(message)
        self.retry_at = retry_at


def infra_problem(text: str) -> Optional[InfraError]:
    m = INFRA_PATTERNS.search(text or "") or _AUTH.search(text or "")
    if not m:
        return None
    line = next((ln for ln in (text or "").splitlines() if INFRA_PATTERNS.search(ln) or _AUTH.search(ln)),
                m.group(0)).strip()[:300]
    return InfraError(line, _retry_at(text))


_AUTH = re.compile(r"invalid api key|authentication_error", re.I)  # provider auth breaks every run alike


def check_suite(suite: dict) -> dict:
    """Raise InfraError when benchmark runs failed because of the provider rather than the code under test."""
    for r in suite.get("results", []):
        if not r.get("pass"):
            err = infra_problem(str(r.get("error", "")))
            if err:
                raise err
    return suite


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

    adopt_cost_increase: float = 0.25   # a planned feature may cost at most +25% on the benchmark

    def adopt(self, cmp: dict, tests: dict, regressed: list) -> Decision:
        """Planned (roadmap) features are not a contest: adopt when tests pass and nothing got worse.
        ``regressed`` are regressions confirmed by a re-run (single LLM runs are noisy)."""
        if not tests.get("ok"):
            return Decision(False, f"tests fail: {tests.get('summary', '')}", -1)
        if regressed:
            return Decision(False, f"breaks tasks: {', '.join(regressed)}", -1)
        b, c = cmp["base"], cmp["candidate"]
        cost_ratio = (c["cost_usd"] / b["cost_usd"] - 1) if b["cost_usd"] else 0.0
        if cost_ratio > self.adopt_cost_increase:
            return Decision(False, f"cost {cost_ratio:+.0%} over the +{self.adopt_cost_increase:.0%} allowance", -1)
        return Decision(True, f"adopted: tests pass, no regression (pass {cmp['delta']['pass_rate']:+.2f}, "
                              f"cost {cost_ratio:+.0%})", 0)


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
        if not self.ledger_path.exists():
            self.restore_journal()
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
        if entry.get("kind") in self.JOURNAL_KINDS:
            self.sync_journal(f"{entry['kind']}: {entry.get('branch') or entry.get('source') or entry.get('generation', '')}")
        return entry

    # --- durability: everything worth keeping goes to the git remote as it happens ---------------
    JOURNAL_BRANCH = "evolve/journal"
    JOURNAL_KINDS = ("baseline", "candidate", "absorb_candidate", "stack", "generation", "absorb", "absorb_error",
                     "roadmap_pick", "paused", "voided")

    def restore_journal(self) -> bool:
        """A fresh machine (or a reclaimed container) continues from the journal on the remote."""
        import io
        import tarfile

        if not git(self.repo, "remote", check=False):
            return False
        f = subprocess.run(["git", "fetch", "--quiet", "origin",
                            f"+refs/heads/{self.JOURNAL_BRANCH}:refs/heads/{self.JOURNAL_BRANCH}"],
                           cwd=self.repo, capture_output=True, timeout=600)
        if f.returncode != 0:
            return False
        tar = subprocess.run(["git", "archive", "--format=tar", self.JOURNAL_BRANCH], cwd=self.repo,
                             capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            try:
                tf.extractall(self.home, filter="data")
            except TypeError:  # Python without extraction filters
                tf.extractall(self.home)
        return True

    def _has_remote(self) -> bool:
        return bool(git(self.repo, "remote", check=False)) and self.state.get("push", True)

    def _push(self, *refs: str, force: bool = False) -> bool:
        """Best-effort push of exact refs to origin with backoff (git proxies drop connections). ``force`` only
        for scratch branches that are rebuilt by design (integration branches)."""
        if not self._has_remote():
            return False
        for attempt in range(4):
            p = subprocess.run(["git", "push", "--quiet", "origin",
                                *[f"{'+' if force else ''}refs/heads/{r}:refs/heads/{r}" for r in refs]],
                               cwd=self.repo, capture_output=True, text=True, timeout=600)
            if p.returncode == 0:
                return True
            time.sleep(2 ** (attempt + 1))
        self.state.setdefault("push_failures", []).append({"refs": list(refs), "error": p.stderr.strip()[-300:]})
        return False

    def _journal_files(self) -> dict:
        """Relative path -> bytes: the ledger, state, baselines, diagnoses, absorb reports, gzipped transcripts."""
        import gzip

        out = {}
        for pat in ("ledger.jsonl", "state.json", "baseline/*.json", "*-diagnosis.md", "absorb/*.json"):
            for f in self.home.glob(pat):
                out[str(f.relative_to(self.home))] = f.read_bytes()
        for f in list(self.home.glob("projects/*/events.jsonl")) + list(self.home.glob("bench/**/events.jsonl")):
            if f.stat().st_size < 50_000_000:
                out[str(f.relative_to(self.home)) + ".gz"] = gzip.compress(f.read_bytes(), mtime=0)
        for f in self.home.glob("bench/**/suite.json"):
            out[str(f.relative_to(self.home))] = f.read_bytes()
        return out

    def sync_journal(self, message: str = "journal") -> Optional[str]:
        """Commit the evolution journal to the orphan branch evolve/journal (git plumbing: no checkout, the
        main working tree is untouched) and push it. Returns the commit sha, or None when nothing changed."""
        import os
        import tempfile

        with self._git_lock:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

                    def g(*args, data=None):
                        return subprocess.run(["git", *GIT_ID, *args], cwd=self.repo, env=env, input=data,
                                              capture_output=True, check=True).stdout.decode().strip()

                    for rel, data in sorted(self._journal_files().items()):
                        blob = g("hash-object", "-w", "--stdin", data=data)
                        g("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}")
                    tree = g("write-tree")
                    parent = git(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{self.JOURNAL_BRANCH}",
                                 check=False)
                    if parent and git(self.repo, "rev-parse", f"{parent}^{{tree}}") == tree:
                        return None
                    sha = g("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message)
                    g("update-ref", f"refs/heads/{self.JOURNAL_BRANCH}", sha)
            except subprocess.CalledProcessError as e:
                self.state.setdefault("push_failures", []).append({"journal": (e.stderr or b"").decode()[-300:]})
                return None
        self._push(self.JOURNAL_BRANCH)
        return sha

    def champion(self) -> str:
        return git(self.repo, "rev-parse", self.state["main"])

    # --- measurement ----------------------------------------------------

    def baseline(self, sha: str) -> dict:
        cache = self.home / "baseline" / f"{sha}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        reuse = self._equivalent_baseline(sha)
        if reuse:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(reuse.read_text())
            return json.loads(cache.read_text())
        wt = self._worktree(f"baseline-{sha[:10]}", sha, new_branch=False)
        try:
            suite = self._bench_call(wt, f"base-{sha[:8]}")
        finally:
            self._remove_worktree(wt)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(suite, ensure_ascii=False))
        self.record(kind="baseline", commit=sha, summary=suite["summary"])
        return suite

    NON_CODE = ("roadmap.yaml", "studied.yaml", "docs/")

    def _equivalent_baseline(self, sha: str) -> Optional[Path]:
        """A cached baseline of a commit that differs from ``sha`` only in non-code files (roadmap, docs)."""
        for f in sorted((self.home / "baseline").glob("*.json"), key=lambda p: -p.stat().st_mtime):
            d = subprocess.run(["git", "diff", "--name-only", f.stem, sha], cwd=self.repo, capture_output=True, text=True)
            if d.returncode == 0 and all(c.startswith(self.NON_CODE) for c in d.stdout.splitlines() if c):
                return f
        return None

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
                "the `scripted` provider working; do not edit evals/tasks or roadmap.yaml (the orchestrator updates it). Do not add notes, summaries, scratch files or a "
                "shared/ directory to the repository: only source, tests and docs that belong to Artel. Every task "
                "must include the check "
                "`python -m pytest -q -x -p no:cacheprovider`.")

    def _implement(self, h: Hypothesis, wt: Path) -> dict:
        from ..manager import run_goal

        project, status = run_goal(self.implementation_goal(h, wt), self.config, name=f"evolve-{wt.name}",
                                   workdir=wt, refs=tuple(h.refs))
        self._raise_if_infra(project, status)
        meta = project.meta
        return {"project": str(project.root), "status": status, "cost_usd": meta["usage"].get("cost_usd", 0.0)}

    @staticmethod
    def _raise_if_infra(project, status: str) -> None:
        if status == "done":
            return
        tail = "\n".join(json.dumps(e, ensure_ascii=False) for e in project.log.read()[-60:])
        err = infra_problem(tail)
        if err:
            raise err

    def _continue_implementation(self, h: Hypothesis, wt: Path) -> Optional[dict]:
        """Resume the interrupted implementation project that worked in ``wt`` (None if there is none)."""
        if self.implement_fn != self._implement:
            return None
        from ..manager import resume as resume_project
        from ..project import artel_home

        for meta_path in sorted((artel_home() / "projects").glob("*/project.json"), key=lambda p: -p.stat().st_mtime):
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                continue
            if meta.get("workdir") == str(wt) and meta.get("status") != "done":
                project, status = resume_project(meta_path.parent.name, self.config)
                self._raise_if_infra(project, status)
                return {"project": str(project.root), "status": status, "resumed": True,
                        "cost_usd": project.meta["usage"].get("cost_usd", 0.0)}
        return None

    def resume_plan_from_ledger(self) -> bool:
        """Rebuild the plan of an interrupted generation started before plans were saved (one-time migration)."""
        run = self.state.get("running")
        if run and run.get("plan"):
            if run["plan"].get("roadmap"):
                run["plan"].setdefault("mode", "adopt")  # plans saved before adopt mode existed
            return True
        if not run:
            return False
        from ..roadmap import Roadmap

        gen = run["generation"]
        picks = [e for e in self.ledger() if e["kind"] == "roadmap_pick"]
        branches = sorted(git(self.repo, "branch", "--list", f"evolve/g{gen}-*", "--format=%(refname:short)").split())
        if not picks or not branches:
            return False
        rm = Roadmap.of_repo(self.repo)
        hyps = [rm.to_hypothesis(rm.get(i), self.home / "sources") for i in picks[-1]["items"]]
        champ = git(self.repo, "merge-base", branches[0], self.state["main"])
        by_k = {b.split("-")[1]: b for b in branches}
        run["plan"] = {"champ": champ, "kind": "candidate", "hypotheses": [h.__dict__ for h in hyps],
                       "branches": by_k, "roadmap": picks[-1]["items"]}
        self._save()
        return True

    def _commit(self, wt: Path, h: Hypothesis) -> dict:
        with self._git_lock:
            return self._commit_locked(wt, h)

    def _commit_locked(self, wt: Path, h: Hypothesis) -> dict:
        git(wt, "add", "-A", "--", ".", ":!**/__pycache__/**", ":!*.pyc", ":!.pytest_cache/**")
        if not git(wt, "diff", "--cached", "--name-only"):
            return {"changed": False}
        git(wt, "commit", "-q", "-m", f"evolve: {h.title}\n\n{h.rationale}\n\nSource: {h.source}")
        stat = git(wt, "diff", "--shortstat", "HEAD~1", "HEAD")
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != "HEAD":  # the candidate's code reaches the remote before it is even measured
            self._push(branch)
        return {"changed": True, "commit": git(wt, "rev-parse", "HEAD"), "diffstat": stat}

    # --- generation -----------------------------------------------------
    # --- population generation ------------------------------------------
    def _bench_call(self, code: Path, label: str, tasks: Optional[list] = None) -> dict:
        try:
            suite = self.bench_fn(code, label, tasks=tasks) if tasks else self.bench_fn(code, label)
        except TypeError:  # bench functions without task selection
            suite = self.bench_fn(code, label)
        return check_suite(suite)

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
        plan = self.state["running"]["plan"]
        with self._git_lock:
            branch = plan["branches"].get(str(k)) or self._unique_branch(f"evolve/g{gen}-{k}-{slug(h.title)}")
            plan["branches"][str(k)] = branch
            self._save()
        entry = {"kind": kind, "generation": gen, "branch": branch, "parent": champ, "hypothesis": h.__dict__}
        if budget_left() <= 0:
            entry.update(accepted=False, reason="generation budget exhausted before implementation", stage="skipped")
            return entry
        wt = self.home / "worktrees" / branch.replace("/", "_")
        exists = bool(git(self.repo, "branch", "--list", branch))
        ahead = exists and int(git(self.repo, "rev-list", "--count", f"{champ}..{branch}") or 0) > 0
        try:
            if not exists:
                wt = self._worktree(branch.replace("/", "_"), champ, branch=branch)
            elif not (wt / ".git").exists():
                with self._git_lock:
                    if wt.exists():
                        self._remove_worktree_locked(wt)
                    git(self.repo, "worktree", "prune", check=False)
                    git(self.repo, "worktree", "add", str(wt), branch)
        except Exception as exc:  # noqa: BLE001
            entry.update(accepted=False, reason=f"error: worktree: {exc!r}", stage="error")
            return entry
        entry["worktree"] = str(wt)
        try:
            if ahead:  # the commit happens only after the implementation finished
                impl = {"resumed": "implementation already committed"}
            else:
                impl = self._continue_implementation(h, wt) if exists else None
                impl = impl or self.implement_fn(h, wt)
            commit = self._commit(wt, h) if not ahead else {"changed": False}
            if ahead:
                commit = {"changed": True, "commit": git(wt, "rev-parse", "HEAD"),
                          "diffstat": git(wt, "diff", "--shortstat", champ, "HEAD")}
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
        except InfraError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken candidate never stops evolution
            err = infra_problem(repr(exc))
            if err:
                raise err from exc
            entry.update(accepted=False, reason=f"error: {exc!r}", stage="error")
        return entry

    def generation(self, *args, **kwargs) -> dict:
        try:
            return self._generation(*args, **kwargs)
        except InfraError as e:
            run = self.state.get("running") or {}
            run["phase"] = f"paused: {e}"
            self._save()
            self.record(kind="paused", generation=run.get("generation"), reason=str(e), retry_at=e.retry_at)
            raise

    def _generation(self, candidates: int = 2, hypotheses: Optional[list] = None, kind: str = "candidate",
                   parallel: int = 1, screen_tasks: Optional[list] = None, finalists: Optional[int] = None,
                   budget_usd: Optional[float] = None, stack: bool = True, resume: bool = False,
                   plan_extra: Optional[dict] = None, repairs: int = 2) -> dict:
        """One generation over a population of hypotheses.

        The plan (parent commit, hypotheses, candidate branches) is saved in state.json before any work, so a
        generation interrupted by a crash or a reclaimed container continues with ``resume=True``: committed
        candidates are kept, half-done implementations continue in their worktree from their own project.

        implement + test (in parallel) -> screen on a cheap task subset -> full benchmark for the best
        ``finalists`` -> stack every accepted candidate on an integration branch, re-testing after each
        merge -> merge the stack if it beats the best single candidate, else the best single candidate.
        """
        from concurrent.futures import ThreadPoolExecutor

        plan = (self.state.get("running") or {}).get("plan") if resume else None
        if plan:
            gen, champ = self.state["running"]["generation"], plan["champ"]
            hypotheses = [Hypothesis.from_dict(h) for h in plan["hypotheses"]]
            kind = plan.get("kind", kind)
            self.state["running"].update(phase="baseline", resumed_at=time.time())
            self.record(kind="resume", generation=gen, branches=plan.get("branches", {}))
        else:
            self.state["generation"] += 1
            gen = self.state["generation"]
            champ = self.champion()
            self.state["running"] = {"generation": gen, "since": time.time(), "phase": "baseline",
                                     "plan": {"champ": champ, "kind": kind, "branches": {}, **(plan_extra or {})}}
        self._save()
        base = self.baseline(champ)
        if hypotheses is None:
            report = diag_mod.doctor(None, [Path(r["project"]) for r in base["results"] if r.get("project")],
                                     bench=base, run_tests=False)
            (self.home / f"gen{gen}-diagnosis.md").write_text(diag_mod.render_markdown(report))
            self._phase("proposing")
            hypotheses = self.propose(report, candidates)
        self.state["running"]["plan"]["hypotheses"] = [h.__dict__ for h in hypotheses]
        self._save()
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
        if self.state["running"]["plan"].get("mode") == "adopt":
            merged_what, stack_entry = self._adopt(gen, champ, base, results, budget_left, repairs)
        else:
            merged_what, stack_entry = self._compete(gen, champ, base, results, budget_left, screen_tasks,
                                                     parallel, finalists, stack)
        merged = None
        if merged_what:
            merged = git(self.repo, "rev-parse", "HEAD")
            git(self.repo, "tag", "-f", f"evolve-gen-{gen}", merged)
            # Some git proxies refuse tag pushes; a branch marks the generation everywhere.
            git(self.repo, "branch", "-f", f"evolve/gen-{gen}", merged)
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

    def _compete(self, gen, champ, base, results, budget_left, screen_tasks, parallel, finalists, stack):
        """Free hypotheses compete: screen, benchmark finalists, merge what measurably beats the champion."""
        from concurrent.futures import ThreadPoolExecutor

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
        merged_what = []
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
        for e in winners:
            if e["branch"] not in merged_what:
                e["reason"] += " (accepted alone, not merged: " + (
                    "excluded from the stack)" if stack_entry else "a better candidate won)")
        return merged_what, stack_entry

    # --- adopt: planned roadmap features --------------------------------
    def _adopt(self, gen, champ, base, results, budget_left, repairs):
        """Roadmap items are adopted, not ranked. All tested candidates are integrated on one branch and measured
        once; on a confirmed regression the culprits are found on the regressed tasks only, sent back to their
        team with the failing checks, re-measured, and adopted when fixed. Nothing is dropped silently."""
        for e in results:  # failing tests are a reason to fix, not to drop
            for _ in range(repairs):
                if e.get("stage") != "tests" or budget_left() <= 0:
                    break
                if self._repair(e, f"The test suite fails after your change:\n{e.get('tests_tail', '')}"):
                    e.update(stage="tested", reason="", tests={"ok": True, "summary": e["repairs"][-1]["tests"]})
            if e.get("stage") == "tests":
                e.update(stage="blocked", reason=f"{e.get('reason', '')}; still failing after {repairs} repair rounds")
        tested = [e for e in results if e.get("stage") == "tested"]
        adopted: list = []
        pending = list(tested)
        stack_entry = None
        for round_no in range(repairs + 1):
            if not pending or budget_left() <= 0:
                break
            self._phase(f"integrating {len(adopted + pending)} items, tests after each merge (round {round_no})")
            wt, included, excluded = self._integrate(gen, champ, adopted + pending, f"r{round_no}")
            for e, why in excluded.items():
                ent = next(x for x in pending + adopted if x["branch"] == e)
                ent.update(accepted=False, reason=why, stage="integrate")
                if ent in pending:
                    pending.remove(ent)
                if ent in adopted:
                    adopted.remove(ent)
            new = [e for e in pending if e["branch"] in included]
            try:
                self._phase(f"benchmarking the integration of {len(included)} items (round {round_no})")
                suite = self._bench_call(wt, f"g{gen}-adopt-r{round_no}")
                cmp = bench_mod.compare(base, suite)
                regressed = self._confirm(wt, cmp, f"g{gen}-adopt-r{round_no}")
                d = self.policy.adopt(cmp, {"ok": True}, regressed)
            finally:
                self._remove_worktree(wt)
            stack_entry = {"kind": "stack", "generation": gen, "branch": f"evolve/g{gen}-adopt", "parent": champ,
                           "included": included, "excluded": excluded, "round": round_no, "accepted": d.accepted,
                           "reason": d.reason, "delta": cmp["delta"], "per_task": cmp["per_task"],
                           "summary": cmp["candidate"], "regressed": regressed,
                           "hypothesis": {"title": f"adoption round {round_no} of generation {gen}", "source": "stack"}}
            self.record(**stack_entry)
            if d.accepted:
                for e in new:
                    e.update(accepted=True, reason=d.reason, stage="adopted", delta=cmp["delta"])
                adopted += new
                pending = []
                break
            self._current_base = base
            self._phase(f"finding which item causes: {d.reason}")
            culprits = self._culprits(gen, new, regressed, cmp, round_no)
            if not culprits and len(new) > 1:
                # every item is fine alone but the combination is not: adopt the cheapest one now; the others are
                # re-measured on top of it in the next generation (incremental, never all-or-nothing)
                new.sort(key=lambda e: (e.get("solo") or {}).get("cost_ratio", 0))
                for e in new[1:]:
                    e.update(accepted=False, stage="deferred",
                             reason=f"fine alone ({(e.get('solo') or {}).get('cost_ratio', 0):+.0%} cost) but the "
                                    f"combination fails ({d.reason}); re-measured on the new main next generation")
                new = new[:1]
            for e in new:
                if e not in culprits:
                    e.update(accepted=True, reason=f"adopted: not involved in {d.reason}", stage="adopted")
                    adopted.append(e)
            pending = []
            if round_no == repairs:
                for e in culprits:
                    e.update(accepted=False, stage="blocked",
                             reason=f"{e.get('blame', d.reason)}; still failing after {repairs} repair rounds")
                break
            if culprits:
                self._phase(f"repairing {len(culprits)} items with the failing checks")
            for e in culprits:  # back to the team with the evidence
                if self._repair(e, f"After integration the benchmark shows: {e.get('blame', d.reason)}.\n"
                                   f"Failing hidden checks:\n{e.get('evidence', '')[:3000]}"):
                    pending.append(e)
                else:
                    last = (e.get("repairs") or [{}])[-1]
                    e.update(accepted=False, stage="blocked",
                             reason=f"{e.get('blame', d.reason)}; repair round {round_no + 1} failed: "
                                    f"{last.get('error') or last.get('tests') or 'no change'}")
        if not adopted:
            return [], stack_entry
        # final integration of everything adopted, tests after each merge, then merge into main
        self._phase(f"final integration of {len(adopted)} adopted items")
        wt, included, excluded = self._integrate(gen, champ, adopted, "final")
        self._remove_worktree(wt)
        for b, why in excluded.items():
            e = next(x for x in adopted if x["branch"] == b)
            e.update(accepted=False, reason=why, stage="integrate")
        if not included:
            return [], stack_entry
        git(self.repo, "checkout", "-q", self.state["main"])
        git(self.repo, "merge", "--no-ff", "-q", "-m",
            f"evolve gen {gen}: adopt {len(included)} roadmap items\n\n" +
            "\n".join(f"- {e['hypothesis']['title']}" for e in adopted if e["branch"] in included) +
            f"\n\n{stack_entry['reason'] if stack_entry else ''}", f"evolve/g{gen}-adopt")
        return included, stack_entry

    def _integrate(self, gen: int, champ: str, entries: list, label: str):
        """(Re)build the integration branch evolve/g<gen>-adopt from champ, merging entries one by one with tests."""
        branch = f"evolve/g{gen}-adopt"
        with self._git_lock:
            git(self.repo, "branch", "-f", branch, champ)
            wt = self.home / "worktrees" / f"{branch.replace('/', '_')}-{label}"
            if wt.exists():
                self._remove_worktree_locked(wt)
            git(self.repo, "worktree", "add", "-q", str(wt), branch)
        included, excluded = [], {}
        for e in entries:
            git(wt, "clean", "-fdq", check=False)  # test debris from the previous merge must not block the next
            m = subprocess.run(["git", *GIT_ID, "merge", "--no-ff", "-q", "-m", f"adopt: {e['hypothesis']['title']}",
                                e["branch"]], cwd=wt, capture_output=True, text=True)
            if m.returncode != 0:
                git(wt, "merge", "--abort", check=False)
                excluded[e["branch"]] = ("conflicts with another item; retried next generation on the new main: "
                                         + (m.stderr or m.stdout).strip()[-200:])
                continue
            tests = self.run_tests(wt)
            if not tests["ok"]:
                git(wt, "reset", "--hard", "-q", "HEAD~1")
                excluded[e["branch"]] = f"tests fail in combination: {tests['summary']}"
                continue
            included.append(e["branch"])
        self._push(branch, force=True)
        return wt, included, excluded

    def _confirm(self, code: Path, cmp: dict, label: str) -> list:
        """Re-run regressed tasks once; only regressions that repeat count."""
        if not cmp["regressed"]:
            return []
        again = self._bench_call(code, f"{label}-confirm", list(cmp["regressed"]))
        still = {r["id"] for r in again["results"] if not r.get("pass")}
        return [t for t in cmp["regressed"] if t in still]

    def _culprits(self, gen: int, new: list, regressed: list, cmp: dict, round_no: int) -> list:
        """Which items cause the regression: each runs the regressed tasks alone (or the full suite for cost)."""
        culprits = []
        for e in new:
            tasks = regressed or None
            suite = self._bench_call(Path(e["worktree"]), f"g{gen}-blame-{e['branch'].split('-')[1]}-r{round_no}",
                                     tasks)
            failing = [r for r in suite["results"] if not r.get("pass") and r["id"] in (regressed or [])]
            if regressed and failing:
                e["blame"] = f"breaks tasks: {', '.join(r['id'] for r in failing)}"
                e["evidence"] = "\n".join(f"[{r['id']}] " + "\n".join(
                    f"$ {c['command']}\n{c['output']}" for c in (r.get("checks") or []) if c.get("exit"))
                    + str(r.get("error", ""))[-800:] for r in failing)
                culprits.append(e)
            elif not regressed:
                base_cost = cmp["base"]["cost_usd"] or 1
                ratio = suite["summary"]["cost_usd"] / base_cost - 1
                e["solo"] = {"cost_ratio": round(ratio, 3), "pass_rate": suite["summary"]["pass_rate"]}
                broke = self._confirm(Path(e["worktree"]), bench_mod.compare(self._base_for(cmp), suite),
                                      f"g{gen}-blame-{e['branch'].split('-')[1]}-r{round_no}")
                if broke:
                    e["blame"] = f"breaks tasks: {', '.join(broke)}"
                    culprits.append(e)
                elif ratio > self.policy.adopt_cost_increase:
                    e["blame"] = f"cost {ratio:+.0%} on the benchmark"
                    e["evidence"] = json.dumps({r["id"]: r.get("cost_usd") for r in suite["results"]})
                    culprits.append(e)
        return culprits

    def _base_for(self, cmp: dict) -> dict:
        return self._current_base

    def _repair(self, e: dict, feedback: str) -> bool:
        """Send a candidate back to its team in the same worktree; True when it is committed and tests pass."""
        h = Hypothesis.from_dict(e["hypothesis"])
        h.plan = (f"{h.plan}\n\nREPAIR: the worktree already contains your implementation of this change. Do not "
                  f"start over; find and fix the cause.\n{feedback}")
        wt = Path(e["worktree"])
        try:
            impl = self.implement_fn(h, wt)
            commit = self._commit(wt, h)
            tests = self.run_tests(wt)
        except InfraError:
            raise
        except Exception as exc:  # noqa: BLE001
            e.setdefault("repairs", []).append({"error": repr(exc)[:300]})
            return False
        e.setdefault("repairs", []).append({"implementation": impl, "commit": commit, "tests": tests["summary"]})
        if not commit.get("changed"):
            e["repairs"][-1]["error"] = "the team made no change"
            return False
        return bool(tests["ok"])

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
        p = subprocess.run(["git", "push", "--quiet", "origin", self.state["main"]], cwd=self.repo, capture_output=True,
                           text=True, timeout=600)
        for b in git(self.repo, "branch", "--list", "evolve/*", "--format=%(refname:short)").split():
            self._push(b, force=b.endswith("-adopt"))
        tags = subprocess.run(["git", "push", "--quiet", "origin", "--tags"], cwd=self.repo, capture_output=True,
                              text=True, timeout=300)
        return {"pushed": p.returncode == 0, "tags_pushed": tags.returncode == 0,
                "detail": (p.stderr or p.stdout).strip()[-300:]}

    def _phase(self, phase: str) -> None:
        if "running" in self.state:
            self.state["running"]["phase"] = phase
            self._save()

    def reopen(self, gen: int, reason: str) -> dict:
        """Void the decisions of a finished generation whose measurements were invalid (e.g. a provider outage
        judged as regressions) and make it resumable: implementations are kept, measurement and selection rerun."""
        from ..roadmap import Roadmap

        if self.state.get("running"):
            raise RuntimeError("another generation is running; resume or finish it first")
        cands = [e for e in self.ledger() if e["kind"] in ("candidate", "absorb_candidate") and e["generation"] == gen]
        if not cands or any(e.get("merged") for e in cands):
            raise RuntimeError(f"generation {gen} has no candidates or already merged something; revert instead")
        picks = [e for e in self.ledger() if e["kind"] == "roadmap_pick" and e["ts"] < cands[0]["ts"]]
        roadmap = [h["hypothesis"]["source"].split(":", 1)[1] for h in cands
                   if h["hypothesis"].get("source", "").startswith("roadmap:")]
        plan = {"champ": cands[0]["parent"], "kind": cands[0]["kind"],
                "hypotheses": [e["hypothesis"] for e in cands],
                "branches": {e["branch"].split("-")[1]: e["branch"] for e in cands},
                **({"roadmap": roadmap, "mode": "adopt"} if roadmap and picks else {})}
        self.state["running"] = {"generation": gen, "since": time.time(), "phase": "reopened", "plan": plan}
        self._save()
        self.record(kind="voided", generation=gen, reason=reason, branches=list(plan["branches"].values()))
        if roadmap and (self.repo / "roadmap.yaml").exists():
            rm = Roadmap.of_repo(self.repo)
            for item_id in roadmap:
                rm.set_status(item_id, "planned", f"gen {gen} measurement voided ({reason}); remeasuring")
            self._commit_roadmap(f"roadmap: gen {gen} decisions voided — {reason}")
        return plan

    def resume(self, **opts) -> Optional[dict]:
        """Finish the generation that was interrupted (None if nothing was running)."""
        if not self.resume_plan_from_ledger():
            return None
        if self.state["running"]["plan"].get("roadmap"):
            return self.roadmap_generation(0, resume=True, **opts)
        return self.generation(resume=True, **opts)

    def run(self, generations: int = 1, candidates: int = 2, **opts) -> list[dict]:
        return [self.generation(candidates, **opts) for _ in range(generations)]

    # --- roadmap --------------------------------------------------------
    def roadmap_generation(self, n: int, wave: Optional[int] = None, ids: Optional[list] = None,
                           resume: bool = False, **opts) -> dict:
        """Implement the next ``n`` evolve items of roadmap.yaml (or exactly ``ids``) as one population;
        mark merged items done."""
        from ..roadmap import Roadmap

        rm = Roadmap.of_repo(self.repo)
        resume = resume and self.resume_plan_from_ledger()
        if resume:
            ids = self.state["running"]["plan"].get("roadmap") or ids
            n = max(n, len(ids or []))
        if ids:
            items = [rm.get(i) for i in ids if rm.get(i).get("status") in ("planned", "partial")
                     and rm.get(i).get("mode") == "evolve"][:n]
        else:
            items = rm.next_items(n, wave=wave)
        if not items:
            return {"roadmap": [], "reason": "no open evolve items"}
        hyps = [rm.to_hypothesis(it, self.home / "sources") for it in items]
        if not resume:
            self.record(kind="roadmap_pick", items=[it["id"] for it in items],
                        refs={h.source: h.refs for h in hyps})
        s = self.generation(len(hyps), hypotheses=None if resume else hyps, kind="candidate", resume=resume,
                            plan_extra={"roadmap": [it["id"] for it in items], "mode": "adopt"}, **opts)
        gen = s["generation"]
        cands = {e["branch"]: e for e in self.ledger() if e["kind"] == "candidate" and e.get("generation") == gen}
        outcome = {}
        for e in cands.values():
            src = (e.get("hypothesis") or {}).get("source", "")
            if not src.startswith("roadmap:"):
                continue
            item_id = src.split(":", 1)[1]
            if e.get("merged"):
                outcome[item_id] = ("done", f"evolve/gen-{gen} ({e['branch']})")
            elif e.get("stage") == "blocked":
                outcome[item_id] = ("blocked", f"gen {gen}: {e.get('reason', '')[:200]} — needs a design decision")
            else:
                outcome[item_id] = (rm.get(item_id).get("status"),
                                    f"gen {gen} {e.get('stage', '?')}: {e.get('reason', '')[:160]}")
        self.update_roadmap(outcome, gen)
        s["roadmap"] = {k: v[0] for k, v in outcome.items()}
        s["published"] = self.publish()
        return s

    def update_roadmap(self, outcome: dict, gen: int) -> Optional[str]:
        """Write statuses into roadmap.yaml + docs/ROADMAP.md on the main branch and commit."""
        from ..roadmap import Roadmap

        if not outcome:
            return None
        with self._git_lock:
            rm = Roadmap.of_repo(self.repo)
            for item_id, (status, note) in outcome.items():
                rm.set_status(item_id, status, note)
            done = [k for k, v in outcome.items() if v[0] == "done"]
            sha = self._commit_roadmap(f"roadmap: gen {gen} — done: {', '.join(done) or 'none'}; "
                                       f"attempted: {', '.join(outcome)}")
            if not sha:
                return None
            if git(self.repo, "branch", "--list", f"evolve/gen-{gen}"):
                git(self.repo, "branch", "-f", f"evolve/gen-{gen}", sha)
            return sha

    def _commit_roadmap(self, message: str) -> Optional[str]:
        """Re-render docs/ROADMAP.md and commit roadmap.yaml, studied.yaml and the render on the main branch."""
        from ..roadmap import Roadmap

        with self._git_lock:
            (self.repo / "docs").mkdir(exist_ok=True)
            (self.repo / "docs" / "ROADMAP.md").write_text(Roadmap.of_repo(self.repo).render_markdown())
            paths = [p for p in ("roadmap.yaml", "studied.yaml", "docs/ROADMAP.md") if (self.repo / p).exists()]
            git(self.repo, "add", *paths)
            if not git(self.repo, "diff", "--cached", "--name-only", check=False):
                return None
            git(self.repo, "commit", "-q", "-m", message)
            sha = git(self.repo, "rev-parse", "HEAD")
        self._push(self.state["main"])
        return sha

    def cycle(self, items: int = 2, study: int = 1, **opts) -> dict:
        """One turn of directed self-improvement: study the next unstudied harness from the registry
        (its findings join the roadmap), then implement the next roadmap items in plan order."""
        from ..roadmap import next_to_study

        studied = []
        for h in next_to_study(self.repo, n=study):
            try:
                out = self.absorb(h["repo"])
                studied.append({"repo": h["repo"], "added": out["roadmap_added"]})
            except Exception as e:  # an unreachable or broken source must not stop the cycle
                self.record(kind="absorb_error", source=h["repo"], error=str(e)[:500])
                studied.append({"repo": h["repo"], "error": str(e)[:200]})
        gen = self.roadmap_generation(items, **opts) if items else {}
        return {"studied": studied, "generation": gen}

    # --- absorb ---------------------------------------------------------
    def absorb(self, source: str, focus: str = "", top: int = 3, run: bool = False, min_fit: float = 0.6) -> dict:
        """Study another harness's code; its worthwhile features become roadmap items (wave ABSORB_WAVE) with the
        donor's source files attached. ``run`` implements the new evolve items right away (out of plan order)."""
        from ..roadmap import ABSORB_WAVE, Roadmap, add_studied

        src = Path(source).expanduser()
        label = source.rstrip("/").removesuffix(".git")
        label = "/".join(label.split("/")[-2:]) if not src.exists() else str(src)
        name = slug(src.name if src.exists() else label.split("/")[-1])
        if not src.exists():
            url = source if "://" in source or source.startswith("git@") else f"https://github.com/{source}.git"
            dest = self.home / "sources" / label.replace("/", "__")
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(dest)], check=True, timeout=900,
                               capture_output=True)
            src = dest
        lic = detect_license(src)
        rm = Roadmap.of_repo(self.repo) if (self.repo / "roadmap.yaml").exists() else None
        goal = (f"Study the harness at {src} completely and find features worth absorbing into Artel (codebase at "
                f"{self.repo}). {('Focus: ' + focus) if focus else ''}\n\nArtel today:\n<code_map>\n{self.code_map()}"
                f"\n</code_map>\n\n<roadmap>\n{rm.text_for_agents() if rm else adopted_table(self.repo)}\n</roadmap>"
                f"\n\nLicense of the source: {lic}. Finish with absorb_report.")
        report = self.agent_fn("absorber", goal, [str(src)], "absorb_report")
        feats = sorted(report["features"], key=lambda f: -float(f.get("fit", 0)))
        out = {"source": source, "path": str(src), "license": lic, "summary": report.get("summary", ""),
               "features": feats, "roadmap_added": []}
        adir = self.home / "absorb"
        adir.mkdir(exist_ok=True)
        (adir / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        if rm:
            items = []
            for f in feats:
                if float(f.get("fit", 0)) < min_fit or f.get("duplicates"):
                    continue
                mode = "design" if f.get("pillar") or f.get("mode") == "design" else "evolve"
                items.append({
                    "id": f"A-{slug(name, 16)}-{slug(f['name'], 28)}", "wave": ABSORB_WAVE, "mode": mode,
                    "status": "planned", "title": f"{f['name']} — {f.get('description', '')}"[:200],
                    "source": {"harness": name, "repo": label, "paths": list(f.get("source_files") or []),
                               "license": lic},
                    "target": [f["target"]] if f.get("target") else [], "fit": round(float(f.get("fit", 0)), 2),
                    **({"pillar": f["pillar"]} if f.get("pillar") else {}),
                    "plan": f.get("port_plan", ""),
                    "acceptance": [x for x in (f.get("expected"), "tests for the new behavior",
                                               "no benchmark regression") if x]})
            out["roadmap_added"] = rm.add_items(items, header=f"absorbed from {label} ({lic})")
            add_studied(self.repo, {"repo": label, "license": lic, "found": len(feats),
                                    "added": out["roadmap_added"],
                                    "skipped": {f["name"]: (f"duplicates {f['duplicates']}" if f.get("duplicates")
                                                            else f"fit {f.get('fit')}") for f in feats
                                                if f.get("duplicates") or float(f.get("fit", 0)) < min_fit},
                                    "summary": report.get("summary", "")[:400]})
            out["commit"] = self._commit_roadmap(
                f"roadmap: absorbed {label} ({lic}) — {len(out['roadmap_added'])} items: "
                f"{', '.join(out['roadmap_added']) or 'none'}")
        self.record(kind="absorb", source=source, license=lic, features=[f["name"] for f in feats],
                    summary=report.get("summary", ""), roadmap_added=out["roadmap_added"])
        if run and feats:
            if rm:
                out["generation"] = self.roadmap_generation(top, ids=out["roadmap_added"])
            else:
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
