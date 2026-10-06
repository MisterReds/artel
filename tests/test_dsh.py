"""DeepSeek Harness runtime, verified against a stand-in SDK (tests/fakes/dsh_sdk) and a stand-in `dsh` CLI."""
import json
import time
from pathlib import Path

import pytest

from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext

FAKES = Path(__file__).parent / "fakes"
FAKE_CLI = str(FAKES / "dsh")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(FAKES / "dsh_sdk"))
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_DSH_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    for var in ("FAKE_DSH_LIMIT", "FAKE_DSH_LAZY", "FAKE_DSH_CMD", "FAKE_DSH_BOOT_FAIL", "FAKE_DSH_PNPM_MISSING"):
        monkeypatch.delenv(var, raising=False)
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05,
                                      "dsh": {"sdk": "fake_deepseek_harness", "binary": FAKE_CLI}})
    project, _ = run_goal("dsh", cfg)
    return cfg, project, tmp_path


def _run(cfg, project, role="worker"):
    from artel.runtimes import run_agent

    cfg = deep_merge(cfg, {"runtime_by_role": {role: "dsh"}})
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role=role, workspace=project.shared, task={"id": "t-1"})
    out = run_agent(k, project, role=role, system="You are a worker.", first_user="Write hello.py", ctx=ctx,
                    max_turns=40, purpose="test")
    return out, ctx


def _calls(tmp):
    p = tmp / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_dsh_runtime_does_work_and_finishes_through_artel_tools(env):
    cfg, project, tmp = env
    t0 = time.time()
    out, ctx = _run(cfg, project)
    assert out.status == "terminal" and ctx.terminal[0] == "task_submit" and time.time() - t0 < 4
    assert (project.shared / "hello.py").exists() and "hello.py" in ctx.edited_files
    ev = project.log.read()
    kinds = {e["kind"] for e in ev if e.get("runtime") == "dsh"}
    assert {"agent/start", "llm/response", "tool/call", "tool/result", "agent/end"} <= kinds
    bash = [e for e in ev if e["kind"] == "tool/call" and e.get("tool") == "bash" and e.get("runtime") == "dsh"]
    assert bash and bash[0]["input"]["command"] == "python hello.py"
    assert any(e["kind"] == "tool/call" and e.get("tool") == "task_submit" for e in ev)  # through the MCP bridge
    assert out.usage == {"input_tokens": 1000, "output_tokens": 200}
    launch = _calls(tmp)[0]
    assert "mcp__artel__task_submit" in launch["tools"] and launch["permission_mode"] == "workspace-write"
    assert launch["opts"]["profile"] == "sdk" and launch["opts"]["cwd"] == str(project.shared)
    assert launch["opts"]["dsh_home"] == str(tmp / "home" / "dsh")
    rows = launch["rows"]
    assert {"id": "session-log-deepseek", "config": {"enabled": False}} in rows  # no session-log upload by default
    end = next(e for e in ev if e["kind"] == "agent/end" and e.get("runtime") == "dsh")
    assert end["model"] == "dsh:fake-default"
    assert not list((project.root / ".cc").glob("dsh-*.patch.yml"))  # the per-run patch layer is removed


def test_dsh_plugins_are_installed_into_the_profile_and_patches_passed_through(env, tmp_path):
    cfg, project, tmp = env
    user_patch = tmp_path / "extra.cordis.yml"
    user_patch.write_text("- id: hmr\n  disabled: true\n")
    cfg = deep_merge(cfg, {"dsh": {"plugins": ["@acme/dsh-plugin-a", "github:acme/dsh-plugin-b", str(user_patch)],
                                   "model": "some-model"}})
    out, _ = _run(cfg, project)
    assert out.status == "terminal"
    calls = _calls(tmp)
    cli = [c for c in calls if "cli" in c]
    assert cli == [{"cli": ["plugin", "--profile", "sdk", "add", "@acme/dsh-plugin-a", "github:acme/dsh-plugin-b"],
                    "home": str(tmp / "home" / "dsh")}]
    launch = next(c for c in calls if "opts" in c)
    assert launch["opts"]["patches"][0] == str(user_patch) and launch["opts"]["patches"][-1].endswith(".patch.yml")
    assert launch["opts"]["model"] == "some-model" and launch["opts"]["dsh_bin"] == FAKE_CLI
    start = next(e for e in project.log.read() if e["kind"] == "agent/start" and e.get("runtime") == "dsh")
    assert start["plugins"] == cfg["dsh"]["plugins"]
    _run(cfg, project)  # already installed: no second `dsh plugin add`
    assert len([c for c in _calls(tmp) if "cli" in c]) == 1


def test_dsh_without_terminal_tool_is_resumed_and_readonly_roles_get_readonly_sandbox(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_DSH_LAZY", "1")
    out, ctx = _run(cfg, project)
    assert out.status == "terminal"
    assert any(e["kind"] == "agent/continuation" and e.get("runtime") == "dsh" for e in project.log.read())
    responses = [e for e in project.log.read() if e["kind"] == "llm/response" and e.get("runtime") == "dsh"]
    assert [r["blocks"][0]["text"] for r in responses] == ["Writing the file.", "Finishing."]  # same durable session
    from artel.runtimes.dsh import dsh_settings, run_env

    assert run_env(dsh_settings(cfg), "reviewer")["DSH_PERMISSION_MODE"] == "read-only"
    assert run_env(dsh_settings(cfg), "worker")["DSH_PERMISSION_MODE"] == "workspace-write"
    _run(deep_merge(cfg, {"dsh": {"max_resumes": 0}}), project, role="reviewer")
    assert _calls(tmp)[-1]["permission_mode"] == "read-only"


def test_dsh_missing_install_or_key_is_a_setup_error(env, monkeypatch):
    from artel.failures import classify

    cfg, project, tmp = env
    out, _ = _run(deep_merge(cfg, {"dsh": {"sdk": "no_such_dsh_sdk_module"}}), project)
    assert out.status == "error" and "не установлен" in out.error and classify(out.error) == "setup"
    out, _ = _run(deep_merge(cfg, {"dsh": {"binary": str(tmp / "missing-dsh")}}), project)
    assert out.status == "error" and classify(out.error) == "setup"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    out, _ = _run(cfg, project)
    assert out.status == "error" and "DEEPSEEK_API_KEY" in out.error and classify(out.error) == "setup"
    assert not _calls(tmp)  # nothing was launched
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("FAKE_DSH_PNPM_MISSING", "1")
    out, _ = _run(deep_merge(cfg, {"dsh": {"plugins": ["@acme/x"]}}), project)
    assert out.status == "error" and "pnpm not found" in out.error and classify(out.error) == "setup"


def test_dsh_limit_is_a_provider_failure(env, monkeypatch):
    from artel.evolve.engine import infra_problem
    from artel.failures import classify

    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_DSH_LIMIT", "1")
    out, _ = _run(cfg, project)
    assert out.status == "error" and "RATE_LIMIT" in out.error
    assert classify(out.error) == "limit" and infra_problem(out.error)
    from artel.runtimes.dsh import _failure

    quota = _failure({"code": "QUOTA", "message": "Insufficient Balance"})
    assert classify(quota) == "limit"
    assert classify(_failure({"code": "MISSING_CREDENTIAL", "message": "no API key"})) == "setup"
    assert classify(_failure({"code": "AUTH", "message": "bad key"})) == "setup"


def test_dsh_policy_stops_disallowed_commands(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_DSH_CMD", "cat .env")
    t0 = time.time()
    out, ctx = _run(cfg, project)
    assert time.time() - t0 < 4 and ctx.terminal[0] == "task_block" and ctx.terminal[1]["needs_human"]
    assert any(e["kind"] == "policy/blocked" and e["runtime"] == "dsh" for e in project.log.read())


def test_dsh_runtime_is_a_plugin_and_auto_detection(env, monkeypatch):
    from artel import runtimes
    from artel.providers import dsh_status, save_routing

    cfg, _, tmp = env
    assert new_kernel(cfg).get("runtime:dsh")
    live = deep_merge(cfg, {"provider": "anthropic"})
    monkeypatch.setattr("artel.providers.claude_code_status", lambda c: {"logged_in": False})
    monkeypatch.setattr("artel.providers.codex_status", lambda c: {"logged_in": False})
    runtimes._auto_cache.clear()
    assert runtimes.detect_runtime(live, ttl=0) == "dsh"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    runtimes._auto_cache.clear()
    assert runtimes.detect_runtime(live, ttl=0) != "dsh"
    st = dsh_status(cfg)
    assert st["installed"] and not st["logged_in"] and st["key_env"] == "DEEPSEEK_API_KEY" and "sk-" not in json.dumps(st)
    r = save_routing(live, {"runtime_by_role": {"worker": "dsh"}, "models": {"dsh": "m-1"}}, path=tmp / "artel.yaml")
    assert r["runtime_by_role"] == {"worker": "dsh"} and live["dsh"]["model"] == "m-1"


def test_dsh_tool_entry_installs_with_pip(env, monkeypatch):
    from artel import tools

    cfg, _, tmp = env
    seen = []
    monkeypatch.setattr(tools, "_pip", lambda args, log: seen.append(args))
    monkeypatch.setattr(tools, "dsh_installed", lambda module="deepseek_harness": False)
    res = tools.install("dsh", log=lambda m: None)
    assert res["ok"] and seen[0][:2] == ["install", "--pre"] and "deepseek-harness-sdk" in seen[0]
    assert tools.installed_by_artel("dsh")
    assert tools.uninstall("dsh")["ok"] and seen[-1][:2] == ["uninstall", "-y"] and not tools.installed_by_artel("dsh")
