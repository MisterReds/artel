import copy
import dataclasses
import json

import pytest

from artel.contracts import (AmplifiedTaskContract, CapabilityProfile, ContractError, DecisionLogEntry,
                             EscalationRequest, Event, ExecutorResult, Intent, ManagerInput, ModelCard,
                             NormRequest, NormResponse, REQUIRED_FIELDS, RangeResearchTask, RewriteProposal,
                             RewriteTaskContract, TaskContract, ToolResult, Usage, ErrorClass)

USAGE = {"in": 1, "out": 2, "cached": 0}
TASK = {"id": "t1", "intent_ref": "i1", "title": "x", "input": "do", "refs": ["a"], "criteria": ["c"],
        "checks": ["k"], "output_schema": "ExecutorResult.v1", "max_tokens": 10, "capability": "executor",
        "reversible": True, "depends_on": []}
BRIEF = {"intent_ref": "i1", "decision_needed": "plan", "goal": "g", "constraints": [], "success_criteria": ["s"],
         "facts": ["f"], "refs": [], "open_questions": [], "budget": {"expensive_left": 5, "usd_left": 1.5}}
RANGE = {"discipline": "d", "question": "q", "method": "m", "raw_inputs": ["r"], "output_schema": "s",
         "critic": "independent", "max_tokens": 5}
SAMPLES = {
    Intent: {"id": "i1", "version": 1, "idea": "a", "intent": "b", "goal_behind_goal": "c", "success_criteria": ["s"],
             "constraints": [], "out_of_scope": [], "assumptions": [], "open_questions": [], "risks": [],
             "autonomy": "full", "budget": {"usd": 1, "expensive_tokens": 2, "rewrite_usd": 0, "amplify_usd": 0},
             "confirmed_by": None},
    ManagerInput: BRIEF,
    TaskContract: TASK,
    AmplifiedTaskContract: {"base": TASK, "altitude": {"goal_behind": "g", "done_means": "d"},
                            "depth": {"raw_sources": [], "methods": [], "tools": [], "independent_evidence": []},
                            "range": [{"discipline": "d", "research_task": RANGE}], "what_changed": [],
                            "why": "w", "cost_usd": 0.5},
    ExecutorResult: {"task": "t1", "status": "done", "summary": "s", "evidence": ["e"],
                     "artifacts": [{"path": "p", "sha256": "h"}], "confidence": 0.5, "blocked_on": None,
                     "usage": USAGE},
    EscalationRequest: {"task": "t1", "kind": "risk", "to": "owner", "brief": BRIEF, "options": ["o"],
                        "recommendation": "r"},
    DecisionLogEntry: {"seq": 1, "ts": "t", "actor": "a", "capability": "c", "provider": "p", "model": "m",
                       "decision": "d", "why": "w", "alternatives": [], "next": "n", "inputs": [],
                       "tokens": {"in": 1, "out": 2}, "cost_usd": 0, "latency_ms": 3, "reversible": True,
                       "status": "done", "intent_ref": "i1"},
    CapabilityProfile: {"id": "c", "reasoning": "high", "max_cost_per_mtok_usd": 1, "max_latency_s": 2,
                        "min_context_tokens": 3, "needs": [], "prefers": [], "fallback": [],
                        "independent_of": "executor"},
    RewriteProposal: {"id": "r1", "trigger": "patches", "what_is_wrong": ["w"], "why_patching_is_worse": ["x"],
                      "target": "t", "keeps": ["intent", "journal"],
                      "modules": [{"name": "m", "paradigm": "fsm", "contract": "c", "behaviour_tests": ["b"]}],
                      "parallel_groups": [["m"]], "budget": {"usd": 1, "expensive_tokens": 2}, "risks": [],
                      "rollback": "rb"},
    RewriteTaskContract: {"base": TASK, "module": "m", "paradigm": "class", "public_interface": "i",
                          "behaviour_tests": [], "known_problems": [], "must_not_read": []},
    RangeResearchTask: RANGE,
    Usage: USAGE,
    NormRequest: {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "t"}],
                  "output_schema": None, "max_tokens": 10, "cache_hint": None},
    NormResponse: {"content": "x", "tool_calls": [{"id": "1", "name": "t", "args": {}}], "usage": USAGE,
                   "latency_ms": 5, "model": "m", "stop": "end"},
    ToolResult: {"output": "o", "is_error": False, "exit_code": 0},
    ModelCard: {"id": "m", "provider": "p", "context_tokens": 10, "capabilities": ["cache"],
                "usd_per_mtok_in": 1.0, "usd_per_mtok_out": 2.0},
    ErrorClass: {"kind": "rate_limit", "retryable": True, "retry_after_s": 2.0},
}
ENUMS = {
    Intent: {"autonomy": "x"}, ManagerInput: {"decision_needed": "x"}, ExecutorResult: {"status": "x"},
    EscalationRequest: {"kind": "x", "to": "x"}, DecisionLogEntry: {"status": "x"},
    CapabilityProfile: {"reasoning": "x", "independent_of": "x"}, RewriteProposal: {"trigger": "x"},
    RewriteTaskContract: {"paradigm": "x"}, RangeResearchTask: {"critic": "x"}, NormResponse: {"stop": "x"},
    ErrorClass: {"kind": "x"},
}
OPTIONAL = {(ToolResult, "exit_code"), (ErrorClass, "retry_after_s")}


@pytest.mark.parametrize("cls", list(SAMPLES))
def test_round_trip(cls):
    x = cls.from_dict(SAMPLES[cls])
    assert cls.from_dict(x.to_dict()) == x


@pytest.mark.parametrize("cls", list(SAMPLES))
def test_missing_required_names_field(cls):
    for key in SAMPLES[cls]:
        if (cls, key) in OPTIONAL:
            continue
        d = copy.deepcopy(SAMPLES[cls])
        del d[key]
        with pytest.raises(ContractError) as e:
            cls.from_dict(d)
        assert key in str(e.value)


@pytest.mark.parametrize("cls", list(ENUMS))
def test_enum_rejects_unknown(cls):
    for key, bad in ENUMS[cls].items():
        d = copy.deepcopy(SAMPLES[cls])
        d[key] = bad
        with pytest.raises(ContractError) as e:
            cls.from_dict(d)
        assert key in str(e.value)


@pytest.mark.parametrize("cls", [NormResponse, ExecutorResult])
def test_usage_required(cls):
    d = copy.deepcopy(SAMPLES[cls])
    del d["usage"]
    with pytest.raises(ContractError, match="usage"):
        cls.from_dict(d)


def test_usage_field_checks():
    with pytest.raises(ContractError, match="^cached"):
        Usage.from_dict({"in": 1, "out": 1, "cached": -1})
    with pytest.raises(ContractError, match="usage.out"):
        NormResponse.from_dict({**SAMPLES[NormResponse], "usage": {"in": 1, "out": "x", "cached": 0}})
    assert Usage.from_dict(USAGE).in_ == 1


def test_facts_at_most_seven_and_confidence_range():
    ManagerInput.from_dict({**BRIEF, "facts": ["f"] * 7})
    with pytest.raises(ContractError, match="facts"):
        ManagerInput.from_dict({**BRIEF, "facts": ["f"] * 8})
    with pytest.raises(ContractError, match="confidence"):
        ExecutorResult.from_dict({**SAMPLES[ExecutorResult], "confidence": 1.5})


def test_nested_error_path_and_types():
    d = copy.deepcopy(SAMPLES[AmplifiedTaskContract])
    del d["range"][0]["research_task"]["method"]
    with pytest.raises(ContractError, match=r"range\[0\]\.research_task\.method"):
        AmplifiedTaskContract.from_dict(d)
    with pytest.raises(ContractError, match="max_tokens"):
        TaskContract.from_dict({**TASK, "max_tokens": True})
    with pytest.raises(ContractError):
        TaskContract.from_dict("nope")


def test_frozen():
    x = TaskContract.from_dict(TASK)
    with pytest.raises(dataclasses.FrozenInstanceError):
        x.title = "other"


def test_event_envelope_and_required_fields():
    ev = Event.from_dict({"seq": 3, "ts": 1.5, "kind": "llm/response", "turn": 1, "stop_reason": "end",
                          "blocks": []})
    assert ev.data["turn"] == 1 and Event.from_dict(ev.to_dict()) == ev
    with pytest.raises(ContractError, match="stop_reason"):
        Event.from_dict({"seq": 3, "ts": 1.5, "kind": "llm/response", "turn": 1, "blocks": []})
    assert Event.from_dict({"seq": 1, "ts": 0, "kind": "something/new"}).data == {}
    with pytest.raises(ContractError, match="kind"):
        Event.from_dict({"seq": 1, "ts": 0})


# One realistic payload per kind; field types match the writers in artel/ (agent.py, runtimes/, manager.py, board.py).
FIXTURES = {
    "llm/response": {"actor": "w1", "role": "executor", "task_id": "t1", "turn": 2, "model": "m",
                     "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4},
                     "blocks": [{"type": "text", "text": "hi"}]},
    "llm/error": {"actor": "w1", "task_id": "t1", "error": "boom"},
    "tool/call": {"actor": "w1", "task_id": "t1", "tool": "shell", "input": {"command": "ls"}, "call_id": "c1"},
    "tool/result": {"actor": "w1", "task_id": "t1", "tool": "shell", "call_id": "c1", "is_error": False,
                    "output": "ok"},
    "agent/start": {"actor": "w1", "role": "executor", "purpose": "work", "task_id": "t1", "model": "m",
                    "effort": "default", "system": "s", "user": "u", "tools": ["shell"]},
    "agent/end": {"actor": "w1", "role": "executor", "purpose": "work", "task_id": "t1", "status": "done",
                  "terminal": "finish", "turns": 3, "tool_calls": 2,
                  "usage": {"input_tokens": 3, "output_tokens": 4}, "error": None},
    "tier/usage": {"role": "manager", "tier": "expensive", "usage": {"input_tokens": 1, "output_tokens": 2},
                   "degraded": False, "simple_request": False},
    "review/verdict": {"task_id": "t1", "passed": True, "feedback": "fine",
                       "gates": [{"command": "pytest", "exit": 0}]},
    "audit/verdict": {"round": 1, "complete": False, "summary": "gaps", "gaps": ["x"]},
    "gate/result": {"task_id": "t1", "command": "pytest", "exit": 0, "output": "ok"},
    "board/task_created": {"task_id": "t1", "title": "T", "task_kind": "task", "assignee": None,
                           "depends_on": [], "criteria": ["c"], "checks": [], "created_by": "owner"},
    "board/task_moved": {"task_id": "t1", "frm": "ready", "to": "running", "actor": "w1"},
    "board/comment": {"task_id": "t1", "author": "auditor", "body": "text"},
    "run/started": {"task_id": "t1", "worker": "w1", "run_id": 7},
    "run/finished": {"run_id": 7, "status": "done", "summary": "ok", "turns": 3},
}


def test_every_kind_has_a_fixture():
    assert set(FIXTURES) == set(REQUIRED_FIELDS)


@pytest.mark.parametrize("kind", sorted(REQUIRED_FIELDS))
def test_every_event_kind_requires_each_field(kind):
    full = {"seq": 1, "ts": 0.0, "kind": kind, **FIXTURES[kind]}
    ev = Event.from_dict(full)
    assert Event.from_dict(ev.to_dict()) == ev
    json.dumps(ev.to_dict())
    for f in REQUIRED_FIELDS[kind]:
        broken = {k: v for k, v in full.items() if k != f}
        with pytest.raises(ContractError, match=f):
            Event.from_dict(broken)


def test_deep_immutability():
    ev = Event.from_dict({"seq": 1, "ts": 0.0, "kind": "tool/call", "tool": "t", "call_id": "c",
                          "input": {"a": [1, {"b": 2}]}})
    with pytest.raises(TypeError):
        ev.data["input"]["a"] = 1
    with pytest.raises(TypeError):
        ev.data["input"]["a"][1]["b"] = 3
    assert isinstance(ev.data["input"]["a"], tuple)
    d = ev.to_dict()
    d["input"]["a"].append(9)
    assert ev.data["input"]["a"] == (1, {"b": 2}) and type(d["input"]) is dict
    json.dumps(d)
    src = copy.deepcopy(SAMPLES[NormRequest])
    src["tools"] = [{"name": "t", "schema": {"p": ["x"]}}]
    src["output_schema"] = {"type": {"k": [1]}}
    req = NormRequest.from_dict(src)
    src["tools"][0]["schema"]["p"].append("y")
    assert req.tools[0]["schema"]["p"] == ("x",)
    with pytest.raises(TypeError):
        req.messages[0]["role"] = "x"
    with pytest.raises(TypeError):
        req.tools[0]["schema"]["q"] = 1
    with pytest.raises(TypeError):
        req.output_schema["type"] = 2
    assert NormRequest.from_dict(req.to_dict()) == req
    json.dumps(req.to_dict())
    resp = NormResponse.from_dict({**SAMPLES[NormResponse], "tool_calls": [{"id": "1", "name": "t",
                                                                          "args": {"x": [1]}}]})
    with pytest.raises(TypeError):
        resp.tool_calls[0]["args"]["x"] = 2
    json.dumps(resp.to_dict())
    card = ModelCard("m", "p", 1, ["a"], 0.0, 0.0)
    assert card.capabilities == ("a",)
    direct = Event(1, 0.0, "x/y", {"k": [1]})
    with pytest.raises(TypeError):
        direct.data["k"] = 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_number_rejects_non_finite(bad):
    with pytest.raises(ContractError, match="confidence"):
        ExecutorResult.from_dict({**SAMPLES[ExecutorResult], "confidence": bad})
    with pytest.raises(ContractError, match="cost_usd"):
        AmplifiedTaskContract.from_dict({**SAMPLES[AmplifiedTaskContract], "cost_usd": bad})
    with pytest.raises(ContractError, match="latency"):
        CapabilityProfile.from_dict({**SAMPLES[CapabilityProfile], "max_latency_s": bad})
    with pytest.raises(ContractError, match="ts"):
        Event.from_dict({"seq": 1, "ts": bad, "kind": "a/b"})
    with pytest.raises(ContractError, match="retry_after_s"):
        ErrorClass.from_dict({"kind": "network", "retryable": True, "retry_after_s": bad})


def test_item_types():
    resp = SAMPLES[NormResponse]
    for call in ({"id": 1, "name": "t", "args": {}}, {"id": "1", "name": None, "args": {}},
                 {"id": "1", "name": "t", "args": []}, {"id": "1", "name": "t"}):
        with pytest.raises(ContractError, match=r"tool_calls\[0\]"):
            NormResponse.from_dict({**resp, "tool_calls": [call]})
    req = SAMPLES[NormRequest]
    for msg in ({"role": 1, "content": "x"}, {"role": "user", "content": 5}, {"role": "user"},
                {"content": "x"}):
        with pytest.raises(ContractError, match=r"messages\[0\]"):
            NormRequest.from_dict({**req, "messages": [msg]})
    NormRequest.from_dict({**req, "messages": [{"role": "user", "content": [{"type": "text"}]}]})
    for tool in ({"name": 3}, {}):
        with pytest.raises(ContractError, match=r"tools\[0\]\.name"):
            NormRequest.from_dict({**req, "tools": [tool]})


def test_wrong_container_types():
    with pytest.raises(ContractError, match="refs"):
        TaskContract.from_dict({**TASK, "refs": "abc"})
    with pytest.raises(ContractError, match="title"):
        TaskContract.from_dict({**TASK, "title": ["x"]})
    with pytest.raises(ContractError, match="usage"):
        ExecutorResult.from_dict({**SAMPLES[ExecutorResult], "usage": [1]})


def test_missing_nested_required_key():
    d = copy.deepcopy(SAMPLES[EscalationRequest])
    del d["brief"]["budget"]["usd_left"]
    with pytest.raises(ContractError, match=r"brief\.budget\.usd_left"):
        EscalationRequest.from_dict(d)


@pytest.mark.parametrize("cls", [c for c in SAMPLES])
def test_unknown_key_rejected(cls):
    with pytest.raises(ContractError, match="surprise"):
        cls.from_dict({**SAMPLES[cls], "surprise": 1})


def test_unknown_key_nested_but_event_open():
    d = copy.deepcopy(SAMPLES[ExecutorResult])
    d["usage"]["extra"] = 1
    with pytest.raises(ContractError, match=r"usage\.extra"):
        ExecutorResult.from_dict(d)
    assert Event.from_dict({"seq": 1, "ts": 0, "kind": "a/b", "anything": 1}).data["anything"] == 1


def test_blocked_on_iff_blocked():
    base = SAMPLES[ExecutorResult]
    ExecutorResult.from_dict({**base, "status": "blocked", "blocked_on": "t2"})
    with pytest.raises(ContractError, match="blocked_on"):
        ExecutorResult.from_dict({**base, "status": "blocked", "blocked_on": None})
    with pytest.raises(ContractError, match="blocked_on"):
        ExecutorResult.from_dict({**base, "status": "done", "blocked_on": "t2"})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1, 2}, b"x", {(1, 2): 3}])
def test_free_form_payloads_hold_only_finite_json_values(bad):
    with pytest.raises(ContractError):
        Event.from_dict({"seq": 1, "ts": 1.0, "kind": "x/y", "v": {"deep": [bad]}})
    with pytest.raises(ContractError):
        NormResponse.from_dict({"content": "", "usage": {"in": 0, "out": 0, "cached": 0}, "latency_ms": 1,
                                "model": "m", "stop": "end", "tool_calls": [{"id": "1", "name": "t", "args": {"a": bad}}]})
