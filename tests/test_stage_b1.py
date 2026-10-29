"""Stage B item 1: every llm/response has stop_reason; an invalid agent event becomes contract/violation; artel log is tolerant."""
import json

import pytest

from artel.adapters.agent import claude_code
from artel.cli import main
from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext
from tests.test_codex import FAKE_CODEX_VARS, fake_codex


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


@pytest.fixture
def project(scripted):
    return run_goal("write a hello world script", scripted)[0]


@pytest.fixture
def codex_env(tmp_path, monkeypatch, scripted):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "calls.jsonl"))
    cfg = deep_merge(scripted, {"codex": {"binary": fake_codex(tmp_path)}, "agent_env": {"pass": FAKE_CODEX_VARS}})
    return cfg, run_goal("codex", cfg)[0]


def run_codex(cfg, project):
    from artel.runtimes import run_agent

    cfg = deep_merge(cfg, {"runtime_by_role": {"worker": "codex"}})
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared, task={"id": "t-1"})
    return run_agent(k, project, role="worker", system="You are a worker.", first_user="Write hello.py", ctx=ctx,
                     max_turns=40, purpose="test")


# --- item 1: stop_reason, contract validation, tolerant `artel log` ------------------------------------------------
def test_log_prints_an_old_journal_line_without_stop_reason(project, capsys):
    with project.log.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 99999, "ts": 1.0, "kind": "llm/response", "turn": 3,
                             "blocks": [{"type": "text", "text": "old line"}]}) + "\n")
        fh.write(json.dumps({"seq": 100000, "ts": 1.0, "kind": "tool/call", "tool": "x"}) + "\n")
    assert main(["log", project.slug, "--kinds", "llm/response,tool/call"]) == 0
    out = capsys.readouterr().out
    assert "old line" in out and "—" in out


def test_codex_llm_response_carries_stop_reason(codex_env):
    cfg, project = codex_env
    assert run_codex(cfg, project).status == "terminal"
    resp = [e for e in project.log.read() if e["kind"] == "llm/response" and e.get("runtime") == "codex"]
    assert resp and all(e.get("stop_reason") in ("end_turn", "tool_use") for e in resp)


def test_claude_llm_response_without_stop_reason_is_filled(codex_env):
    from artel.adapters.agent import loop

    cfg, project = codex_env
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared, task={"id": "t-1"})
    run = loop.Run(claude_code, k, project, role="worker", system="", first_user="", ctx=ctx,
                   max_turns=5, purpose="t", persona=None, stop_event=None, tool_names=None,
                   cc=claude_code.settings(cfg), actor="a")
    run._response([claude_code.AgentEvent("text", {"text": "hi"})], {"stop_reason": "", "usage": {}})
    last = project.log.read()[-1]
    assert last["kind"] == "llm/response" and last["stop_reason"] == "end_turn"


def test_an_invalid_event_becomes_a_contract_violation_and_the_run_continues(project):
    from artel.agent import journal_agent_event

    before = project.log.seq
    assert journal_agent_event(project.log, "llm/response", actor="w1", turn=1, blocks=[]) is None
    ev = project.log.read(after=before)
    assert [e["kind"] for e in ev] == ["contract/violation"]
    v = ev[0]
    assert v["event_kind"] == "llm/response" and v["missing"] == ["stop_reason"] and v["actor"] == "w1"
    ok = journal_agent_event(project.log, "llm/response", actor="w1", turn=1, stop_reason="end_turn", blocks=[])
    assert ok and ok["kind"] == "llm/response"
