import json
import os
import time
from pathlib import Path

import pytest

from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext

FAKE = str(Path(__file__).parent / "fakes" / "codex")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "calls.jsonl"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05, "codex": {"binary": FAKE}})
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
    args = json.loads((tmp / "calls.jsonl").read_text().splitlines()[0])
    assert args[:2] == ["exec", "--json"] and "workspace-write" in args and "--add-dir" in args
    assert 'approval_policy="never"' in args and args[-1] == "-"
    assert out.usage["input_tokens"] == 1000 and out.usage["output_tokens"] == 200


def test_codex_without_terminal_tool_is_resumed_and_readonly_roles_get_readonly_sandbox(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_CODEX_LAZY", "1")
    out, ctx = _run(cfg, project)
    calls = [json.loads(x) for x in (tmp / "calls.jsonl").read_text().splitlines()]
    assert out.status == "terminal" and len(calls) == 2 and calls[1][-3:] == ["resume", "th-1", "-"]
    assert any(e["kind"] == "agent/continuation" and e.get("runtime") == "codex" for e in project.log.read())
    from artel.runtimes.codex import build_args, codex_settings

    a = build_args(codex_settings(cfg), workspace=tmp, shared=tmp, role="reviewer", bridge_url="u", bridge_token="t",
                   effort="high", thread_id=None, last_message=tmp / "m")
    assert a[a.index("-s") + 1] == "read-only" and 'model_reasoning_effort="high"' in a


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
    (Path(os.environ["CODEX_HOME"]) / "confirm").write_text("1")  # the user confirms in the browser
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
                            "models": {"codex": "gpt-5-codex"}}, path=tmp_path / "artel.yaml")
    assert r["runtime_by_role"] == {"worker": "codex", "reviewer": "claude_code"} and live["codex"]["model"] == "gpt-5-codex"
    o = overview(live)
    assert all(set(k) == {"env", "label", "set"} for k in o["keys"])


def test_codex_runtime_is_a_plugin(env):
    cfg, _, _ = env
    assert new_kernel(cfg).get("runtime:codex")
