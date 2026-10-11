"""The manager: keeps the project moving until the goal is verifiably met.

Loop (one tick):
    recover orphans -> promote ready tasks -> collect finished jobs -> schedule jobs

Jobs run in a thread pool and are keyed by purpose:
    work:<task>     a worker run (agent loop with task_submit/task_block)
    review:<task>   shell gates + independent reviewer
    replan:<task>   manager decision for a stuck task (circuit breaker / blocker)
    audit           goal audit once every task is done; adds tasks if the goal is not met
    skills:<task>   librarian skill review after an accepted non-trivial task (Hermes)
    refine          continual-harness refinement (Prime) after failure streaks and at the end

Nothing ends a project except: the audit confirming the goal, a budget limit, a
human stop, or every remaining path waiting on a human.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

from . import prompts
from .agent import Outcome
from . import guardian
from .runtimes import run_agent
from .snapshot import Snapshot
from .tools import shell_env, shell_run
from .project import Persona, Project, persona_from_dict
from .tooling import ToolContext, window_text
from .tz import fmt_ts, zone_name
from .worktree import Isolation, IsolationError, isolation_enabled

# Size bounds for context handed to the reviewer / auditor (head+tail windows keep the verifying lines)
REVIEW_SUMMARY_CHARS = 3000
REVIEW_EVIDENCE_CHARS = 4000
GATE_OUTPUT_CHARS = 1500
REVIEW_GATES_CHARS = 5000
AUDIT_RESULT_CHARS = 1200

TICK_S = 0.5
BACKOFF_BASE_S = 10
BACKOFF_CAP_S = 600
REFINE_EVERY_FAILURES = 3


START_OVER_RE = re.compile(r"start(ing)?\s+(over|from\s+scratch)|\brevert|\bdiscard", re.I)


def wants_start_over(feedback: str) -> bool:
    """True when reviewer feedback asks to throw the attempt away (e.g. 'START OVER')."""
    return bool(START_OVER_RE.search(feedback or ""))


def _fmt_list(items: list) -> str:
    return "\n".join(f"- {x}" for x in items) or "- (none)"


class Manager:
    def __init__(self, kernel, project: Project, stop_event: Optional[threading.Event] = None):
        self.kernel = kernel
        self.project = project
        self.board = project.board
        self.cfg = kernel.config
        self.b = self.cfg["budgets"]
        self.stop = stop_event or threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=int(self.b["max_parallel"]) + 4, thread_name_prefix="artel")
        self._wake = threading.Event()  # set when a job finishes, so the loop does not sleep through it
        self.jobs: dict[str, Future] = {}
        self.background: list[Future] = []
        self.digests: dict[str, Outcome] = {}
        self.review_retries: dict[str, int] = {}
        self.failures_since_refine = 0
        self.run_failures = 0  # never reset; gates the final refine
        self.tool_calls = 0    # worker tool calls over the whole run; with run_failures it defines an "easy" run
        self.snapshots: dict[str, str] = {}  # task id -> tree hash of shared/ before the pending attempt
        self.snapper = Snapshot(project.root / "snapshot.git", project.shared)
        self.last_refine = 0.0
        self.iso: Optional[Isolation] = None  # per-task worktrees (W2-worktree-isolation), opt-in
        if isolation_enabled(self.cfg):
            if Isolation.available():
                self.iso = Isolation(project)
            else:
                self.note("isolation_unavailable", reason="git is not installed; tasks share the deliverables")
        self.risk: dict[str, str] = {t: r["risk"] for t, r in guardian.latest_risk(project.log.read()).items()}

    # --- helpers --------------------------------------------------------
    def note(self, kind: str, **payload: Any) -> None:
        self.project.log.append(f"manager/{kind}", **payload)

    def _submit(self, key: str, fn, *args) -> None:
        fut = self.pool.submit(fn, *args)
        fut.add_done_callback(lambda _f: self._wake.set())  # the loop reacts at once, not on the next tick
        self.jobs[key] = fut
        self.note("job_started", job=key)

    def _bg(self, fn, *args) -> None:
        self.background.append(self.pool.submit(fn, *args))

    def ask_doctor(self, task_id: Optional[str], problem: str) -> None:
        """A task (or the project) stopped for a person: the diagnostician explains why, on an available model,
        and writes the answer into the task — so the person sees a cause and steps, not only an error."""
        if not (self.cfg.get("doctor") or {}).get("auto", True):
            return
        key = (task_id, (self.board.get(task_id) or {}).get("attempts") if task_id else None)
        seen = self.__dict__.setdefault("_doctor_seen", set())
        if key in seen:
            return
        seen.add(key)

        def run():
            from .doctor import diagnose, format_report
            from .runtimes import runtime_for

            rep = diagnose(self.cfg, problem, self.project, task_id, exclude_runtime=runtime_for(self.cfg, "worker"))
            self.project.log.append("doctor/report", task_id=task_id, cause=rep.get("cause"), who=rep.get("who"),
                                    steps=rep.get("steps"), by=rep.get("by"), report_id=rep.get("id"))
            if task_id:
                self.board.comment(task_id, "Диагност", format_report(rep))
            else:
                self.project.set_meta(diagnosis=rep)

        if task_id:
            self._bg(run)
        else:
            threading.Thread(target=run, daemon=True, name="doctor").start()

    def _roster(self) -> list[Persona]:
        return self.project.personas()

    def _assignee(self, name: Optional[str]) -> str:
        roster = self._roster()
        names = {p.name.lower(): p.name for p in roster}
        if name and name.lower() in names:
            return names[name.lower()]
        return roster[0].name

    @property
    def root_id(self) -> Optional[str]:
        return self.project.meta.get("root_task")

    # --- planning -------------------------------------------------------
    def plan(self) -> None:
        skills = self.kernel.get("skills")
        user = (f"<goal>\n{self.project.goal}\n</goal>\n\nSkill library index:\n{skills.index(query=self.project.goal, config=self.cfg) or '(empty)'}\n\n"
                "Plan the project now.")
        ctx = ToolContext(self.kernel, self.project, role="planner", workspace=self.project.shared)
        from .runtimes import runtime_for

        self.note("planning", runtime=runtime_for(self.cfg, "planner"))
        out = run_agent(self.kernel, self.project, role="planner", system=prompts.PLANNER, first_user=user, ctx=ctx,
                       max_turns=12, purpose="plan", stop_event=self.stop)
        if out.name != "submit_plan":
            raise RuntimeError(f"planning failed: {out.status} {out.error}")
        self.apply_plan(out.payload)

    def apply_plan(self, plan: dict) -> None:
        for w in plan["workers"]:
            self.project.hire(persona_from_dict(w))
        root = self.board.add_task(self.project.goal[:200], self.project.goal, kind="goal",
                                   criteria=["The goal is met, verified by the completion audit"], created_by="planner")
        self.board.update(root, status="running", actor="planner")
        self._add_tasks(plan["tasks"], created_by="planner")
        self.project.set_meta(root_task=root, name=plan.get("project_name") or self.project.meta["name"],
                              status="running")
        self.note("planned", workers=[w["name"] for w in plan["workers"]], tasks=len(plan["tasks"]))

    def _add_tasks(self, tasks: list, created_by: str, extra_deps: tuple = ()) -> dict:
        """Insert tasks with local ``key`` references in dependency order; returns key -> id."""
        ids: dict[str, str] = {}
        pending = list(tasks)
        while pending:
            progressed = False
            for t in list(pending):
                local = [d for d in t.get("depends_on", []) if d in {x.get("key") for x in tasks}]
                if all(d in ids for d in local):
                    deps = [ids[d] for d in local] + [d for d in t.get("depends_on", []) if self.board.get(d)]
                    ids[t["key"]] = self.board.add_task(
                        t["title"], t.get("body", ""), assignee=self._assignee(t.get("assignee")),
                        criteria=t.get("criteria", []), checks=t.get("checks", []),
                        depends_on=deps + list(extra_deps), priority=int(t.get("priority", 0)), created_by=created_by)
                    pending.remove(t)
                    progressed = True
            if not progressed:
                raise ValueError("task graph has a cycle or unknown dependency")
        return ids

    # --- prompts --------------------------------------------------------
    def worker_system(self, persona: Persona) -> str:
        files = self.project.worker_files(persona.name)
        skills = self.kernel.get("skills")
        preloaded = []
        for name in persona.skills:
            try:
                preloaded.append(f"<skill name=\"{name}\">\n{skills.view(name)}\n</skill>")
            except Exception:  # noqa: BLE001 - a missing preload must not block work
                continue
        notes = self.project.prompt_notes().strip()
        return prompts.WORKER.format(
            name=persona.name, role=persona.role, soul=files.get("SOUL.md", ""),
            rules=files.get("RULES.md", ""), memory=files.get("MEMORY.md", ""),
            notes=f"\n<project_notes>\n{notes}\n</project_notes>\n" if notes else "",
            skills=skills.index(query=f"{persona.role}. {self.project.goal}", config=self.cfg) or "(empty)",
            preloaded=("\nPreloaded skills:\n" + "\n".join(preloaded)) if preloaded else "")

    def task_card(self, task: dict, include_feedback: bool = True) -> str:
        deps = []
        for d in self.board.deps_of(task["id"]):
            dt = self.board.get(d)
            if dt and dt["status"] == "done":
                deps.append(f"- {dt['title']} ({dt['assignee']}): {window_text(dt['result'] or '', 1500)}")
        feedback = []
        if include_feedback:
            for c in self.board.comments(task["id"])[-12:]:
                feedback.append(f"[{c['author']}] {c['body'][:2000]}")
        checks = f"<checks run_in=\"shared/\">\n{_fmt_list(task['checks'])}\n</checks>\n" if task["checks"] else ""
        return prompts.TASK_CARD.format(
            id=task["id"], title=task["title"], body=task["body"], criteria=_fmt_list(task["criteria"]),
            checks=checks,
            deps=f"<completed_dependencies>\n{chr(10).join(deps)}\n</completed_dependencies>\n" if deps else "",
            feedback=f"<history>\n{chr(10).join(feedback)}\n</history>\n" if feedback else "",
            goal=self.project.goal, attempt=task["attempts"] + 1)

    # --- jobs -----------------------------------------------------------
    def job_work(self, task: dict) -> tuple[Outcome, ToolContext]:
        persona = self.project.persona(task["assignee"])
        run_id = self.board.start_run(task["id"], persona.name)
        workspace = self.project.worker_dir(persona.name) / "workspace"
        ctx = ToolContext(self.kernel, self.project, role="worker", workspace=workspace, persona=persona, task=task,
                          run_id=run_id)
        effort = self.difficulty_effort(task)
        if effort:
            ctx.state["effort"] = effort
        if self.risk.get(task["id"]) == "high":  # an earlier run was high risk: bash commands get blocking review
            ctx.state["guardian"] = "blocking"
        seq0 = self.project.log.seq
        if self.iso is not None:  # the worker writes into its own worktree; shared/ changes only on acceptance
            try:
                ctx.deliverables = self.iso.ensure(task["id"])
            except IsolationError as exc:
                out = Outcome(status="error", error=f"isolation: {exc}")
                self.board.finish_run(run_id, "error", out.error)
                return out, ctx
            self.note("worktree", task_id=task["id"], path=str(ctx.deliverables))
        elif task["id"] not in self.snapshots:  # a pending snapshot predates the first run of this attempt
            tree = self.snapper.track()
            if tree:
                self.snapshots[task["id"]] = tree
                self.note("snapshot", task_id=task["id"], tree=tree)
        try:
            out = run_agent(self.kernel, self.project, role="worker", system=self.worker_system(persona),
                           first_user=self.task_card(task), ctx=ctx, max_turns=int(self.b["max_turns_per_run"]),
                           purpose="work", persona=persona, stop_event=self.stop)
        except Exception as exc:  # noqa: BLE001 - a crashed run is retried, never fatal
            out = Outcome(status="error", error=repr(exc))
        status = {"task_submit": "submitted", "task_block": "blocked"}.get(out.name or "", out.status)
        summary = out.payload.get("summary") or out.payload.get("reason") or out.error
        self.board.finish_run(run_id, status, summary or "", turns=out.turns, tool_calls=out.tool_calls,
                              tokens_in=out.usage["input_tokens"], tokens_out=out.usage["output_tokens"],
                              cost_usd=round(out.cost_usd, 6))
        self.assess_run(task, ctx, seq0)
        return out, ctx

    def assess_run(self, task: dict, ctx: ToolContext, seq0: int) -> None:
        """Guardian: score the finished worker run from its events and remember a high risk for later runs."""
        try:
            events = [e for e in self.project.log.read(after=seq0, kinds={"tool/call", "tool/result"})
                      if e.get("task_id") == task["id"] and e.get("actor") == ctx.actor]
            res = guardian.refine(self.kernel, self.project, guardian.assess(events, ctx.workspace), events)
            self.project.log.append("guardian/assessed", task_id=task["id"], run_id=ctx.run_id, **res)
            if res["risk"] == "high":
                self.risk[task["id"]] = "high"
                self.board.comment(task["id"], "guardian", f"HIGH RISK ({res['score']}): {res['reason']}. "
                                   "Later runs need blocking review of every shell command.")
        except Exception as exc:  # noqa: BLE001 - the guardian must never break a run
            self.note("guardian_error", task_id=task["id"], error=repr(exc))

    def difficulty_effort(self, task: dict) -> Optional[str]:
        """System One difficulty estimate -> effort for this run (first attempt only)."""
        if task["attempts"]:
            return None
        from .decide import decide, score

        ans = decide(self.kernel, self.project, "difficulty",
                     {"task": f"{task['title']}\n{task['body'][:3000]}", "criteria": "; ".join(task["criteria"])},
                     {"difficulty": score("How hard is this task for an experienced engineer?",
                                          ["trivial: a few obvious edits", "routine: standard work, some care",
                                           "hard: subtle logic, debugging or design needed"])})
        if not ans or "difficulty" not in ans or float(ans["difficulty"].get("confidence", 0)) < 0.6:
            return None
        return ["low", "medium", "high"][min(2, max(0, round(float(ans["difficulty"]["score"]))))]

    def run_gates(self, task: dict, cwd=None) -> list[dict]:
        """Run the task's shell checks in ``cwd`` (the task worktree under isolation, else shared/)."""
        from . import sandbox

        results = []
        cwd = cwd or self.project.shared
        # gates observe, they must not change the deliverable (bytecode / pytest caches made reviewers reject
        # work that had been cleaned up before the gate ran)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ARTEL_SHARED": str(cwd),
               "PYTEST_ADDOPTS": (os.environ.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider").strip()}
        sb = sandbox.settings(self.cfg)
        for cmd in task["checks"]:
            # gates run unsandboxed unless `sandbox.gates` asks for it (then writable: the gate cwd + temp)
            run = sandbox.plan(self.cfg, cmd, cwd=cwd, writable=[cwd], mode=None if sb["gates"] else "off")
            try:
                p = subprocess.run(run.argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=int(self.cfg.get("gate_timeout_s", 900)), env=shell_env(env))
                res = {"command": cmd, "exit": p.returncode, "output": window_text(p.stdout + p.stderr, 3000)}
            except subprocess.TimeoutExpired:
                res = {"command": cmd, "exit": -1, "output": "timed out"}
            results.append(res)
            self.project.log.append("gate/result", task_id=task["id"], **res)
        return results

    def job_review(self, task: dict) -> tuple[list, Outcome]:
        wt, changed = None, ""
        if self.iso is not None:
            # review the work as it would land: the current shared deliverables merged into the task's worktree
            try:
                conflicts = self.iso.integrate(task["id"])
            except IsolationError as exc:
                return [], Outcome(status="conflict", error=str(exc),
                                   terminal=("merge_conflict", {"conflicts": []}))
            if conflicts:
                return [], Outcome(status="conflict", error="",
                                   terminal=("merge_conflict", {"conflicts": conflicts}))
            wt = self.iso.path(task["id"])
            files = self.iso.changed_files(task["id"])
            changed = f"<changed_files vs shared>\n{_fmt_list(files[:200])}\n</changed_files>\n\n"
        gates = self.run_gates(task, cwd=wt)
        gate_text = window_text("\n".join(f"$ {g['command']}  -> exit {g['exit']}\n{window_text(g['output'], GATE_OUTPUT_CHARS)}"
                                         for g in gates) or "(no automated gates)", REVIEW_GATES_CHARS)
        user = (f"{self.task_card(task)}\n\n<submission by=\"{task['assignee']}\">\n<summary>\n{window_text(task['result'] or '', REVIEW_SUMMARY_CHARS)}\n"
                f"</summary>\n<evidence>\n{window_text(task['evidence'] or '', REVIEW_EVIDENCE_CHARS)}\n</evidence>\n</submission>\n\n"
                f"{changed}<gate_results>\n{gate_text}\n</gate_results>\n\nReview it now.")
        ctx = ToolContext(self.kernel, self.project, role="reviewer", workspace=wt or self.project.shared, task=task,
                          deliverables=wt)
        out = run_agent(self.kernel, self.project, role="reviewer", system=prompts.REVIEWER, first_user=user, ctx=ctx,
                       max_turns=25, purpose="review", stop_event=self.stop)
        return gates, out

    def triage(self, task: dict) -> Optional[str]:
        """Fast System One triage of a stuck task: retry | guidance | reassign | human, or None if unsure."""
        from .decide import choice, decide

        runs = self.board.runs(task["id"])
        state = {"task": f"{task['title']}\n{task['body'][:2000]}", "criteria": "; ".join(task["criteria"]),
                 "history": "\n".join(f"[{c['author']}] {c['body'][:500]}" for c in self.board.comments(task["id"])[-8:]),
                 "runs": "; ".join(f"{r['status']} after {r['turns']} turns: {(r['summary'] or '')[:200]}" for r in runs[-4:])}
        ans = decide(self.kernel, self.project, "replan_shortcut", state, {"action": choice(
            "What will most likely get this stuck task done?",
            {"retry": "The failure looks transient or the worker was close; retrying as-is is enough.",
             "guidance": "The worker misunderstood something and needs concrete corrective guidance.",
             "reassign": "This worker lacks the skills for the task; another specialist should take it.",
             "human": "Only a human can unblock it: missing credentials, access, or a business decision."})})
        if not ans or "action" not in ans:
            return None
        a = ans["action"]
        floor = float((self.cfg.get("decide") or {}).get("thresholds", {}).get("shortcut_min", 0.8))
        top = float((a.get("probabilities") or {}).get(a.get("choice"), 0))
        return a.get("choice") if top >= floor and a.get("choice") in ("retry", "human") else None

    def job_replan(self, task: dict) -> Outcome:
        fast = self.triage(task)
        if fast:
            action = {"retry": "retry", "human": "escalate"}[fast]
            return Outcome(status="terminal", terminal=("replan_decision", {
                "action": action, "rationale": f"System One triage: {fast}", "guidance": "", "assignee": "",
                "new_worker": {}, "subtasks": []}))
        runs = self.board.runs(task["id"])
        roster = "\n".join(f"- {p.name}: {p.role}" for p in self._roster())
        user = (f"{self.task_card(task)}\n\n<runs>\n" +
                "\n".join(f"- run {r['id']} by {r['worker']}: {r['status']} after {r['turns']} turns — "
                          f"{window_text(r['summary'] or '', 600)}" for r in runs) +
                f"\n</runs>\n\n<roster>\n{roster}\n</roster>\n\nBoard:\n" +
                "\n".join(f"[{t['status']}] {t['id']} ({t['assignee']}) {t['title']}" for t in self.board.list()) +
                "\n\nDecide now.")
        ctx = ToolContext(self.kernel, self.project, role="manager", workspace=self.project.shared, task=task)
        return run_agent(self.kernel, self.project, role="manager", system=prompts.MANAGER, first_user=user, ctx=ctx,
                        max_turns=15, purpose="replan", stop_event=self.stop)

    def job_audit(self) -> Outcome:
        done = [t for t in self.board.list(status=["done"]) if t["kind"] != "goal"]
        roster = "\n".join(f"- {p.name}: {p.role}" for p in self._roster())
        user = (f"<goal>\n{self.project.goal}\n</goal>\n\n<completed_tasks>\n" +
                "\n\n".join(f"## {t['title']} ({t['assignee']})\nCriteria:\n{_fmt_list(t['criteria'])}\n"
                            f"Result: {window_text(t['result'] or '', AUDIT_RESULT_CHARS)}" for t in done) +
                f"\n</completed_tasks>\n\n<roster>\n{roster}\n</roster>\n\nAudit round "
                f"{self.project.meta.get('audit_rounds', 0) + 1}. Inspect shared/ and audit now.")
        ctx = ToolContext(self.kernel, self.project, role="auditor", workspace=self.project.shared)
        return run_agent(self.kernel, self.project, role="auditor", system=prompts.AUDITOR, first_user=user, ctx=ctx,
                        max_turns=30, purpose="audit", stop_event=self.stop)

    def _gate(self, point: str, instructions: str, yes: str, no: str, context: str) -> bool:
        """System One gate in front of an expensive role; True = run it (also when undecided)."""
        from .decide import decide, noul

        ans = decide(self.kernel, self.project, point, {"trajectory": context[-12000:]},
                     {"go": noul(instructions, yes, no)})
        if not ans or "go" not in ans:
            return True
        floor = float((self.cfg.get("decide") or {}).get("thresholds", {}).get("skip_learning_below", 0.25))
        return float(ans["go"].get("noul", 1.0)) >= floor

    def job_skill_review(self, task: dict, out: Outcome) -> None:
        if not self._gate("skill_review", "Does this trajectory contain something worth saving as a reusable skill?",
                          "A non-obvious technique, a corrected mistake, a workaround or a pitfall a future agent "
                          "would benefit from.", "Routine work with no new technique, correction or pitfall.",
                          out.digest_text(12000)):
            return
        user = (f"{self.task_card(task, include_feedback=False)}\n\nOutcome: accepted by review.\n"
                f"Result: {task.get('result') or ''}\n\n<trajectory>\n{out.digest_text()}\n</trajectory>")
        ctx = ToolContext(self.kernel, self.project, role="librarian", workspace=self.project.shared, task=task)
        res = run_agent(self.kernel, self.project, role="librarian", system=prompts.LIBRARIAN, first_user=user,
                       ctx=ctx, max_turns=15, purpose="skill_review")
        self.project.log.append("skills/reviewed", task_id=task["id"], status=res.status,
                                summary=res.payload.get("summary", ""), actions=ctx.state.get("skill_actions", []))

    def job_refine(self) -> None:
        from .refine import trajectory_digest

        if not self._gate("refine", "Would a new project rule have prevented problems in this trajectory?",
                          "There were failures, rejections, stalls, repeated tool errors or wasted effort with a "
                          "clear cause.", "The run was clean or problems had no preventable cause.",
                          trajectory_digest(self.project, 120)):
            return

        roster = "\n".join(f"- {p.name}: {p.role}" for p in self._roster())
        user = (f"<goal>\n{self.project.goal}\n</goal>\n\n<roster>\n{roster}\n</roster>\n\n<current_prompt_notes>\n"
                f"{self.project.prompt_notes() or '(empty)'}\n</current_prompt_notes>\n\n<trajectory_digest>\n"
                f"{trajectory_digest(self.project)}\n</trajectory_digest>")
        ctx = ToolContext(self.kernel, self.project, role="refiner", workspace=self.project.shared)
        res = run_agent(self.kernel, self.project, role="refiner", system=prompts.REFINER, first_user=user, ctx=ctx,
                       max_turns=12, purpose="refine")
        self.project.log.append("harness/refined", status=res.status, summary=res.payload.get("summary", ""),
                                edits=ctx.state.get("harness_edits", []))

    # --- outcome handling ----------------------------------------------
    def _retry_later(self, task: dict, reason: str, count_attempt: bool = True) -> None:
        attempts = task["attempts"] + (1 if count_attempt else 0)
        no_prog = task["no_progress"] + 1
        if attempts >= int(self.b["max_attempts"]):
            self.board.update(task["id"], status="blocked", attempts=attempts, no_progress=no_prog)
            self.board.comment(task["id"], "manager", f"Circuit breaker after {attempts} attempts: {reason}. Replanning.")
            return
        base = float(self.cfg.get("backoff_base_s", BACKOFF_BASE_S))
        delay = min(BACKOFF_CAP_S, base * 2 ** (no_prog - 1))
        self.board.update(task["id"], status="ready", attempts=attempts, no_progress=no_prog,
                          backoff_until=time.time() + delay)
        self.board.comment(task["id"], "manager", f"Run ended without completion ({reason}). Workspace and files are "
                                                   f"preserved; continue from where it stopped. Retry in {delay}s.")
        self.failures_since_refine += 1
        self.run_failures += 1

    def on_work(self, task_id: str, result: tuple[Outcome, ToolContext]) -> None:
        out, _ctx = result
        task = self.board.get(task_id)
        self.run_failures += sum(1 for line in out.digest if " -> ERROR " in line)
        self.tool_calls += out.tool_calls
        if out.name == "task_submit":
            p = out.payload
            artifacts = "\n".join(p.get("artifacts", []))
            self.board.update(task_id, status="review", result=p["summary"] + (f"\nArtifacts:\n{artifacts}" if artifacts
                                                                                else ""), evidence=p["evidence"],
                              actor=task["assignee"])
            self.digests[task_id] = out
        elif out.name == "task_block":
            p = out.payload
            self.board.comment(task_id, task["assignee"], f"BLOCKED: {p['reason']}\nEvidence:\n{p['evidence']}")
            self.board.update(task_id, status="blocked", needs_human=1 if p.get("needs_human") else 0,
                              actor=task["assignee"])
            if p.get("needs_human"):
                self.ask_doctor(task_id, f"Исполнитель {task['assignee']} заблокирован: {p['reason']}\n{p['evidence']}")
            self.failures_since_refine += 1
            self.run_failures += 1
        elif out.status in ("stopped", "budget"):
            self.board.update(task_id, status="ready", actor="manager")
        else:
            self._failed(task, f"{out.status}: {out.error}".strip(": "))

    def _failed(self, task: dict, reason: str) -> None:
        """A run ended without a result. Retry only what a retry can fix."""
        from .failures import classify, retry_at

        kind = classify(reason)
        if kind == "setup":  # missing CLI, no login, bad key: every retry fails the same way
            self.board.update(task["id"], status="blocked", needs_human=1, actor="manager")
            self.board.comment(task["id"], "manager", f"Нужна настройка, повтор не поможет: {reason}")
            self.note("escalated", task_id=task["id"], rationale=f"setup: {reason}")
            self.ask_doctor(task["id"], reason)
            return
        if kind == "limit":  # the provider's limit: switch executor if auto has another one, else wait for the reset
            from . import limits
            from .runtimes import runtime_for

            alt = runtime_for(self.cfg, "worker")
            out = limits.load()  # the run that hit the limit recorded its runtime there (runtimes._note_limit)
            if out and alt != "none" and alt.split(":")[0] not in out:
                self.board.update(task["id"], status="ready", backoff_until=0, actor="manager")
                self.board.comment(task["id"], "manager", f"Лимит провайдера ({reason[:200]}); продолжаю на {alt}.")
                self.note("provider_switch", task_id=task["id"], to=alt, reason=reason)
                return
            until = retry_at(reason) or time.time() + 900
            self.board.update(task["id"], status="ready", backoff_until=until, actor="manager")
            self.board.comment(task["id"], "manager", f"Лимит провайдера ({reason}); повтор после "
                                                       f"{fmt_ts(until, '%H:%M', self.cfg)} ({zone_name(self.cfg)}).")
            self.note("provider_limit", task_id=task["id"], until=until, reason=reason)
            return
        self._retry_later(task, reason)

    def on_review(self, task_id: str, result: tuple[list, Outcome]) -> None:
        gates, out = result
        task = self.board.get(task_id)
        if out.name == "merge_conflict":  # the updated shared deliverables do not merge into this task's work
            self.review_retries.pop(task_id, None)
            self._merge_conflict(task, out.payload.get("conflicts") or [], out.error, stage="before review")
            return
        gates_ok = all(g["exit"] == 0 for g in gates)
        if out.name != "review_verdict":
            n = self.review_retries.get(task_id, 0) + 1
            self.review_retries[task_id] = n
            if out.status in ("stopped", "budget"):
                return
            if n < 3:
                self.note("review_retry", task_id=task_id, reason=out.status)
                return  # still in review; schedule() re-submits it
            passed, feedback = gates_ok and bool(gates), "reviewer produced no verdict; decided by gates only"
        else:
            passed = bool(out.payload["passed"]) and gates_ok
            feedback = out.payload["feedback"]
            if out.payload["passed"] and not gates_ok:
                feedback = "Automated gates failed:\n" + "\n".join(
                    f"$ {g['command']} -> exit {g['exit']}\n{window_text(g['output'], 800)}" for g in gates if g["exit"] != 0)
        self.review_retries.pop(task_id, None)
        snapshot = self.snapshots.pop(task_id, None)
        self.project.log.append("review/verdict", task_id=task_id, passed=passed, feedback=feedback,
                                gates=[{"command": g["command"], "exit": g["exit"]} for g in gates])
        if passed and self.iso is not None:
            try:
                merged, conflicts = self.iso.accept(task_id)
                error = ""
            except IsolationError as exc:
                merged, conflicts, error = False, [], str(exc)
            if not merged:  # never overwrite: the worker resolves on the updated base and the task is re-reviewed
                self.board.comment(task_id, "reviewer", f"ACCEPTED (not merged yet): {feedback}")
                self._merge_conflict(task, conflicts, error, stage="on acceptance")
                return
            self.note("merged", task_id=task_id)
        if passed:
            self.board.comment(task_id, "reviewer", f"ACCEPTED: {feedback}")
            self.board.update(task_id, status="done", no_progress=0, actor="reviewer")
            self.kernel.emit("task_done", project=self.project, task=self.board.get(task_id), feedback=feedback)
            digest = self.digests.pop(task_id, None)
            threshold = int(self.cfg["skills"]["review_after_tool_calls"])
            troubled = task["attempts"] > 0 or (digest is not None and any(" -> ERROR " in d for d in digest.digest))
            if digest is not None and digest.tool_calls >= threshold and (troubled or digest.tool_calls >= 2 * threshold):
                self._bg(self.job_skill_review, self.board.get(task_id), digest)
        else:
            self.board.comment(task_id, "reviewer", f"REJECTED: {feedback}")
            if self.iso is not None and wants_start_over(feedback):
                try:
                    if self.iso.discard(task_id):
                        self.board.comment(task_id, "manager", "manager: discarded the attempt; the task worktree "
                                                               "is reset to the current shared deliverables")
                        self.note("worktree_discard", task_id=task_id)
                except IsolationError as exc:
                    self.note("worktree_error", task_id=task_id, error=str(exc))
            elif snapshot and wants_start_over(feedback) and self.snapper.restore(snapshot):
                self.board.comment(task_id, "manager", "manager: restored deliverables to pre-attempt snapshot")
                self.note("snapshot_restore", task_id=task_id, tree=snapshot)
            attempts = task["attempts"] + 1
            self.failures_since_refine += 1
            self.run_failures += 1
            if attempts >= int(self.b["max_attempts"]):
                self.board.update(task_id, status="blocked", attempts=attempts, actor="reviewer")
                self.board.comment(task_id, "manager", f"Circuit breaker after {attempts} rejected attempts. Replanning.")
            else:
                self.board.update(task_id, status="ready", attempts=attempts, actor="reviewer")

    def _merge_conflict(self, task: dict, conflicts: list, error: str, stage: str) -> None:
        """Surface a merge conflict as a task comment and send the task back for a retry on the updated base."""
        task_id = task["id"]
        files = ", ".join(conflicts) if conflicts else "(none listed)"
        self.note("merge_conflict", task_id=task_id, stage=stage, files=conflicts, error=error)
        body = (f"MERGE CONFLICT {stage}: other accepted work changed the same shared deliverables. Conflicting "
                f"files: {files}. " + (f"Details: {error}. " if error else "") +
                "Your worktree now contains the updated base merged with your work; resolve the conflict markers "
                "(<<<<<<< / >>>>>>>), keep both intents, re-run the checks and submit again. Nothing was "
                "overwritten in the shared deliverables.")
        self.board.comment(task_id, "manager", body)
        attempts = task["attempts"] + 1
        self.failures_since_refine += 1
        self.run_failures += 1
        if attempts >= int(self.b["max_attempts"]):
            self.board.update(task_id, status="blocked", attempts=attempts, actor="manager")
            self.board.comment(task_id, "manager", f"Circuit breaker after {attempts} attempts (merge conflicts). "
                                                   "Replanning.")
        else:
            self.board.update(task_id, status="ready", attempts=attempts, actor="manager")

    def _drop_worktree(self, task_id: str) -> None:
        if self.iso is not None:
            try:
                self.iso.drop(task_id)
            except IsolationError as exc:
                self.note("worktree_error", task_id=task_id, error=str(exc))

    def _replans(self, task_id: str) -> int:
        return sum(1 for e in self.project.log.read() if e["kind"] == "manager/decision" and e.get("task_id") == task_id)

    def on_replan(self, task_id: str, out: Outcome) -> None:
        task = self.board.get(task_id)
        limit = int(self.b.get("max_replans", 3))
        if out.status not in ("stopped", "budget") and self._replans(task_id) >= limit:
            # the same task keeps failing after re-planning: stop circling, hand it to a person with the history
            self.board.update(task_id, status="blocked", needs_human=1, actor="manager")
            self.board.comment(task_id, "manager", f"Перепланирование {limit} раз не помогло — нужна помощь человека. "
                                                    f"Последняя причина: {window_text(task.get('result') or '', 300)}")
            self.note("escalated", task_id=task_id, rationale=f"{limit} replans without progress")
            self.ask_doctor(task_id, f"Задача не сдвинулась после {limit} перепланирований: "
                                     f"{window_text(task.get('result') or '', 600)}")
            return
        if out.name != "replan_decision":
            if out.status in ("stopped", "budget"):
                return
            self.note("decision", task_id=task_id, action="retry", rationale=f"no decision ({out.status}); default retry")
            self.board.update(task_id, status="ready", attempts=0,
                              backoff_until=time.time() + float(self.cfg.get("backoff_base_s", BACKOFF_BASE_S)))
            return
        d = out.payload
        self.note("decision", task_id=task_id, **d)
        action = d["action"]
        if d.get("guidance"):
            self.board.comment(task_id, "manager", f"Guidance: {d['guidance']}")
        if action == "retry":
            self.board.update(task_id, status="ready", attempts=0, no_progress=0, backoff_until=0, needs_human=0)
        elif action == "reassign":
            name = d.get("assignee")
            if d.get("new_worker") and d["new_worker"].get("name"):
                self.project.hire(persona_from_dict({"role": "", "soul": "", **d["new_worker"]}))
                name = d["new_worker"]["name"]
            self.board.update(task_id, status="ready", assignee=self._assignee(name), attempts=0, no_progress=0,
                              backoff_until=0, needs_human=0)
        elif action == "split":
            ids = self._add_tasks(d["subtasks"], created_by="manager",
                                  extra_deps=tuple(self.board.deps_of(task_id)))
            for dependent in self.board.dependents_of(task_id):
                for new_id in ids.values():
                    self.board.add_dep(dependent, new_id)
            self.board.update(task_id, status="cancelled")
            self.board.comment(task_id, "manager", f"Split into {', '.join(ids.values())}")
            self._drop_worktree(task_id)
        elif action == "cancel":
            self.board.update(task_id, status="cancelled")
            self._drop_worktree(task_id)
        elif action == "escalate":
            self.board.update(task_id, status="blocked", needs_human=1)
            self.note("escalated", task_id=task_id, rationale=d["rationale"])
            self.ask_doctor(task_id, f"Менеджер передал задачу человеку: {d['rationale']}")

    def is_easy_run(self) -> bool:
        """Gating rule for the goal audit: only a small run with no trouble at all is "easy".

        Deterministic (counts only). Any failure, retry, rejection, tool error, second attempt, more tasks or more
        tool calls than the `roles` limits keeps the full audit, so hard goals are always audited.
        """
        roles = self.cfg.get("roles") or {}
        if not roles.get("skip_on_easy") or self.run_failures or self.project.meta.get("audit_rounds", 0):
            return False
        tasks = [t for t in self.board.list() if t["kind"] != "goal"]
        return (0 < len(tasks) <= int(roles.get("easy_max_tasks", 2)) and self.tool_calls <= int(roles.get("easy_max_tool_calls", 15))
                and all(t["status"] == "done" and t["attempts"] == 0 for t in tasks))

    def on_audit(self, out: Outcome) -> None:
        rounds = self.project.meta.get("audit_rounds", 0) + 1
        self.project.set_meta(audit_rounds=rounds)
        if out.name != "audit_verdict":
            if out.status not in ("stopped", "budget"):
                self.note("audit_retry", reason=out.status)
            return
        v = out.payload
        self.project.log.append("audit/verdict", round=rounds, **v)
        if v["complete"]:
            self.board.update(self.root_id, status="done", result=v["summary"], actor="auditor")
            self.board.comment(self.root_id, "auditor", f"GOAL MET: {v['summary']}")
            return
        ids = self._add_tasks([{**m, "key": m.get("key") or f"audit{rounds}-{i}"} for i, m in enumerate(v["missing"])],
                              created_by="auditor")
        self.board.comment(self.root_id, "auditor", f"Round {rounds}: goal not yet met — {v['summary']}\n"
                                                    f"Added tasks: {', '.join(ids.values())}")
        cap = int(self.b.get("max_audit_rounds") or 0)
        if cap and rounds >= cap:
            self.project.set_meta(status="needs_review")
            self.stop.set()

    # --- loop -----------------------------------------------------------
    def recover(self) -> None:
        """Reset orphans after a crash/restart: running tasks have no live worker in this process."""
        for t in self.board.list(status=["running"]):
            if t["kind"] == "goal":
                continue
            self.board.update(t["id"], status="ready", actor="manager")
            self.board.comment(t["id"], "manager", "Recovered after restart; continue from the preserved workspace.")
        for r in self.board.runs():
            if r["status"] == "running":
                self.board.finish_run(r["id"], "error", "orphaned by restart")

    def collect(self) -> None:
        for key, fut in list(self.jobs.items()):
            if not fut.done():
                continue
            del self.jobs[key]
            kind, _, tid = key.partition(":")
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - surface and retry
                self.note("job_crashed", job=key, error=repr(exc))
                if kind == "work":
                    self._retry_later(self.board.get(tid), f"crash: {exc!r}")
                continue
            self.note("job_finished", job=key)
            {"work": self.on_work, "review": self.on_review, "replan": self.on_replan}.get(
                kind, lambda _t, r: self.on_audit(r))(tid, res)

    def schedule(self) -> None:
        now = time.time()
        busy_workers = {self.board.get(k.partition(":")[2])["assignee"] for k in self.jobs if k.startswith("work:")}
        n_work = sum(1 for k in self.jobs if k.startswith("work:"))
        for t in self.board.list(status=["ready"]):
            if n_work >= int(self.b["max_parallel"]):
                break
            if t["backoff_until"] > now or t["assignee"] in busy_workers or f"work:{t['id']}" in self.jobs:
                continue
            if self.board.claim(t["id"], t["assignee"]):
                self._submit(f"work:{t['id']}", self.job_work, self.board.get(t["id"]))
                busy_workers.add(t["assignee"])
                n_work += 1
        for t in self.board.list(status=["review"]):
            if f"review:{t['id']}" not in self.jobs:
                self._submit(f"review:{t['id']}", self.job_review, t)
        for t in self.board.list(status=["blocked"]):
            if not t["needs_human"] and f"replan:{t['id']}" not in self.jobs:
                self._submit(f"replan:{t['id']}", self.job_replan, t)
        open_work = [t for t in self.board.list(status=["todo", "ready", "running", "review", "blocked"])
                     if t["kind"] != "goal"]
        if not open_work and not self.jobs and self.root_id and self.board.get(self.root_id)["status"] != "done":
            if self.is_easy_run():
                self.project.log.append("audit/skipped", reason="easy run: small, clean, accepted first time",
                                        tool_calls=self.tool_calls)
                self.board.update(self.root_id, status="done", result="Skipped goal audit: easy, clean run",
                                  actor="manager")
            else:
                self._submit("audit", self.job_audit)
        if (self.failures_since_refine >= REFINE_EVERY_FAILURES and now - self.last_refine > 300):
            self.failures_since_refine = 0
            self.last_refine = now
            self._bg(self.job_refine)

    def waiting_on_humans(self) -> bool:
        if self.jobs:
            return False
        open_work = [t for t in self.board.list(status=["todo", "ready", "review", "blocked"]) if t["kind"] != "goal"]
        if not open_work:
            return False
        if any(t["status"] in ("ready", "review") for t in open_work):
            return False
        if any(t["status"] == "blocked" and not t["needs_human"] for t in open_work):
            return False
        return any(t["needs_human"] for t in open_work)

    def run(self) -> str:
        budget = self.kernel.get("budget")
        skills = self.kernel.get("skills")
        sk = self.cfg["skills"]
        curated = skills.curate(sk["stale_after_days"], sk["archive_after_days"])
        if curated:
            self.project.log.append("skills/curated", actions=curated)
        self.recover()
        for plugin, error in self.kernel.failed.items():  # a plugin that failed to load is shown, never fatal
            self.project.log.append("plugin/failed", plugin=plugin, error=error)
        self.kernel.emit("project_start", project=self.project, resumed=bool(self.root_id))
        if not self.root_id:
            for _attempt in range(4):  # a provider limit during planning moves planning to the next executor
                try:
                    self.plan()
                    exc = None
                    break
                except Exception as e:  # noqa: BLE001
                    exc = e
                    from . import limits
                    from .failures import classify
                    from .runtimes import runtime_for

                    alt = runtime_for(self.cfg, "planner")
                    out = limits.load()
                    if classify(str(e)) != "limit" or not out or alt == "none" or alt.split(":")[0] in out:
                        break
                    self.note("provider_switch", to=alt, reason=str(e)[:300])
            if exc is not None:  # a project that cannot start must say why, not crash
                from .failures import classify

                reason = str(exc)
                kind = classify(reason)
                status = "waiting_human" if kind in ("setup", "limit") else "error"
                self.project.set_meta(status=status, blocked_reason=reason[:1000])
                self.note("escalated", rationale=f"planning: {reason[:500]}")
                self.ask_doctor(None, f"Проект не смог начаться (планирование): {reason}")
                self.project.log.append("project/status", status=status)
                self.kernel.emit("project_end", project=self.project, status=status, usage=self.project.meta["usage"])
                return status
        self.project.set_meta(status="running")
        final = "running"
        while True:
            if self.stop.is_set():
                final = self.project.meta["status"] if self.project.meta["status"] == "needs_review" else "stopped"
                break
            reason = budget(self.project) if budget else None
            if reason:
                self.note("budget_exhausted", reason=reason)
                final = "paused_budget"
                self.stop.set()
                break
            self.board.refresh_readiness()
            self.collect()
            if self.board.get(self.root_id)["status"] == "done":
                final = "done"
                break
            self.schedule()
            if self.waiting_on_humans():
                final = "waiting_human"
                break
            self._wake.wait(TICK_S)
            self._wake.clear()
        if final != "done":
            self.stop.set()
        for fut in list(self.jobs.values()):
            try:
                fut.result(timeout=600)
            except Exception:  # noqa: BLE001
                pass
        self.collect()
        if final == "done":
            if self.run_failures:
                self._bg(self.job_refine)
            else:
                self.project.log.append("harness/refine_skipped", reason="no failures this run")
        for fut in self.background:
            try:
                fut.result(timeout=900)
            except Exception as exc:  # noqa: BLE001
                self.note("background_error", error=repr(exc))
                try:
                    from . import reports

                    reports.record("exception", f"фоновая ошибка менеджера: {exc!r}"[:200], repr(exc),
                                   project=self.project.slug, config=self.cfg)
                except Exception:  # noqa: BLE001
                    pass
        self.pool.shutdown(wait=True)
        self.project.set_meta(status=final)
        self.note("finished", status=final, usage=self.project.meta["usage"])
        self.kernel.emit("project_end", project=self.project, status=final, usage=self.project.meta["usage"])
        return final


def human_comment(project: Project, task_id: str, body: str) -> dict:
    """A human message on a task. Unblocks tasks waiting on a human."""
    task = project.board.get(task_id)
    if task is None:
        raise KeyError(task_id)
    project.board.comment(task_id, "human", body)
    if task["status"] == "blocked" and task["needs_human"]:
        project.board.update(task_id, status="ready", needs_human=0, attempts=0, no_progress=0, backoff_until=0,
                             actor="human")
    return project.board.get(task_id)


def new_kernel(config: dict):
    """Build a kernel with the standard services wired in."""
    from .agent import budget_guard
    from .kernel import build_kernel
    from .project import artel_home
    from .refine import setup_refiner
    from .skills import SkillStore

    kernel = build_kernel(config)
    kernel.provide("skills", SkillStore(artel_home() / "skills"))
    kernel.provide("budget", budget_guard(config))
    setup_refiner(kernel)
    return kernel


def run_goal(goal: str, config: dict, name: Optional[str] = None, stop_event: Optional[threading.Event] = None,
             kernel=None, workdir=None, refs: tuple = (), seed=None) -> tuple[Project, str]:
    """Create a project and drive it. A kernel built here is shut down at the end (``session_shutdown``)."""
    own = kernel is None
    kernel = kernel or new_kernel(config)
    try:
        project = Project.create(goal, config, name=name, workdir=workdir, refs=refs, seed=seed)
        return project, Manager(kernel, project, stop_event).run()
    finally:
        if own:
            kernel.shutdown(reason="run_end")


def continue_after_human(project: Project) -> list:
    """A person pressed "continue": tasks waiting for them get another chance (fresh attempts)."""
    reopened = []
    for t in project.board.list(status=["blocked"]):
        if t["needs_human"]:
            project.board.update(t["id"], status="ready", needs_human=0, attempts=0, no_progress=0, backoff_until=0,
                                 actor="human")
            project.board.comment(t["id"], "human", "Продолжено человеком.")
            reopened.append(t["id"])
    if project.meta.get("blocked_reason"):
        project.set_meta(blocked_reason="")
    return reopened


def resume(slug: str, config: dict, stop_event: Optional[threading.Event] = None, kernel=None) -> tuple[Project, str]:
    own = kernel is None
    kernel = kernel or new_kernel(config)
    try:
        project = Project.open(slug, config)
        continue_after_human(project)
        return project, Manager(kernel, project, stop_event).run()
    finally:
        if own:
            kernel.shutdown(reason="run_end")


__all__ = ["Manager", "human_comment", "new_kernel", "run_goal", "resume", "json"]
