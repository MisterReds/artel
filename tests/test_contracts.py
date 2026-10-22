import copy

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
    with pytest.raises(Exception):
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


@pytest.mark.parametrize("kind", sorted(REQUIRED_FIELDS))
def test_every_event_kind_requires_each_field(kind):
    full = {"seq": 1, "ts": 0.0, "kind": kind, **{f: 1 for f in REQUIRED_FIELDS[kind]}}
    Event.from_dict(full)
    for f in REQUIRED_FIELDS[kind]:
        broken = {k: v for k, v in full.items() if k != f}
        with pytest.raises(ContractError, match=f):
            Event.from_dict(broken)
