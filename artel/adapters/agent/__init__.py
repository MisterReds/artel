"""CLI agents behind artel.ports.AgentPort: one shared loop (loop.py) plus a thin codec per CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ...contracts import ErrorClass, ExecutorResult, ModelCard, TaskContract
from . import claude_code, codex, loop
from .loop import ToolBridge, run_agent  # noqa: F401 - re-exported


def run_claude_code(kernel, project, **kw):
    """Run one role on Claude Code (runtime ``claude_code``); same contract as :func:`artel.agent.run_loop`."""
    return loop.run_agent(claude_code, kernel, project, **kw)


def run_codex(kernel, project, **kw):
    """Run one role on Codex (runtime ``codex``); same contract as :func:`artel.agent.run_loop`."""
    return loop.run_agent(codex, kernel, project, **kw)


class CliAgent:
    """An :class:`artel.ports.AgentPort` over a codec: runs the contract through the shared loop in ``workspace``."""

    def __init__(self, codec, kernel, project, *, role: str = "worker", system: str = "", max_turns: int = 60):
        self.codec, self.kernel, self.project, self.role, self.system, self.max_turns = (
            codec, kernel, project, role, system, max_turns)

    def run(self, contract: TaskContract, workspace: str, tools: Sequence[str]) -> ExecutorResult:
        from ...tooling import ToolContext

        ctx = ToolContext(self.kernel, self.project, self.role, Path(workspace),
                          task={"id": contract.id, "title": contract.title})
        out = loop.run_agent(self.codec, self.kernel, self.project, role=self.role, system=self.system,
                             first_user=contract.input, ctx=ctx, max_turns=self.max_turns, purpose="task",
                             tool_names=list(tools))
        payload = out.payload
        blocked = out.name == "task_block"
        status = "blocked" if blocked else "done" if out.status == "terminal" else "failed"
        summary = str(payload.get("summary") or payload.get("reason") or out.error or "")
        usage = {"in": out.usage.get("input_tokens", 0), "out": out.usage.get("output_tokens", 0),
                 "cached": out.usage.get("cached", 0)}
        return ExecutorResult.from_dict({
            "task": contract.id, "status": status, "summary": summary, "evidence": [], "artifacts": [],
            "confidence": 1.0 if status == "done" else 0.0, "usage": usage,
            "blocked_on": (summary or "blocked") if blocked else None})

    def card(self) -> ModelCard:
        return ModelCard.from_dict({"id": self.codec.NAME, "provider": self.codec.NAME, "context_tokens": 0,
                                    "capabilities": ["tools", "agent"], "usd_per_mtok_in": 0.0,
                                    "usd_per_mtok_out": 0.0})

    def classify(self, err: BaseException) -> ErrorClass:
        try:
            return self.codec.classify(str(err))
        except Exception:  # noqa: BLE001 - the port promises classify never raises
            return ErrorClass("unknown", False, None)


def claude_code_agent(kernel, project, **kw) -> CliAgent:
    return CliAgent(claude_code, kernel, project, **kw)


def codex_agent(kernel, project, **kw) -> CliAgent:
    return CliAgent(codex, kernel, project, **kw)
