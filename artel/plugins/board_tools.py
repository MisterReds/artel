"""Board and coordination tools, including the terminal tools that end a role's loop.

A terminal tool is the only way a loop finishes successfully: a worker must
``task_submit`` (with evidence) or ``task_block`` (with proof); a reviewer must
return ``review_verdict``; the planner ``submit_plan`` and so on. Ending a turn
without one triggers the continuation prompt instead of stopping.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..kernel import ToolSpec
from ..tooling import ToolContext, ToolError

NON_CODE = {".md", ".markdown", ".txt", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".html", ".svg"}


def board_view(ctx: ToolContext, task_id: str = "") -> str:
    board = ctx.project.board
    if task_id:
        t = board.get(task_id)
        if not t:
            raise ToolError(f"no task {task_id}")
        out = {k: t[k] for k in ("id", "title", "status", "assignee", "body", "criteria", "checks", "result",
                                  "evidence", "attempts")}
        out["depends_on"] = board.deps_of(task_id)
        out["comments"] = [f"{c['author']}: {c['body']}" for c in board.comments(task_id)][-15:]
        return json.dumps(out, ensure_ascii=False, indent=1)
    lines = []
    for t in board.list():
        res = f" — {t['result'][:160]}" if t.get("result") else ""
        lines.append(f"[{t['status']}] {t['id']} ({t['assignee'] or '-'}) {t['title']}{res}")
    return "\n".join(lines)


def task_comment(ctx: ToolContext, body: str, task_id: str = "") -> str:
    tid = task_id or (ctx.task or {}).get("id")
    if not tid or not ctx.project.board.get(tid):
        raise ToolError("task_id required")
    ctx.project.board.comment(tid, ctx.actor, body)
    return "comment posted"


def _terminal(ctx: ToolContext, name: str, payload: dict) -> str:
    ctx.terminal = (name, payload)
    return f"{name} accepted"


def task_submit(ctx: ToolContext, summary: str, evidence: str, artifacts: list = None) -> str:
    if not evidence.strip():
        raise ToolError("evidence is required: paste the command output, test results or file listing that "
                        "proves each acceptance criterion is met")
    code_edits = [f for f in ctx.edited_files if Path(f).suffix.lower() not in NON_CODE]
    if code_edits and not ctx.outputs:
        raise ToolError(f"you edited code ({', '.join(code_edits[:5])}) but ran nothing this run; execute it or its "
                        "tests with `bash` and submit that output as evidence")
    missing = [a for a in (artifacts or []) if not ctx.resolve(a).exists()]
    if missing:
        raise ToolError(f"artifacts do not exist: {missing}")
    return _terminal(ctx, "task_submit", {"summary": summary, "evidence": evidence, "artifacts": artifacts or []})


def task_block(ctx: ToolContext, reason: str, evidence: str, needs_human: bool = False) -> str:
    if len(evidence.strip()) < 20:
        raise ToolError("a blocker needs host-observable proof (error output, missing access, failed command); "
                        "keep looking for safe progress if you cannot prove it")
    return _terminal(ctx, "task_block", {"reason": reason, "evidence": evidence, "needs_human": bool(needs_human)})


def review_verdict(ctx: ToolContext, passed: bool, feedback: str, failed_criteria: list = None) -> str:
    if not passed and not feedback.strip():
        raise ToolError("a failing verdict needs actionable feedback")
    return _terminal(ctx, "review_verdict", {"passed": bool(passed), "feedback": feedback,
                                             "failed_criteria": failed_criteria or []})


def audit_verdict(ctx: ToolContext, complete: bool, summary: str, missing: list = None) -> str:
    missing = missing or []
    if not complete and not missing:
        raise ToolError("an incomplete verdict must list the missing work as new tasks")
    for m in missing:
        if not m.get("title") or not m.get("criteria"):
            raise ToolError("each missing item needs a title and acceptance criteria")
    return _terminal(ctx, "audit_verdict", {"complete": bool(complete), "summary": summary, "missing": missing})


def replan_decision(ctx: ToolContext, action: str, rationale: str, guidance: str = "", assignee: str = "",
                    new_worker: dict = None, subtasks: list = None) -> str:
    if action not in ("retry", "reassign", "split", "cancel", "escalate"):
        raise ToolError("action must be retry | reassign | split | cancel | escalate")
    if action == "split" and not subtasks:
        raise ToolError("split needs subtasks")
    if action == "reassign" and not (assignee or new_worker):
        raise ToolError("reassign needs assignee or new_worker")
    return _terminal(ctx, "replan_decision", {"action": action, "rationale": rationale, "guidance": guidance,
                                              "assignee": assignee, "new_worker": new_worker or {},
                                              "subtasks": subtasks or []})


def validate_plan(workers: list, tasks: list) -> None:
    if not workers:
        raise ToolError("plan needs at least one worker")
    names = {w.get("name") for w in workers}
    if None in names or "" in names:
        raise ToolError("every worker needs a name")
    keys = [t.get("key") for t in tasks]
    if not tasks or None in keys or len(set(keys)) != len(keys):
        raise ToolError("tasks need unique `key` values")
    for t in tasks:
        if not t.get("criteria"):
            raise ToolError(f"task {t['key']} needs acceptance criteria")
        for d in t.get("depends_on", []):
            if d not in keys:
                raise ToolError(f"task {t['key']} depends on unknown key {d}")
    graph = {t["key"]: list(t.get("depends_on", [])) for t in tasks}
    state: dict = {}

    def visit(k: str) -> None:
        if state.get(k) == 1:
            raise ToolError(f"dependency cycle through {k}")
        if state.get(k) == 2:
            return
        state[k] = 1
        for d in graph[k]:
            visit(d)
        state[k] = 2

    for k in graph:
        visit(k)


def submit_plan(ctx: ToolContext, project_name: str, workers: list, tasks: list) -> str:
    validate_plan(workers, tasks)
    return _terminal(ctx, "submit_plan", {"project_name": project_name, "workers": workers, "tasks": tasks})


def review_done(ctx: ToolContext, summary: str) -> str:
    return _terminal(ctx, "review_done", {"summary": summary})


def harness_edit(ctx: ToolContext, target: str, content: str, evidence: str, worker: str = "",
                 op: str = "append") -> str:
    refine = ctx.kernel.get("refine")
    try:
        res = refine.apply(ctx.project, target=target, content=content, evidence=evidence, worker=worker, op=op,
                           actor=ctx.actor)
    except ValueError as exc:
        raise ToolError(str(exc))
    ctx.state.setdefault("harness_edits", []).append(res)
    return json.dumps(res, ensure_ascii=False)


def refine_done(ctx: ToolContext, summary: str) -> str:
    return _terminal(ctx, "refine_done", {"summary": summary})


TASK_ITEM = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "short unique id used in depends_on"},
        "title": {"type": "string"},
        "body": {"type": "string", "description": "what to do, inputs, expected outputs and where to put them"},
        "assignee": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"},
                     "description": "independently checkable acceptance criteria"},
        "checks": {"type": "array", "items": {"type": "string"},
                   "description": "optional shell commands run in shared/ that must exit 0"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer"},
    },
    "required": ["key", "title", "body", "assignee", "criteria"],
}
WORKER_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "role": {"type": "string"},
        "soul": {"type": "string", "description": "personality, values, standards — who this bot is"},
        "rules": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}, "description": "library skills to preload"},
        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
    },
    "required": ["name", "role", "soul"],
}


def setup(kernel) -> None:
    S = {"type": "object", "additionalProperties": False}
    kernel.register_tool(ToolSpec(
        "board_view", "View the kanban board, or one task in detail (results, criteria, comments).",
        {**S, "properties": {"task_id": {"type": "string"}}, "required": []}, board_view,
        roles=("worker", "reviewer", "auditor", "manager", "refiner")))
    kernel.register_tool(ToolSpec(
        "task_comment", "Post a comment on a task (yours by default): progress, handoff notes, questions for "
        "teammates.",
        {**S, "properties": {"body": {"type": "string"}, "task_id": {"type": "string"}}, "required": ["body"]},
        task_comment, roles=("worker", "manager")))
    kernel.register_tool(ToolSpec(
        "task_submit", "Submit your task for review once EVERY acceptance criterion is met. `evidence` must contain "
        "real proof (command output, test results, file listings). Reviewers verify independently.",
        {**S, "properties": {"summary": {"type": "string"}, "evidence": {"type": "string"},
                             "artifacts": {"type": "array", "items": {"type": "string"}}},
         "required": ["summary", "evidence"]}, task_submit, roles=("worker",), terminal=True))
    kernel.register_tool(ToolSpec(
        "task_block", "Declare a hard blocker you cannot work around, with host-observable proof. Set needs_human "
        "only for access/credentials/decisions only a human can provide.",
        {**S, "properties": {"reason": {"type": "string"}, "evidence": {"type": "string"},
                             "needs_human": {"type": "boolean"}}, "required": ["reason", "evidence"]},
        task_block, roles=("worker",), terminal=True))
    kernel.register_tool(ToolSpec(
        "review_verdict", "Deliver the review verdict. Pass only if every criterion is verifiably met.",
        {**S, "properties": {"passed": {"type": "boolean"}, "feedback": {"type": "string"},
                             "failed_criteria": {"type": "array", "items": {"type": "string"}}},
         "required": ["passed", "feedback"]}, review_verdict, roles=("reviewer",), terminal=True))
    kernel.register_tool(ToolSpec(
        "audit_verdict", "Deliver the goal audit. If anything required by the goal is missing or weak, set "
        "complete=false and list the missing work as new tasks.",
        {**S, "properties": {"complete": {"type": "boolean"}, "summary": {"type": "string"},
                             "missing": {"type": "array", "items": TASK_ITEM}},
         "required": ["complete", "summary"]}, audit_verdict, roles=("auditor",), terminal=True))
    kernel.register_tool(ToolSpec(
        "replan_decision", "Decide how to unstick a task: retry (with guidance), reassign (to an existing or new "
        "worker), split (into subtasks), cancel (not needed for the goal), escalate (human required).",
        {**S, "properties": {
            "action": {"type": "string", "enum": ["retry", "reassign", "split", "cancel", "escalate"]},
            "rationale": {"type": "string"}, "guidance": {"type": "string"}, "assignee": {"type": "string"},
            "new_worker": WORKER_ITEM, "subtasks": {"type": "array", "items": TASK_ITEM}},
         "required": ["action", "rationale"]}, replan_decision, roles=("manager",), terminal=True))
    kernel.register_tool(ToolSpec(
        "submit_plan", "Submit the project plan: the worker roster and the task DAG.",
        {**S, "properties": {"project_name": {"type": "string"},
                             "workers": {"type": "array", "items": WORKER_ITEM},
                             "tasks": {"type": "array", "items": TASK_ITEM}},
         "required": ["project_name", "workers", "tasks"]}, submit_plan, roles=("planner",), terminal=True))
    kernel.register_tool(ToolSpec(
        "review_done", "Finish the skill review with a one-paragraph summary of what changed (or 'Nothing to save').",
        {**S, "properties": {"summary": {"type": "string"}}, "required": ["summary"]}, review_done,
        roles=("librarian",), terminal=True))
    kernel.register_tool(ToolSpec(
        "harness_edit", "Apply one small, evidence-backed edit to harness state. target: prompt_notes (shared "
        "guidance for every agent in this project) or worker_rules (one worker's RULES.md; set worker).",
        {**S, "properties": {"target": {"type": "string", "enum": ["prompt_notes", "worker_rules"]},
                             "content": {"type": "string"}, "evidence": {"type": "string"},
                             "worker": {"type": "string"}, "op": {"type": "string", "enum": ["append", "replace"]}},
         "required": ["target", "content", "evidence"]}, harness_edit, roles=("refiner",)))
    kernel.register_tool(ToolSpec(
        "refine_done", "Finish refinement with a summary of edits made.",
        {**S, "properties": {"summary": {"type": "string"}}, "required": ["summary"]}, refine_done,
        roles=("refiner",), terminal=True))
