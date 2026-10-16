import pytest

from artel.manager import run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge

EASY = {"roles": {"easy_max_tasks": 3, "easy_max_tool_calls": 100}}  # the scripted plan has 3 small tasks


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


def kinds(project):
    return [e["kind"] for e in project.log.read()]


def roles_started(project):
    return {e["role"] for e in project.log.read() if e["kind"] == "agent/start"}


def test_easy_clean_run_skips_audit(cfg):
    project, status = run_goal("Write a short guide to SQLite WAL mode", deep_merge(cfg, EASY))
    assert status == "done"
    assert "audit/skipped" in kinds(project) and "audit/verdict" not in kinds(project)
    assert "auditor" not in roles_started(project)


def test_default_limits_keep_audit(cfg):  # 3 tasks exceed easy_max_tasks=2
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done" and "audit/verdict" in kinds(project)


def test_hard_run_keeps_audit_and_refine(cfg):  # a rejection makes the run hard even under easy limits
    over = deep_merge(EASY, {"scripted": {"review_failures": 1}})
    project, status = run_goal("Write a short guide to SQLite WAL mode", deep_merge(cfg, over))
    assert status == "done"
    assert "audit/verdict" in kinds(project) and "audit/skipped" not in kinds(project)
    assert "refiner" in roles_started(project)


def test_skip_can_be_disabled(cfg):
    over = deep_merge(EASY, {"roles": {"skip_on_easy": False}})
    project, _ = run_goal("Write a short guide to SQLite WAL mode", deep_merge(cfg, over))
    assert "audit/verdict" in kinds(project)


def test_cheap_roles_model_and_effort(tmp_path, monkeypatch):
    """Tiers (artel/routing.py): the expensive model only manages (plan, decisions, acceptance), the medium one does
    the work and the review, the cheap one the routine."""
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    p = Project.create("g", deep_merge(DEFAULT_CONFIG, {"provider": "anthropic"}), name="a", workdir=str(tmp_path / "w"))
    for role in ("librarian", "refiner", "doctor"):
        assert p.model_for(role) == "claude-haiku-5-5"
    for role in ("worker", "reviewer"):
        assert p.model_for(role) == "claude-sonnet-5-5"
    for role in ("planner", "manager", "auditor"):
        assert p.model_for(role) == "claude-opus-5-5"
    assert [p.effort_for(r) for r in ("librarian", "refiner", "auditor")] == ["low", "low", "medium"]
    assert p.effort_for("worker") == "high"


def test_cheap_model_overridable_and_provider_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "anthropic", "models": {"auditor": "my-model"}})
    p = Project.create("g", cfg, name="b", workdir=str(tmp_path / "w1"))
    assert p.model_for("auditor") == "my-model" and p.model_for("refiner") == "claude-haiku-5-5"
    other = Project.create("g", deep_merge(DEFAULT_CONFIG, {"provider": "openai_compat"}), name="c",
                           workdir=str(tmp_path / "w2"))
    assert other.model_for("auditor") == DEFAULT_CONFIG["models"]["default"]  # no claude name on other providers
