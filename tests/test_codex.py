import json
import os
import sys
import time
from pathlib import Path

import pytest

from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext

FAKE = str(Path(__file__).parent / "fakes" / "codex")


def fake_codex(tmp_path) -> str:
    """The fake CLI is a Python script; Windows cannot run a shebang script, so it gets a .cmd shim (the same shape
    as an npm-installed codex.cmd)."""
    if sys.platform != "win32":
        return FAKE
    shim = tmp_path / "codex.cmd"
    shim.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8", newline="")
    return str(shim)


FAKE_CODEX_VARS = ["FAKE_CODEX_CMD", "FAKE_CODEX_LAZY", "FAKE_CODEX_LIMIT", "FAKE_CODEX_LOG", "FAKE_CODEX_NO_USAGE",
                   "FAKE_CODEX_SANDBOX", "FAKE_CODEX_STDERR_KB", "FAKE_CODEX_USAGE_DELAY", "FAKE_CODEX_WRITE"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "calls.jsonl"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05, "codex": {"binary": fake_codex(tmp_path)},
                                      # the fake CLI is steered by FAKE_* variables: name them, as a person would
                                      "agent_env": {"pass": FAKE_CODEX_VARS}})
    project, _ = run_goal("codex", cfg)
    return cfg, project, tmp_path


def _run(cfg, project, role="worker"):
    from artel.runtimes import run_agent

    cfg = deep_merge(cfg, {"runtime_by_role": {role: "codex"}})
    k = new_kernel(cfg)
    ws = project.shared
    ctx = ToolContext(k, project, role=role, workspace=ws, task={"id": "t-1"})
    out = run_agent(k, project, role=role, system="You are a worker.", first_user="Write hello.py", ctx=ctx,
                    max_turns=40, purpose="test")
    return out, ctx


def test_codex_runtime_does_work_and_finishes_through_artel_tools(env):
    cfg, project, tmp = env
    t0 = time.time()
    out, ctx = _run(cfg, project)
    assert out.status == "terminal" and ctx.terminal[0] == "task_submit" and time.time() - t0 < 4
    assert (project.shared / "hello.py").exists() and "hello.py" in ctx.edited_files
    ev = project.log.read()
    kinds = [e["kind"] for e in ev if e.get("runtime") == "codex"]
    assert {"agent/start", "llm/response", "tool/call", "tool/result", "agent/end"} <= set(kinds)
    shell = [e for e in ev if e["kind"] == "tool/call" and e.get("tool") == "shell"]
    assert shell and shell[0]["input"]["command"] == "python hello.py"
    assert any(e["kind"] == "tool/call" and e.get("tool") == "task_submit" for e in ev)  # via the bridge
    start = next(e for e in ev if e["kind"] == "agent/start" and e.get("runtime") == "codex")
    assert start["model"] == "codex:default"
    args = json.loads((tmp / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert args[:2] == ["exec", "--json"] and "workspace-write" in args and "--add-dir" in args
    assert 'approval_policy="never"' in args and args[-1] == "-"
    assert out.usage["input_tokens"] == 1000 and out.usage["output_tokens"] == 200


def test_codex_without_terminal_tool_is_resumed_and_readonly_roles_get_readonly_sandbox(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_CODEX_LAZY", "1")
    out, ctx = _run(cfg, project)
    calls = [json.loads(x) for x in (tmp / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert out.status == "terminal" and len(calls) == 2 and calls[1][-3:] == ["resume", "th-1", "-"]
    assert any(e["kind"] == "agent/continuation" and e.get("runtime") == "codex" for e in project.log.read())
    from artel.runtimes.codex import build_args, codex_settings

    scratch = tmp / "scratch"
    a = build_args(codex_settings(cfg), workspace=scratch, shared=tmp, role="reviewer", bridge_url="u",
                   bridge_token="t", effort="high", thread_id=None, last_message=tmp / "m")
    # judges: workspace-write rooted at a scratch dir, deliverables never a writable root, network on
    assert a[a.index("-s") + 1] == "workspace-write" and a[a.index("-C") + 1] == str(scratch)
    assert "--add-dir" not in a and "sandbox_workspace_write.network_access=true" in a
    assert 'model_reasoning_effort="high"' in a


def test_codex_usage_limit_is_a_provider_failure(env, monkeypatch):
    from artel.evolve.engine import infra_problem

    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_CODEX_LIMIT", "1")
    out, _ = _run(cfg, project)
    assert out.status == "error" and "usage limit" in out.error and infra_problem(out.error)


def test_device_code_login_status_and_logout(env, tmp_path):
    from artel.providers import CodexLogin, codex_status, logout_codex, overview, save_routing

    cfg, project, tmp = env
    assert codex_status(cfg)["logged_in"] is False
    login = CodexLogin(cfg)
    st = login.start(wait_s=5)
    assert st["phase"] == "waiting" and st["code"] == "ABCD-EFG12" and st["url"] == "https://auth.openai.com/codex/device"
    (Path(os.environ["CODEX_HOME"])).mkdir(parents=True, exist_ok=True)
    (Path(os.environ["CODEX_HOME"]) / "confirm").write_text("1", encoding="utf-8")  # the user confirms in the browser
    for _ in range(100):
        if login.state["phase"] == "done":
            break
        time.sleep(0.05)
    assert login.state["phase"] == "done"
    s = codex_status(cfg)
    assert s["logged_in"] and s["method"] == "ChatGPT"
    assert logout_codex(cfg)["ok"] and not codex_status(cfg)["logged_in"]
    # routing is saved to ARTEL_HOME/artel.yaml and applied live; key values are never exposed
    live = dict(cfg)
    r = save_routing(live, {"runtime_by_role": {"worker": "codex", "reviewer": "claude_code", "bogus": "codex"},
                            "models": {"codex": "gpt-fake-sol"}}, path=tmp_path / "artel.yaml")
    assert r["runtime_by_role"] == {"worker": "codex", "reviewer": "claude_code"} and live["codex"]["model"] == "gpt-fake-sol"
    o = overview(live)
    assert all(set(k) == {"env", "label", "set"} for k in o["keys"])


def test_codex_runtime_is_a_plugin(env):
    cfg, _, _ = env
    assert new_kernel(cfg).get("runtime:codex")


def test_codex_policy_stops_disallowed_commands(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_CODEX_CMD", "cat .env")
    t0 = time.time()
    out, ctx = _run(cfg, project)
    assert time.time() - t0 < 4 and ctx.terminal[0] == "task_block" and ctx.terminal[1]["needs_human"]
    assert "cat .env" in ctx.terminal[1]["reason"]
    assert any(e["kind"] == "policy/blocked" and e["runtime"] == "codex" for e in project.log.read())
    monkeypatch.setenv("FAKE_CODEX_CMD", "rm -rf /")
    monkeypatch.setenv("FAKE_CODEX_LAZY", "1")
    out, ctx = _run(cfg, project)
    calls = [json.loads(x) for x in (tmp / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ctx.terminal is None and calls[-1][-3:] == ["resume", "th-1", "-"]


def _reset_sandbox():
    from artel.runtimes import codex

    codex._sandbox_mode.clear()


def test_sandbox_probe_picks_the_mode_that_runs_commands(env, monkeypatch):
    from artel.runtimes.codex import codex_settings, sandbox_mode

    cfg, _, _ = env
    cc = codex_settings(cfg)
    for fake, network, want in (("", True, "sandbox+network"), ("nonet", False, "sandbox+network"),
                                ("broken", True, "none")):
        _reset_sandbox()
        monkeypatch.setenv("FAKE_CODEX_SANDBOX", fake)
        assert sandbox_mode({**cc, "network": network}, config=cfg) == want, fake
    _reset_sandbox()
    assert sandbox_mode({**cc, "sandbox_fallback": False}, config=cfg) == "sandbox"  # no fallback: keep it, fail loudly later


def test_bots_never_get_the_owners_apps_or_screen_and_have_network(env, tmp_path):
    from artel.runtimes import codex

    cc = {**codex.DEFAULTS, "binary": fake_codex(tmp_path)}
    args = codex.build_args(cc, workspace=tmp_path, shared=tmp_path, role="worker", bridge_url="u", bridge_token="t",
                            effort=None, thread_id=None, last_message=tmp_path / "m", mode="sandbox+network")
    for f in ("apps", "computer_use", "browser_use", "browser_use_external"):
        assert f"features.{f}=false" in args
    assert "sandbox_workspace_write.network_access=true" in args and args[args.index("-s") + 1] == "workspace-write"
    none = codex.build_args(cc, workspace=tmp_path, shared=tmp_path, role="worker", bridge_url="u", bridge_token="t",
                            effort=None, thread_id=None, last_message=tmp_path / "m", mode="none")
    assert "--dangerously-bypass-approvals-and-sandbox" in none
    ro = codex.build_args(cc, workspace=tmp_path, shared=tmp_path, role="reviewer", bridge_url="u", bridge_token="t",
                          effort=None, thread_id=None, last_message=tmp_path / "m", mode="none")
    assert "--dangerously-bypass-approvals-and-sandbox" in ro  # judges are then guarded by a snapshot instead


def test_a_sandbox_that_fails_mid_run_is_dropped_and_the_bot_continues(env, monkeypatch):
    """The bot must not end up 'BLOCKED: commands are blocked by the sandbox' — Artel notices the failed command,
    switches the sandbox off for this machine and resumes the same thread."""
    cfg, project, tmp = env
    _reset_sandbox()
    monkeypatch.setenv("FAKE_CODEX_SANDBOX", "exec-broken")
    out, ctx = _run(cfg, project)
    assert out.status == "terminal" and out.name == "task_submit", out.error
    kinds = [e["kind"] for e in project.log.read()]
    assert "codex/sandbox" in kinds
    calls = [json.loads(ln) for ln in (tmp / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "--dangerously-bypass-approvals-and-sandbox" in calls[-1] and "resume" in calls[-1]
    _reset_sandbox()


def test_a_judge_cannot_change_the_deliverables_even_without_a_sandbox(env, monkeypatch):
    """A reviewer runs from a scratch directory; if the sandbox is unusable and it writes into the deliverables
    anyway, Artel restores them after the run and records the violation."""
    from artel.runtimes import codex

    cfg, project, tmp = env
    (project.shared / "spec.md").write_text("original\n", encoding="utf-8")
    codex._sandbox_mode.clear()
    monkeypatch.setenv("FAKE_CODEX_SANDBOX", "broken")
    monkeypatch.setenv("FAKE_CODEX_WRITE", str(project.shared / "spec.md"))
    monkeypatch.setenv("FAKE_CODEX_LAZY", "1")  # one short run, no terminal tool needed
    out, _ = _run(deep_merge(cfg, {"codex": {"max_resumes": 0}}), project, role="reviewer")
    assert (project.shared / "spec.md").read_text(encoding="utf-8") == "original\n"
    ev = [e for e in project.log.read() if e["kind"] == "agent/readonly_violation"]
    assert ev and ev[0]["files"] == ["spec.md"]
    calls = [json.loads(x) for x in (tmp / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert calls[-1][calls[-1].index("-C") + 1].endswith("reviewer-scratch")
    codex._sandbox_mode.clear()


def test_artel_tools_reach_the_bridge_even_with_a_system_http_proxy(env, monkeypatch):
    """With http_proxy set (offices, schools) the MCP proxy must still reach Artel's bridge on 127.0.0.1,
    otherwise a CLI bot cannot even submit its work."""
    cfg, project, _ = env
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    from artel.runtimes import mcp_proxy

    assert mcp_proxy._DIRECT.handlers and all(not getattr(h, "proxies", None) for h in mcp_proxy._DIRECT.handlers)
    out, _ = _run(cfg, project)
    assert out.status == "terminal" and out.name == "task_submit", out.error


def test_codex_with_a_megabyte_of_stderr_does_not_deadlock(env, monkeypatch):
    cfg, project, _ = env
    monkeypatch.setenv("FAKE_CODEX_STDERR_KB", "1024")
    cfg = deep_merge(cfg, {"codex": {"timeout_s": 30}})
    out, ctx = _run(cfg, project)
    # a blocked stderr pipe would hang the CLI until the 30 s kill: the run would end killed, not through the tool
    assert out.status == "terminal" and out.name == "task_submit", out.error


def test_codex_usage_arriving_after_the_terminal_tool_is_recorded(env, monkeypatch):
    cfg, project, _ = env
    monkeypatch.setenv("FAKE_CODEX_USAGE_DELAY", "1")
    out, ctx = _run(cfg, project)
    assert out.status == "terminal"
    assert out.usage["input_tokens"] == 1000 and out.usage["output_tokens"] == 200 and out.usage["cached"] == 400
    end = [e for e in project.log.read() if e["kind"] == "agent/end" and e.get("runtime") == "codex"][-1]
    assert not end.get("usage_missing")


def test_codex_killed_without_usage_is_flagged_not_silently_zero(env, monkeypatch):
    from artel.runtimes import codex

    cfg, project, _ = env
    monkeypatch.setattr(codex, "TERMINAL_GRACE_S", 0.5)
    monkeypatch.setenv("FAKE_CODEX_NO_USAGE", "1")
    out, ctx = _run(cfg, project)
    assert out.status == "terminal" and out.usage["input_tokens"] == 0
    end = [e for e in project.log.read() if e["kind"] == "agent/end" and e.get("runtime") == "codex"][-1]
    assert end["usage_missing"] is True and end["usage"]["input_tokens"] == 0
