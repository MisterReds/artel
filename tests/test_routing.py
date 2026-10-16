"""Tiers: the expensive model only manages and sees a brief of constant size; near its budget the work goes on
on the medium model; the metrics say how much of the expensive budget a project used."""
from artel import routing
from artel.agent import Outcome
from artel.manager import MANAGER_BRIEF_CHARS, Manager, new_kernel
from artel.project import DEFAULT_CONFIG, Project, deep_merge


def _project(tmp_path, monkeypatch, extra=None):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", **(extra or {})})
    return cfg, Project.create("g" * 3000, cfg, name="r", workdir=str(tmp_path / "w"))


def test_roles_map_to_tiers_and_runtime_models(tmp_path, monkeypatch):
    cfg, p = _project(tmp_path, monkeypatch)
    assert routing.model_for(cfg, "claude_code", "planner", p) == "opus"
    assert routing.model_for(cfg, "claude_code", "auditor", p) == "opus"
    assert routing.model_for(cfg, "claude_code", "worker", p) == "sonnet"
    assert routing.model_for(cfg, "claude_code", "librarian", p) == "haiku"
    assert routing.model_for(cfg, "codex", "worker", p) is None  # no names configured: Codex keeps its own model
    off = deep_merge(cfg, {"tiers": {"enabled": False}})
    assert routing.model_for(off, "claude_code", "planner", p) is None


def test_near_the_expensive_budget_the_manager_moves_to_medium_and_work_goes_on(tmp_path, monkeypatch):
    cfg, p = _project(tmp_path, monkeypatch, {"tiers": {"expensive_budget_tokens": 1000, "degrade_at": 0.9}})
    assert routing.tier_for(cfg, "planner", p) == "expensive"
    routing.record(p, "expensive", {"input_tokens": 850, "output_tokens": 60})
    assert routing.tier_for(cfg, "planner", p) == "medium"  # degraded, not stopped
    assert routing.model_for(cfg, "claude_code", "manager", p) == "sonnet"
    assert routing.tier_for(cfg, "librarian", p) == "cheap"


def test_manager_briefs_stay_small_however_big_the_project(tmp_path, monkeypatch):
    cfg, p = _project(tmp_path, monkeypatch)
    m = Manager(new_kernel(cfg), p)
    root = p.board.add_task("goal", "g", kind="goal")
    p.set_meta(root_task=root)
    ids = [p.board.add_task(f"task {i} " + "x" * 200, "body " * 400, criteria=["c" * 300] * 10) for i in range(300)]
    for i in ids[:250]:
        p.board.update(i, status="done", result="r" * 5000)
    for _ in range(20):
        p.board.comment(ids[260], "worker", "long comment " * 500)
    assert len(m.audit_brief()) <= MANAGER_BRIEF_CHARS
    assert len(m.manager_brief(p.board.get(ids[260]))) <= MANAGER_BRIEF_CHARS
    eff = routing.efficiency(p)
    assert eff["success_rate"] == 1.0 and eff["expensive_share"] is None


def test_run_agent_accounts_tokens_per_tier(tmp_path, monkeypatch):
    from artel import runtimes

    cfg, p = _project(tmp_path, monkeypatch)
    out = Outcome(status="terminal", terminal=("submit_plan", {}))
    out.usage = {"input_tokens": 100, "output_tokens": 20}
    monkeypatch.setattr(runtimes, "_run_agent", lambda *a, **kw: out)
    runtimes.run_agent(new_kernel(cfg), p, role="planner")
    runtimes.run_agent(new_kernel(cfg), p, role="worker")
    t = p.meta["tiers"]
    assert t["expensive_tokens"] == 120 and t["medium_tokens"] == 120 and t["expensive_calls"] == 1
    assert any(e["kind"] == "tier/usage" and e["role"] == "planner" for e in p.log.read())
    assert routing.efficiency(p)["expensive_share"] == 0.5
