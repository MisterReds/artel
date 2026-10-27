"""Codecs parse recorded CLI stream lines into AgentEvent lists (lines as tests/fakes/codex and Claude Code emit them)."""
import json

import pytest

from artel.adapters.agent import claude_code, codex
from artel.contracts import AgentEvent, ErrorClass


def L(**o):
    return json.dumps(o)


def shape(events):
    return [(e.kind, {k: v for k, v in e.to_dict()["data"].items()}) for e in events]


def test_codex_thread_and_usage():
    assert shape(codex.parse(L(type="thread.started", thread_id="th-1"))) == [("session", {"id": "th-1"})]
    u = L(type="turn.completed", usage={"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200})
    assert shape(codex.parse(u)) == [("usage", {"input_tokens": 1000, "output_tokens": 200, "cached": 400,
                                                "cost_usd": 0.0})]
    assert codex.parse(L(type="turn.started")) == []


def test_codex_items():
    msg = codex.parse(L(type="item.completed", item={"id": "i1", "type": "agent_message", "text": "Writing the file."}))
    assert shape(msg) == [("text", {"text": "Writing the file.", "response": {"n": 1, "fields": {}}})]
    patch = codex.parse(L(type="item.completed", item={"id": "i2", "type": "file_change", "status": "completed",
                                                       "changes": [{"path": "hello.py", "kind": "add"}]}))
    assert [e.kind for e in patch] == ["tool_call", "tool_result"]
    assert patch[0].data["name"] == "apply_patch" and list(patch[0].data["edited"]) == ["hello.py"]
    assert patch[1].data["output"] == "completed" and patch[1].data["is_error"] is False
    cmd = codex.parse(L(type="item.completed", item={"id": "i3", "type": "command_execution",
                                                     "command": "python hello.py", "aggregated_output": "hi\n",
                                                     "exit_code": 0, "status": "completed"}))
    assert shape(cmd) == [("tool_call", {"id": "i3", "name": "shell", "input": {"command": "python hello.py"},
                                         "turn": True, "observe": "late"}),
                          ("tool_result", {"id": "i3", "output": "hi\n", "is_error": False})]
    mcp = codex.parse(L(type="item.completed", item={"id": "i4", "type": "mcp_tool_call", "server": "artel",
                                                     "tool": "task_submit", "arguments": {}, "status": "completed"}))
    assert shape(mcp) == [("tool_call", {"id": "i4", "name": "task_submit", "input": {}, "turn": True,
                                         "bridged": True})]
    assert codex.parse(L(type="item.completed", item={"type": "reasoning"}))[0].data["text"] == ""


def test_codex_started_and_failures():
    assert shape(codex.parse(L(type="item.started", item={"id": "c1", "type": "command_execution",
                                                          "command": "rm -rf /", "status": "in_progress"}))) == [
        ("command", {"command": "rm -rf /", "phase": "start"})]
    assert codex.parse(L(type="item.started", item={"id": "m1", "type": "mcp_tool_call"}))[0].data["phase"] == "start"
    limit = codex.parse(L(type="turn.failed", error={"message": "You've hit your usage limit. Try again at 4:20 PM."}))
    assert shape(limit) == [("error", {"fatal": True, "message": "You've hit your usage limit. Try again at 4:20 PM."})]
    assert codex.parse(L(type="error", message="boom"))[0].data["message"] == "boom"
    assert shape(codex.parse(L(type="item.completed", item={"type": "error", "message": "bad"}))) == [
        ("error", {"message": "bad"})]


def test_codex_sandbox_failure_is_flagged():
    bwrap = "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
    res = codex.parse(L(type="item.completed", item={"id": "s1", "type": "command_execution", "command": "ls -la",
                                                     "aggregated_output": bwrap, "exit_code": 1, "status": "failed"}))
    assert res[1].data["is_error"] is True and res[1].data["sandbox_broken"] == bwrap


@pytest.mark.parametrize("line", ["", "not json", "[1]", "123", L(type="nope")])
def test_codecs_ignore_garbage(line):
    assert codex.parse(line) == [] and claude_code.parse(line) == []


def test_claude_init_and_assistant_response_group():
    assert shape(claude_code.parse(L(type="system", subtype="init", session_id="s-1", model="claude-x"))) == [
        ("session", {"id": "s-1", "model": "claude-x"})]
    line = L(type="assistant", message={"model": "claude-x", "stop_reason": "tool_use", "usage": {"output_tokens": 3},
                                        "content": [{"type": "thinking"}, {"type": "text", "text": "Doing it."},
                                                    {"type": "tool_use", "id": "t1", "name": "Edit",
                                                     "input": {"file_path": "a.py"}},
                                                    {"type": "tool_use", "id": "t2", "name": "mcp__artel__task_submit",
                                                     "input": {"summary": "x"}}]})
    ev = claude_code.parse(line)
    assert [e.kind for e in ev] == ["text", "tool_call", "tool_call"]
    assert ev[0].data["response"] == {"n": 3, "fields": {"model": "claude-x", "stop_reason": "tool_use",
                                                        "usage": {"output_tokens": 3}}}
    assert ev[1].data["observe"] == "first" and list(ev[1].data["edited"]) == ["a.py"]
    assert ev[2].data["bridged"] is True and ev[2].data["label"] == "task_submit"
    assert claude_code.parse(L(type="assistant", message={"content": [{"type": "thinking"}]})) == []


def test_claude_tool_results_and_result():
    user = L(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False},
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
         "is_error": True},
        {"type": "text", "text": "ignored"}]})
    assert shape(claude_code.parse(user)) == [("tool_result", {"id": "t1", "output": "ok", "is_error": False}),
                                              ("tool_result", {"id": "t2", "output": "ab", "is_error": True})]
    res = L(type="result", session_id="s-1", is_error=False, result="done", total_cost_usd=0.5,
            usage={"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 20,
                   "output_tokens": 7})
    assert shape(claude_code.parse(res)) == [
        ("session", {"id": "s-1"}), ("usage", {"input_tokens": 35, "output_tokens": 7, "cost_usd": 0.5}),
        ("end", {"is_error": False, "detail": "done"})]


def test_classify():
    for codec in (claude_code, codex):
        assert codec.classify("You've hit your usage limit") == ErrorClass("rate_limit", True, None)
        assert codec.classify("not logged in").kind == "auth"
        assert codec.classify("syntax error").kind == "unknown"


def test_events_are_immutable_and_validated():
    ev = claude_code.parse(L(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t",
                                                               "content": "x"}]}))[0]
    assert isinstance(ev, AgentEvent) and AgentEvent.from_dict(ev.to_dict()) == ev
    with pytest.raises(TypeError):
        ev.data["id"] = "other"
