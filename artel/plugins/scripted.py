"""Deterministic offline provider: drives every role through the real harness
(tools, board, gates, skills, refine) without a model. Used by tests and demos.

Knobs (config["scripted"]):
    review_failures: int    reject the first N reviews (exercises the retry path)
    audit_missing: int      first N audits report missing work (exercises re-planning)
    delay_s: float          sleep per call (demos: watch projects run live)
    bad_check_tool: str     the build task's check starts with this (missing) program; a plan sent back with
                            <missing_tools> drops it unless bad_check_persists
    bad_check_persists: bool  keep the missing program even when the plan is sent back (exercises the replan path)
    idle_task: str          on attempt 1 of the task with this title, only talk (continuation -> stalled)
"""

from __future__ import annotations

import re
import threading
import uuid

from .llm_providers import LLMResponse


def _use(tool: str, **inp) -> dict:
    return {"type": "tool_use", "id": "tu_" + uuid.uuid4().hex[:12], "name": tool, "input": inp}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _first_text(messages: list) -> str:
    c = messages[0]["content"]
    return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))


def _tool_rounds(messages: list) -> int:
    return sum(1 for m in messages if m["role"] == "user" and isinstance(m["content"], list)
               and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]))


def _last_result(messages: list) -> str:
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    return str(b.get("content", ""))
    return ""


def _check_files(card: str) -> list[tuple[str, str]]:
    """(file, required_word) pairs from `test -s FILE` / `grep -q WORD FILE` gate commands."""
    out = []
    block = re.search(r"<checks[^>]*>(.*?)</checks>", card, re.S)
    for line in (block.group(1) if block else "").splitlines():
        m = re.search(r"grep -q (\S+) (\S+)", line)
        if m:
            out.append((m.group(2), m.group(1)))
            continue
        m = re.search(r"test -s (\S+)", line)
        if m:
            out.append((m.group(1), ""))
    return out


class ScriptedProvider:
    name = "scripted"

    def __init__(self, config: dict):
        self.knobs = dict(config.get("scripted", {}))
        self._lock = threading.Lock()
        self.counters = {"review": 0, "audit": 0}

    def _count(self, key: str) -> int:
        with self._lock:
            self.counters[key] += 1
            return self.counters[key]

    def chat(self, *, system, messages, tools, model, effort, max_tokens, purpose="") -> LLMResponse:
        if self.knobs.get("delay_s"):
            import time

            time.sleep(float(self.knobs["delay_s"]))
        blocks = getattr(self, f"_{purpose}", self._default)(messages, system)
        usage = {"input_tokens": 50 + 10 * len(messages), "output_tokens": 40}
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return LLMResponse(blocks=blocks, raw_content=blocks, stop_reason=stop, usage=usage, model="scripted")

    def _default(self, messages, system):
        return [_text("nothing to do")]

    def _intake(self, messages, system):
        req = re.search(r"<request>\s*(.*?)\s*</request>", _first_text(messages), re.S).group(1)
        return [_use("submit_contract", objective=req[:200], deliverables=["shared/deliverable.md"],
                     success_criteria=["the deliverable exists and is verified"], assumptions=["scripted run"],
                     complexity="medium")]

    def _plan(self, messages, system):
        goal = re.search(r"<goal>\s*(.*?)\s*</goal>", _first_text(messages), re.S).group(1)
        bad = self.knobs.get("bad_check_tool")
        if bad and "<missing_tools>" in _first_text(messages) and not self.knobs.get("bad_check_persists"):
            bad = None
        build_check = f"{bad} deliverable.md && test -s deliverable.md" if bad else "test -s deliverable.md"
        return [_text("Planning the project."), _use(
            "submit_plan", project_name=goal[:60],
            workers=[
                {"name": "Ada", "role": "researcher", "soul": "Curious and exact. Cites sources, never guesses.",
                 "rules": ["Write findings to shared/notes.md"], "skills": []},
                {"name": "Linus", "role": "engineer", "soul": "Pragmatic builder. Ships working things, tests them.",
                 "rules": ["Every deliverable is verified by a command"], "skills": []},
            ],
            tasks=[
                {"key": "research", "title": "Research and outline", "assignee": "Ada",
                 "body": f"Research the goal and write an outline to shared/notes.md.\nGoal: {goal}",
                 "criteria": ["shared/notes.md exists and outlines the approach"], "checks": ["test -s notes.md"]},
                {"key": "build", "title": "Build the deliverable", "assignee": "Linus", "depends_on": ["research"],
                 "body": "Produce shared/deliverable.md from the outline.",
                 "criteria": ["shared/deliverable.md exists"], "checks": [build_check]},
                {"key": "verify", "title": "Integrate and verify", "assignee": "Ada", "depends_on": ["build"],
                 "body": "Write shared/report.md confirming the deliverable; include the word VERIFIED.",
                 "criteria": ["report.md states VERIFIED"], "checks": ["grep -q VERIFIED report.md"]},
            ])]

    def _work(self, messages, system):
        card = _first_text(messages)
        rounds = _tool_rounds(messages)
        idle = self.knobs.get("idle_task")
        if idle and f"<title>{idle}</title>" in card and "Attempt 1." in card:
            return [_text("Thinking about the task without acting.")]
        if rounds == 0:
            return [_text("Inspecting the board and shared files."), _use("list_files", path="shared/"),
                    _use("skills_list", query="")]
        if rounds == 1:
            uses = []
            title = re.search(r"<title>(.*?)</title>", card, re.S).group(1)
            for f, word in _check_files(card) or [("output.md", "")]:
                uses.append(_use("write_file", path=f"shared/{f}",
                                 content=f"# {title}\n\n{('Status: ' + word) if word else 'Done.'}\n"))
            return [_text("Writing the deliverables.")] + uses
        if rounds == 2:
            return [_use("bash", command="ls -la \"$ARTEL_SHARED\"")]
        if rounds == 3:
            return [_use("memory_write", content="- Deliverables live in shared/; gates run there.")]
        evidence = _last_result(messages) if "ls -la" in str(messages) else "files written"
        for m in messages:
            if m["role"] == "user" and isinstance(m["content"], list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("content", "").startswith("[exit"):
                        evidence = b["content"]
        return [_use("task_submit", summary="Deliverables written and listed.", evidence=evidence)]

    def _review(self, messages, system):
        if _tool_rounds(messages) == 0:
            return [_use("list_files", path=".")]
        n = self._count("review")
        if n <= int(self.knobs.get("review_failures", 0)):
            return [_use("review_verdict", passed=False, feedback="Add more detail to the deliverable.",
                         failed_criteria=["detail"])]
        return [_use("review_verdict", passed=True, feedback="All criteria verified against the files.")]

    def _audit(self, messages, system):
        n = self._count("audit")
        if n <= int(self.knobs.get("audit_missing", 0)):
            return [_use("audit_verdict", complete=False, summary="A changelog is missing.", missing=[
                {"key": f"changelog{n}", "title": "Write a changelog", "assignee": "Linus",
                 "body": "Write shared/CHANGELOG.md.", "criteria": ["CHANGELOG.md exists"],
                 "checks": ["test -s CHANGELOG.md"]}])]
        return [_use("audit_verdict", complete=True, summary="Every requirement of the goal is met.")]

    def _verify(self, messages, system):
        return [_use("audit_verdict", complete=True, summary="Every requirement of the goal is met.")]

    def _skill_review(self, messages, system):
        rounds = _tool_rounds(messages)
        last = _last_result(messages)
        if rounds == 0:
            return [_use("skills_list", query="deliverable")]
        if rounds == 1:
            if "shared-deliverable-workflow" in last:
                return [_use("skill_view", name="shared-deliverable-workflow")]
            return [_use("skill_manage", action="create", name="shared-deliverable-workflow", category="workflow",
                         reason="first accepted task", content=(
                             "---\nname: shared-deliverable-workflow\ndescription: Produce gated deliverables in "
                             "shared/ and submit with command evidence.\n---\n# Shared Deliverable Workflow\n\n"
                             "## When to Use\n- A task has shell checks run in shared/\n\n## Procedure\n"
                             "1. Read the checks.\n2. Write files under shared/.\n3. List them with bash.\n"
                             "4. Submit with the listing as evidence.\n\n## Pitfalls\n- Gates run inside shared/, "
                             "so paths in checks are relative to it.\n\n## Verification\n- Every check exits 0.\n"))]
        if rounds == 2 and "Shared Deliverable Workflow" in last and "Re-run" not in last:
            return [_use("skill_manage", action="patch", name="shared-deliverable-workflow",
                         old="## Verification", new="- Re-run the checks yourself before submitting.\n\n## Verification",
                         reason="reviewers reject unverified submissions")]
        return [_use("review_done", summary="Skill library updated.")]

    def _refine(self, messages, system):
        if _tool_rounds(messages) == 0:
            return [_use("harness_edit", target="prompt_notes", op="append",
                         content="- Run every gate command yourself before task_submit.",
                         evidence="review/verdict and gate/result events in the trajectory")]
        return [_use("refine_done", summary="Added one project note.")]

    def _evolver(self, messages, system):
        return [_use("propose_hypotheses", hypotheses=[{
            "title": "Add an evolution marker file", "rationale": "scripted evidence",
            "plan": "write EVOLVED.md", "expected": "no metric change", "files": ["EVOLVED.md"]}])]

    def _absorber(self, messages, system):
        return [_use("absorb_report", summary="scripted analysis", features=[{
            "name": "marker", "description": "a marker feature", "source_files": ["README.md"],
            "value": "demonstrates absorb", "fit": 0.9, "target": "artel/", "port_plan": "write ABSORBED.md"}])]

    def _diagnose(self, messages, system):
        text = _first_text(messages)
        if _tool_rounds(messages) == 0 and "llama-server.log" in text:
            return [_use("list_files", path=".")]  # look before answering, like a model would
        m = re.search(r"Подсказки по известным причинам:\n- (.+?) \(исправляет: (\w+)\)", text)
        cause, who = (m.group(1), m.group(2)) if m else ("Причина не найдена в фактах.", "unknown")
        return [_text("Diagnosing."), _use("diagnosis_report", cause=cause, who=who,
                                           steps=["шаг из сценарного провайдера"], evidence="scripted", confidence=0.7)]

    def _replan(self, messages, system):
        brief = _first_text(messages)
        if 'failure_class="environment"' in brief:  # the check cannot run here: keep its tail, drop the missing tool
            block = re.search(r"<checks>\n(.*?)\n</checks>", brief, re.S)
            checks = [c[2:].split(" && ", 1)[-1] if " && " in c else "true"
                      for c in (block.group(1).splitlines() if block else [])]
            return [_use("replan_decision", action="retry", rationale="the check uses a tool this machine lacks",
                         guidance="The work is fine; the check is rewritten.", checks=checks)]
        return [_use("replan_decision", action="retry", rationale="transient failure",
                     guidance="Re-read the criteria and verify each with a command.")]


def register() -> None:
    from .llm_providers import register_provider

    register_provider("scripted", ScriptedProvider)
