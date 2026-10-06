"""Prompt-cache breadcrumbs and bounded reviewer/auditor context."""

from artel import manager as mgr
from artel.agent import Outcome
from artel.manager import Manager, new_kernel
from artel.plugins.llm_providers import cacheable
from artel.project import DEFAULT_CONFIG, Project, deep_merge


def test_system_and_last_tool_marked_cacheable():
    tools = [{"name": "a"}, {"name": "b"}]
    system, marked = cacheable("SYS", tools)
    assert system == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert marked[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked[0]
    assert "cache_control" not in tools[-1]          # input not mutated
    assert cacheable("SYS", [])[1] == []


def _manager(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("g", cfg, name="cost", workdir=str(tmp_path / "wd"))
    return Manager(new_kernel(cfg), project), project


def test_reviewer_context_is_bounded(tmp_path, monkeypatch):
    m, project = _manager(tmp_path, monkeypatch)
    tid = project.board.add_task("t", "b", criteria=["c"], assignee="Ada", checks=["python -c \"print('x' * 50000)\""])
    project.board.update(tid, status="review", result="R" * 100_000 + "RESULT-TAIL",
                         evidence="E" * 100_000 + "EVIDENCE-TAIL", actor="test")
    seen = {}

    def fake(kernel, project, **kw):
        seen["user"] = kw["first_user"]
        return Outcome(status="terminal")

    monkeypatch.setattr(mgr, "run_agent", fake)
    m.job_review(project.board.get(tid))
    user = seen["user"]
    bound = mgr.REVIEW_SUMMARY_CHARS + mgr.REVIEW_EVIDENCE_CHARS + mgr.REVIEW_GATES_CHARS
    assert len(user) < bound + 4000
    assert "RESULT-TAIL" in user and "EVIDENCE-TAIL" in user
