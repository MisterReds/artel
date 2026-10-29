"""Base behaviour a person hits first: a limited provider hands work to the next one, executors can be switched
off, Codex models come from the CLI (so a display name or a wrong case still works), and the dashboard shows who is
working right now."""
import time

import pytest

from artel import limits
from artel.project import DEFAULT_CONFIG, deep_merge
from tests.test_codex import fake_codex


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    import artel.runtimes as rt

    monkeypatch.setattr(rt, "connected_runtimes", lambda config, ttl=120.0: ["claude_code", "codex"])
    return tmp_path


def test_auto_skips_a_limited_runtime_and_returns_after_the_reset(home):
    from artel.runtimes import detect_runtime, runtime_for

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "anthropic"})
    assert detect_runtime(cfg) == "claude_code"
    limits.mark("claude_code", time.time() + 600, "usage limit")
    assert detect_runtime(cfg) == "codex" and runtime_for(cfg, "planner") == "codex"
    # an explicit choice is a preference, not a cage: while it is limited, work goes to the free executor
    assert runtime_for(deep_merge(cfg, {"runtime": "claude_code"}), "worker") == "codex"
    assert runtime_for(deep_merge(cfg, {"runtime": "claude_code", "failover": False}), "worker") == "claude_code"
    limits.mark("codex", time.time() + 60, "usage limit")  # both out: the one that resets first
    assert detect_runtime(cfg) == "codex"
    limits.clear()
    limits.mark("claude_code", time.time() - 1, "old")  # an expired limit is forgotten
    assert detect_runtime(cfg) == "claude_code" and limits.load() == {}


def test_disabled_runtime_is_never_picked(home):
    from artel.providers import save_routing
    from artel.runtimes import detect_runtime

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "anthropic"})
    r = save_routing(cfg, {"disabled_runtimes": ["claude_code", "bogus"]}, path=home / "artel.yaml")
    assert r["disabled_runtimes"] == ["claude_code"] and detect_runtime(cfg) == "codex"
    assert "claude_code" in (home / "artel.yaml").read_text()
    save_routing(cfg, {"disabled_runtimes": []}, path=home / "artel.yaml")
    assert detect_runtime(cfg) == "claude_code"


def test_a_retired_runtime_in_an_old_config_falls_back_to_auto(home):
    from artel.runtimes import runtime_for
    from artel.tools import install, uninstall

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "anthropic"})
    assert runtime_for(deep_merge(cfg, {"runtime": "dsh"}), "worker") == "claude_code"
    assert runtime_for(deep_merge(cfg, {"runtime_by_role": {"planner": "acp:gemini"}}), "planner") == "claude_code"
    assert install("dsh")["ok"] is False and uninstall("dsh")["ok"] is False


def test_limit_from_a_run_is_recorded_for_the_failover(home):
    from artel.agent import Outcome
    from artel.runtimes import _note_limit

    class Log:
        events = []

        def append(self, kind, **kw):
            self.events.append(kind)

    class P:
        log = Log()

    _note_limit("codex", Outcome(status="error", error="You've hit your usage limit. Try again at 5:00 PM."), P())
    _note_limit("claude_code", Outcome(status="error", error="tests failed"), P())
    assert list(limits.load()) == ["codex"] and P.log.events == ["provider/limited"]


def test_codex_models_come_from_the_cli_and_names_are_normalized(tmp_path, monkeypatch):
    from artel.providers import model_catalog, save_routing
    from artel.adapters.agent import codex

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    codex._catalog_cache.clear()
    cfg = deep_merge(DEFAULT_CONFIG, {"codex": {"binary": fake_codex(tmp_path)}})
    cat = model_catalog(cfg, "codex")
    assert [m["id"] for m in cat] == ["gpt-fake-luna", "gpt-fake-sol"] and cat[0]["efforts"] == ["low", "medium", "high"]
    r = save_routing(cfg, {"models": {"codex": "GPT-FAKE-LUNA"}}, path=tmp_path / "a.yaml")
    assert r["models"]["codex"] == "gpt-fake-luna" and r["catalogs"]["codex"] == cat
    with pytest.raises(ValueError, match="gpt-fake-sol"):
        save_routing(cfg, {"models": {"codex": "gpt-9"}}, path=tmp_path / "a.yaml")
    assert cfg["codex"]["model"] == "gpt-fake-luna"  # a rejected value changes nothing
    args = codex.build_args({**codex.DEFAULTS, "model": "GPT-Fake-Sol", "binary": fake_codex(tmp_path)},
                            workspace=tmp_path, shared=tmp_path, role="worker", bridge_url="http://x", bridge_token="t",
                            effort=None, thread_id=None, last_message=tmp_path / "m")
    assert "gpt-fake-sol" in args
    # autonomous: Codex never asks, and Artel's own tools are pre-approved so planning and the board work
    assert 'approval_policy="never"' in args and 'mcp_servers.artel.default_tools_approval_mode="approve"' in args


def test_active_agents_show_who_works_on_what():
    from artel.analytics import active_agents

    ev = [{"kind": "agent/start", "ts": 1, "actor": "planner", "role": "planner", "purpose": "plan", "model": "codex:gpt-6-luna"},
          {"kind": "tool/call", "ts": 2, "actor": "planner", "tool": "submit_plan"},
          {"kind": "agent/start", "ts": 3, "actor": "Ann", "role": "worker", "purpose": "work", "task_id": "t-1",
           "model": "claude-code:sonnet"},
          {"kind": "agent/end", "ts": 4, "actor": "planner", "purpose": "plan"},
          {"kind": "llm/error", "ts": 5, "actor": "Ann", "task_id": "t-1", "error": "overloaded"}]
    a = active_agents(ev[:2])
    assert a[0]["actor"] == "planner" and a[0]["runtime"] == "codex" and a[0]["last"] == "submit_plan"
    a = active_agents(ev)
    assert [x["actor"] for x in a] == ["Ann"] and a[0]["runtime"] == "claude_code" and "overloaded" in a[0]["last"]
    assert active_agents(ev, live=False) == []


def test_a_limited_task_moves_to_the_free_executor_instead_of_waiting(tmp_path, monkeypatch):
    from artel.manager import Manager, new_kernel, run_goal

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("limit switch", cfg)
    m = Manager(new_kernel(cfg), project)
    task = next(t for t in project.board.list() if t["kind"] != "goal")
    project.board.update(task["id"], status="running", needs_human=0, attempts=1)
    limits.mark("claude_code", time.time() + 3600, "usage limit")  # what the failed run recorded
    m._failed(project.board.get(task["id"]), "error: You've hit your usage limit · resets 4:20pm (UTC)")
    t = project.board.get(task["id"])
    assert t["status"] == "ready" and t["backoff_until"] == 0
    assert any(e["kind"] == "manager/provider_switch" for e in project.log.read())


def fake_claude(tmp_path, monkeypatch) -> str:
    import sys
    from pathlib import Path

    monkeypatch.setenv("FAKE_CLAUDE_HOME", str(tmp_path / "claude"))
    fake = str(Path(__file__).parent / "fakes" / "claude")
    if sys.platform != "win32":
        return fake
    shim = tmp_path / "claude.cmd"
    shim.write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8", newline="")
    return str(shim)


def test_claude_login_with_pasted_code_logout_and_models_from_the_cli(tmp_path, monkeypatch):
    from artel.providers import ClaudeLogin, claude_code_status, logout_claude, model_catalog

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"claude_code": {"binary": fake_claude(tmp_path, monkeypatch)},
                                      "agent_env": {"pass": ["FAKE_CLAUDE_HOME"]}})
    assert [m["id"] for m in model_catalog(cfg, "claude_code")] == ["fable", "opus", "sonnet", "haiku"]
    assert not claude_code_status(cfg)["logged_in"]
    login = ClaudeLogin(cfg)
    st = login.start()
    assert st["phase"] == "waiting" and "/oauth/authorize" in st["url"]
    assert login.submit("two words")["error"]  # nothing sent to the CLI, still waiting
    assert login.submit("bad-code")["phase"] == "error" and "400" in login.state["error"]
    assert login.start()["phase"] == "waiting" and login.submit("good-code")["phase"] == "done"
    assert claude_code_status(cfg)["logged_in"]
    assert logout_claude(cfg)["ok"] and not claude_code_status(cfg)["logged_in"]


def test_a_goal_audit_that_hits_a_limit_waits_for_the_reset_instead_of_looping(tmp_path, monkeypatch):
    """Generation 6 restarted a limited audit every tick: 3566 audits in two hours, 28 MB of events. A failed audit
    waits like a task: until the reset for a limit, a growing delay for other errors, a person after repeated ones."""
    import time

    from artel import limits
    from artel.agent import Outcome
    from artel.manager import MAX_AUDIT_FAILURES, Manager, new_kernel
    from artel.project import DEFAULT_CONFIG, Project, deep_merge

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(limits, "load", lambda: {"claude_code": {"until": time.time() + 3600}})
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "runtime": "claude_code"})
    project = Project.create("g", cfg, name="audit", workdir=str(tmp_path / "wd"))
    m = Manager(new_kernel(cfg), project)
    m.ask_doctor = lambda *a, **kw: None
    m.on_audit(Outcome(status="error", error="claude code failed: You've hit your session limit · resets 11:20am (UTC)"))
    assert m.audit_backoff_until > time.time() + 60 and not m.audit_blocked
    assert project.meta.get("audit_rounds", 0) == 0  # a failed audit is not a round
    m.on_audit(Outcome(status="error", error="model returned no verdict"))
    first = m.audit_backoff_until - time.time()
    m.on_audit(Outcome(status="error", error="model returned no verdict"))
    assert m.audit_backoff_until - time.time() > first  # the delay grows
    for _ in range(MAX_AUDIT_FAILURES):
        m.on_audit(Outcome(status="error", error="model returned no verdict"))
    assert m.audit_blocked
    m2 = Manager(new_kernel(cfg), project)
    m2.ask_doctor = lambda *a, **kw: None
    m2.on_audit(Outcome(status="error", error="Claude Code CLI не установлен (not installed)"))
    assert m2.audit_blocked
