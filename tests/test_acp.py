import json
import sys
import time
from pathlib import Path

import pytest

from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext

FAKE = str(Path(__file__).parent / "fakes" / "acp_agent")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.jsonl"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05,
                                      "acp": {"presets": {"fake": {"command": [sys.executable, FAKE],
                                                                   "env": {"FAKE_ACP_MARKER": "from-preset"}}},
                                              "cancel_grace_s": 3}})
    project, _ = run_goal("acp", cfg)
    return cfg, project, tmp_path


def _run(cfg, project, role="worker", runtime="acp:fake", workspace=None, **acp):
    from artel.runtimes import run_agent

    cfg = deep_merge(cfg, {"runtime_by_role": {role: runtime}, "acp": acp})
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role=role, workspace=workspace or project.shared, task={"id": "t-1"})
    out = run_agent(k, project, role=role, system="You are a worker.", first_user="Write hello.py", ctx=ctx,
                    max_turns=40, purpose="test")
    return out, ctx


def _log(tmp):
    return [json.loads(x) for x in (tmp / "acp.jsonl").read_text().splitlines()]


def _received(tmp, method):
    return [r["in"] for r in _log(tmp) if "in" in r and r["in"].get("method") == method]


def _answers(tmp, method):
    return [r for r in _log(tmp) if r.get("answer") == method]


def _paired(events):
    calls = {e["call_id"] for e in events if e["kind"] == "tool/call"}
    results = {e["call_id"] for e in events if e["kind"] == "tool/result"}
    return calls == results


def test_acp_handshake_mcp_streaming_and_terminal_tool_through_the_bridge(env):
    cfg, project, tmp = env
    t0 = time.time()
    out, ctx = _run(cfg, project)
    assert out.status == "terminal" and ctx.terminal[0] == "task_submit" and time.time() - t0 < 10
    assert (project.shared / "hello.py").read_text() == "print('hi')\n"
    assert any(p.endswith("hello.py") for p in ctx.edited_files)
    # handshake: protocol v1, client fs capabilities, no terminal capability
    init = _received(tmp, "initialize")[0]["params"]
    assert init["protocolVersion"] == 1 and init["clientCapabilities"]["fs"] == {"readTextFile": True,
                                                                                 "writeTextFile": True}
    assert init["clientCapabilities"]["terminal"] is False and init["clientInfo"]["name"] == "artel"
    # session/new: cwd is the workspace and Artel's MCP proxy is passed as a stdio server
    new = _received(tmp, "session/new")[0]["params"]
    assert new["cwd"] == str(project.shared.resolve())
    srv = new["mcpServers"][0]
    assert srv["name"] == "artel" and srv["command"] == sys.executable and srv["args"][0].endswith("mcp_proxy.py")
    assert srv["env"][0]["name"] == "PYTHONPATH"
    mcp = next(r for r in _log(tmp) if "mcp_tools" in r)
    assert "task_submit" in mcp["mcp_tools"] and not mcp["mcp_result"]["isError"]
    prompt = _received(tmp, "session/prompt")[0]["params"]
    assert prompt["sessionId"] == "sess-1" and "<role_instructions>" in prompt["prompt"][0]["text"]
    assert _received(tmp, "session/cancel")  # Artel cancels the turn once the terminal tool is accepted
    assert _log(tmp)[0]["env_marker"] == "from-preset"
    # streaming updates mirrored into the event log
    ev = [e for e in project.log.read() if e.get("runtime") == "acp"]
    kinds = {e["kind"] for e in ev}
    assert {"agent/start", "acp/session", "llm/response", "agent/plan", "tool/call", "tool/result",
            "acp/permission", "agent/end"} <= kinds
    resp = next(e for e in ev if e["kind"] == "llm/response")
    assert resp["blocks"] == [{"type": "thinking", "thinking": "Plan: "}, {"type": "text", "text": "Writing the file."}]
    assert resp["model"] == "acp:fake:fake-acp@0.1"
    shell = next(e for e in ev if e["kind"] == "tool/call" and e["tool"] == "execute")
    assert shell["input"] == {"command": "python hello.py"}
    res = next(e for e in ev if e["kind"] == "tool/result" and e["call_id"] == shell["call_id"])
    assert res["output"].strip() == "hi" and not res["is_error"]
    assert any("$ python hello.py\nhi" in o for o in ctx.outputs)
    # the terminal tool is logged once, by the bridge; the agent's own report of it is not duplicated
    submits = [e for e in ev if e["kind"] == "tool/call" and "task_submit" in (e.get("tool", "") + e.get("title", ""))]
    assert len(submits) == 1 and submits[0]["tool"] == "task_submit"
    # a call the agent never finished still gets a result
    unfinished = next(e for e in ev if e["kind"] == "tool/result" and e["tool"] == "search")
    assert unfinished["is_error"] and _paired(project.log.read())
    assert out.usage == {"input_tokens": 1000, "output_tokens": 200} or \
        (out.usage["input_tokens"] == 1000 and out.usage["output_tokens"] == 200)
    assert out.cost_usd == pytest.approx(0.01)
    end = next(e for e in ev if e["kind"] == "agent/end")
    assert end["status"] == "terminal" and end["terminal"] == "task_submit"


def test_acp_without_terminal_tool_is_resumed_in_the_same_session(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_LAZY", "1")
    out, ctx = _run(cfg, project)
    assert out.status == "terminal"
    assert len(_received(tmp, "initialize")) == 1 and len(_received(tmp, "session/new")) == 1
    prompts = _received(tmp, "session/prompt")
    assert len(prompts) == 2 and prompts[1]["params"]["sessionId"] == "sess-1"
    assert prompts[1]["params"]["prompt"][0]["text"].startswith("[continuation]")
    ev = project.log.read()
    assert any(e["kind"] == "agent/continuation" and e.get("runtime") == "acp" for e in ev)
    assert any(e["kind"] == "acp/turn_end" and e["stop_reason"] == "end_turn" for e in ev)


def test_acp_gives_up_after_max_resumes(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_NO_SUBMIT", "1")
    out, ctx = _run(cfg, project, max_resumes=1)
    assert out.status == "stalled" and "no terminal tool" in out.error
    assert len(_received(tmp, "session/prompt")) == 2


def test_acp_permissions_follow_the_role(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_NO_SUBMIT", "1")
    monkeypatch.setenv("FAKE_ACP_FS_PROBE", json.dumps([["fs/write_text_file", str(project.shared / "x.txt")]]))
    out, ctx = _run(cfg, project, role="reviewer", max_resumes=0)
    perms = [e for e in project.log.read() if e["kind"] == "acp/permission"]
    assert [(p["tool_kind"], p["decision"]) for p in perms] == [("edit", "reject"), ("execute", "allow")]
    assert not (project.shared / "hello.py").exists() and not (project.shared / "x.txt").exists()
    answers = _answers(tmp, "session/request_permission")
    assert answers[0]["msg"]["result"]["outcome"] == {"outcome": "selected", "optionId": "no"}
    assert answers[1]["msg"]["result"]["outcome"] == {"outcome": "selected", "optionId": "yes"}
    assert "error" in _answers(tmp, "fs/write_text_file")[0]["msg"]  # read-only role: client fs writes refused


def test_acp_policy_blocks_commands(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_CMD", "cat .env")
    out, ctx = _run(cfg, project)
    assert ctx.terminal[0] == "task_block" and ctx.terminal[1]["needs_human"] and "cat .env" in ctx.terminal[1]["reason"]
    assert any(e["kind"] == "policy/blocked" and e["runtime"] == "acp" for e in project.log.read())
    (tmp / "acp.jsonl").unlink()
    monkeypatch.setenv("FAKE_ACP_CMD", "rm -rf /")
    monkeypatch.setenv("FAKE_ACP_LAZY", "1")
    out, ctx = _run(cfg, project)
    prompts = _received(tmp, "session/prompt")
    assert out.status == "terminal" and "blocked by policy" in prompts[1]["params"]["prompt"][0]["text"]


def test_acp_fs_methods_are_confined_to_allowed_paths(env, monkeypatch):
    cfg, project, tmp = env
    ws = tmp / "ws"
    ws.mkdir()
    ref = tmp / "ref"
    ref.mkdir()
    (ref / "notes.md").write_text("one\ntwo\nthree\n")
    (tmp / "secret.txt").write_text("secret")
    project.refs = [ref]
    probes = [["fs/read_text_file", str(tmp / "secret.txt")],
              ["fs/write_text_file", str(tmp / "outside.txt")],
              ["fs/write_text_file", str(ref / "notes.md")],
              ["fs/read_text_file", str(ref / "notes.md")],
              ["fs/write_text_file", str(project.shared / "deliverable.txt")],
              ["fs/write_text_file", str(ws / "sub" / "draft.txt")],
              ["fs/read_text_file", "relative.txt"],
              ["fs/read_text_file", str(ws / "../secret.txt")]]
    monkeypatch.setenv("FAKE_ACP_FS_PROBE", json.dumps(probes))
    out, ctx = _run(cfg, project, workspace=ws)
    got = [a["msg"] for a in _log(tmp) if a.get("answer") in ("fs/read_text_file", "fs/write_text_file")][:len(probes)]
    ok = ["error" not in m for m in got]
    assert ok == [False, False, False, True, True, True, False, False]
    assert got[3]["result"]["content"] == "one\ntwo\nthree\n"
    assert not (tmp / "outside.txt").exists() and (ref / "notes.md").read_text().startswith("one")
    assert (project.shared / "deliverable.txt").read_text() == "probe\n" and (ws / "sub" / "draft.txt").exists()
    assert (ws / "hello.py").exists() and out.status == "terminal"
    assert _received(tmp, "session/new")[0]["params"]["additionalDirectories"] == [str(project.shared.resolve()),
                                                                                  str(ref.resolve())]
    assert _paired(project.log.read())


def test_acp_read_text_file_honours_line_and_limit(env, tmp_path):
    from artel.runtimes.acp import AcpRun

    cfg, project, tmp = env
    (project.shared / "f.txt").write_text("a\nb\nc\nd\n")
    run = AcpRun.__new__(AcpRun)
    run.read_roots, run.ctx = [project.shared.resolve()], ToolContext(None, project, role="worker",
                                                                     workspace=project.shared)
    assert run.fs_read({"path": str(project.shared / "f.txt"), "line": 2, "limit": 2}) == {"content": "b\nc\n"}


def test_acp_loop_is_detected_and_the_turn_cancelled(env, monkeypatch):
    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_LOOP", "1")
    out, ctx = _run(cfg, project)
    assert out.status == "stalled" and out.error.startswith("loop")
    ev = project.log.read()
    assert [e["stage"] for e in ev if e["kind"] == "agent/loop"] == ["warn", "stop"]
    assert _received(tmp, "session/cancel") and _paired(ev)


def test_acp_missing_command_is_a_setup_error(env):
    from artel.failures import classify

    cfg, project, tmp = env
    t0 = time.time()
    out, _ = _run(cfg, project, runtime="acp", command=["no-such-acp-agent-xyz"])
    assert out.status == "error" and "не установлен" in out.error and classify(out.error) == "setup"
    assert time.time() - t0 < 2
    out, _ = _run(cfg, project, runtime="acp:nonexistent")
    assert out.status == "error" and "nonexistent" in out.error
    assert any(e["kind"] == "llm/error" and e["runtime"] == "acp" for e in project.log.read())


def test_acp_auth_required_is_a_setup_error(env, monkeypatch):
    from artel.failures import classify

    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_AUTH", "1")
    out, _ = _run(cfg, project)
    assert out.status == "error" and "Log in with Google" in out.error and classify(out.error) == "setup"


def test_acp_provider_limit_is_classified_as_a_limit(env, monkeypatch):
    from artel.evolve.engine import infra_problem
    from artel.failures import classify

    cfg, project, tmp = env
    monkeypatch.setenv("FAKE_ACP_LIMIT", "1")
    out, _ = _run(cfg, project)
    assert out.status == "error" and "Quota exceeded" in out.error
    assert classify(out.error) == "limit" and infra_problem(out.error)


def test_acp_runtime_presets_and_routing(env, tmp_path):
    from artel.providers import acp_status, overview, routing, save_routing
    from artel.runtimes.acp import acp_settings

    cfg, project, tmp = env
    assert new_kernel(cfg).get("runtime:acp")
    assert acp_settings(cfg)["command"] == ["gemini", "--experimental-acp"]
    assert acp_settings(cfg, "goose")["command"] == ["goose", "acp"]
    assert acp_settings(deep_merge(cfg, {"acp": {"command": ["my-agent", "--acp"]}}))["command"] == ["my-agent", "--acp"]
    st = acp_status(cfg)
    fake = next(p for p in st["presets"] if p["name"] == "fake")
    assert fake["installed"] and st["agent"] == "gemini"
    assert "acp:gemini" in routing(cfg)["runtimes"] and "acp" in overview(cfg)
    live = dict(cfg)
    r = save_routing(live, {"runtime_by_role": {"worker": "acp:fake", "reviewer": "acp:bogus"}, "acp_agent": "goose"},
                     path=tmp_path / "artel.yaml")
    assert r["runtime_by_role"] == {"worker": "acp:fake"} and live["acp"]["agent"] == "goose"
    assert live["acp"]["presets"]["fake"]  # saving the agent keeps the rest of the acp section
