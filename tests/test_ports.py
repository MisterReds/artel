import ast
import pathlib

import pytest

import artel.contracts
import artel.ports
from artel.contracts import (ExecutorResult, Event, NormRequest, NormResponse, TaskContract, Usage)
from artel.ports import (AgentPort, ClockPort, ConfigPort, EventPort, LLMPort, MemoryPort, ToolPort)
from artel.ports.fakes import (DictConfig, DictMemory, FakeClock, ListEventLog, RecordingToolRunner, ScriptedAgent,
                               ScriptedLLM)

REQ = NormRequest.from_dict({"messages": [{"role": "user", "content": "a b c"}], "tools": [], "output_schema": None,
                             "max_tokens": 5, "cache_hint": None})
RESP = NormResponse.from_dict({"content": "x", "tool_calls": [], "usage": {"in": 1, "out": 1, "cached": 0},
                               "latency_ms": 1, "model": "m", "stop": "end"})


def test_fakes_satisfy_protocols():
    for fake, port in [(ScriptedLLM([]), LLMPort), (ScriptedAgent([]), AgentPort), (DictMemory(), MemoryPort),
                       (ListEventLog(), EventPort), (RecordingToolRunner(), ToolPort), (FakeClock(), ClockPort),
                       (DictConfig({}), ConfigPort)]:
        assert isinstance(fake, port)
    assert not isinstance(object(), LLMPort)


def test_scripted_llm_in_order_and_exhaustion():
    llm = ScriptedLLM([RESP, RESP])
    assert llm.call(REQ) is RESP
    assert list(llm.stream(REQ))[-1]["response"] is RESP
    assert llm.requests == [REQ, REQ]
    with pytest.raises(RuntimeError):
        llm.call(REQ)
    assert llm.count_tokens(REQ) == 3 and llm.estimate_cost(Usage(1, 1, 0)) == 0.0


def test_scripted_agent_records():
    res = ExecutorResult.from_dict({"task": "t", "status": "done", "summary": "", "evidence": [], "artifacts": [],
                                    "confidence": 1, "blocked_on": None,
                                    "usage": {"in": 0, "out": 0, "cached": 0}})
    task = TaskContract.from_dict({"id": "t", "intent_ref": "", "title": "", "input": "", "refs": [], "criteria": [],
                                   "checks": [], "output_schema": "", "max_tokens": 0, "capability": "executor",
                                   "reversible": True, "depends_on": []})
    agent = ScriptedAgent([res])
    assert agent.run(task, "/w", ["shell"]) is res
    assert agent.runs == [(task, "/w", ("shell",))]


def test_memory_content_addressed():
    m = DictMemory()
    ref = m.put(b"abc")
    assert m.put(b"abc") == ref and m.get(ref) == b"abc"
    with pytest.raises(KeyError):
        m.get("missing")
    m.apply_delta({"a": 1, "b": 2})
    m.apply_delta({"a": None})
    assert m.summary() == "b: 2"


def test_event_log_append_only_and_ordered():
    log = ListEventLog()
    seqs = [log.append(Event(0, float(i), "x/y", {"i": i})) for i in range(3)]
    assert seqs == [1, 2, 3]
    assert [e.data["i"] for e in log.read()] == [0, 1, 2]
    assert [e.seq for e in log.read(after=1)] == [2, 3]
    assert not hasattr(log, "update") and not hasattr(log, "delete")
    with pytest.raises(Exception):
        log.append(Event(0, 0.0, "llm/response", {"turn": 1}))
    assert len(list(log.read())) == 3


def test_tool_runner_records_and_enforces_grants():
    t = RecordingToolRunner({"echo": lambda a: a["v"]})
    assert t.run("echo", {"v": "hi"}, ["echo"]).output == "hi"
    assert t.run("echo", {"v": "hi"}, []).is_error
    assert t.run("other", {}, ["other"]).is_error
    assert len(t.calls) == 3


def test_clock_advances_only_on_sleep():
    c = FakeClock(10)
    assert c.now() == 10 and c.now() == 10
    c.sleep(2.5)
    assert c.now() == 12.5
    with pytest.raises(ValueError):
        c.sleep(-1)


def test_config_read_only():
    src = {"a": {"b": [1, 2]}}
    c = DictConfig(src)
    assert c.get("a.b") == [1, 2] and c.get("a.x", "d") == "d" and c.get("q") is None
    c.get("a.b").append(3)
    src["a"]["b"].append(9)
    assert c.get("a.b") == [1, 2]
    assert not hasattr(c, "set")


def _imports(pkg):
    root = pathlib.Path(pkg.__file__).parent
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                yield path, [a.name for a in node.names], 0
            elif isinstance(node, ast.ImportFrom):
                yield path, [node.module or ""], node.level


def test_import_boundaries():
    import sys
    stdlib = set(sys.stdlib_module_names)
    for pkg, allowed in [(artel.contracts, {"artel.contracts"}), (artel.ports, {"artel.contracts", "artel.ports"})]:
        for path, names, level in _imports(pkg):
            if level:
                continue  # relative imports stay inside the package
            for name in names:
                top = name.split(".")[0]
                assert top in stdlib or any(name == a or name.startswith(a + ".") for a in allowed), (path, name)
