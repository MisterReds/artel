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


def test_deep_copy_on_store_and_read():
    log = ListEventLog()
    payload = {"k": [1, {"z": 2}]}
    ev = Event(0, 0.0, "x/y", payload)
    log.append(ev)
    payload["k"].append(99)
    stored = next(log.read())
    assert stored.data["k"] == (1, {"z": 2})
    with pytest.raises(TypeError):
        stored.data["k"][1]["z"] = 5
    assert next(log.read()).data["k"] == (1, {"z": 2})
    mem = DictMemory()
    delta = {"a": [1, 2]}
    mem.apply_delta(delta)
    delta["a"].append(3)
    assert mem.summary() == "a: [1, 2]"


def test_tool_runner_handler_exception_is_error_result():
    def boom(a):
        raise RuntimeError("bad")

    t = RecordingToolRunner({"boom": boom})
    r = t.run("boom", {}, ["boom"])
    assert r.is_error and "bad" in r.output
    args = {"l": [1]}
    t.run("boom", args, ["boom"])
    args["l"].append(2)
    assert t.calls[-1][1] == {"l": [1]}


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), float("-inf"), "1", None])
def test_clock_rejects_bad_sleep(bad):
    c = FakeClock()
    with pytest.raises(ValueError):
        c.sleep(bad)
    assert c.now() == 0.0


STDLIB_DENY = {"os", "sys", "socket", "ssl", "urllib", "http", "subprocess", "pathlib", "shutil", "tempfile", "io",
               "glob", "multiprocessing", "threading", "asyncio"}
VENDORS = ("anthropic", "openai", "claude", "codex", "gpt", "gemini", "deepseek", "sonnet", "opus", "haiku", "llama",
           "mistral", "qwen")
BANNED_NAMES = {"__import__", "importlib"}


def scan_source(source, package, allowed):
    """Return the violations in one module's source. `package` is the dotted package the module lives in."""
    import sys
    ok_stdlib = set(sys.stdlib_module_names) - STDLIB_DENY - BANNED_NAMES
    bad = []

    def check(name, line):
        top = name.split(".")[0]
        if top in ok_stdlib or any(name == a or name.startswith(a + ".") for a in allowed):
            return
        bad.append(f"line {line}: import of {name}")

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for a in node.names:
                check(a.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                if node.level - 1 > len(parts) - 1:
                    bad.append(f"line {node.lineno}: relative import escapes the top package")
                    continue
                base = parts[:len(parts) - (node.level - 1)]
                base = ".".join(base + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if node.module is None and node.level:  # `from . import x` imports submodules named x
                for a in node.names:
                    check(f"{base}.{a.name}", node.lineno)
            else:
                check(base, node.lineno)
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            bad.append(f"line {node.lineno}: dynamic import via {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            bad.append(f"line {node.lineno}: dynamic import via {node.attr}")
    return bad


PACKAGES = [(artel.contracts, "artel.contracts", {"artel.contracts"}),
            (artel.ports, "artel.ports", {"artel.contracts", "artel.ports"})]


def test_import_boundaries():
    for pkg, name, allowed in PACKAGES:
        files = sorted(pathlib.Path(pkg.__file__).parent.rglob("*.py"))
        assert files
        for path in files:
            rel = path.relative_to(pathlib.Path(pkg.__file__).parent)
            module_pkg = ".".join([name, *rel.parts[:-1]])
            assert scan_source(path.read_text(), module_pkg, allowed) == [], path


def test_scanner_catches_violations():
    allowed = {"artel.contracts"}
    assert scan_source("from ..manager import x", "artel.contracts", allowed)
    assert scan_source("from .. import manager", "artel.contracts", allowed)
    assert scan_source("from ..ports import x", "artel.contracts", allowed)
    assert not scan_source("from .core import x\nfrom . import _base\nimport json", "artel.contracts", allowed)
    for src in ("import os", "from os import path", "import subprocess", "import importlib",
                "x = __import__('os')", "import importlib.util", "from pathlib import Path", "import artel.kernel",
                "from artel import board"):
        assert scan_source(src, "artel.contracts", allowed), src


def test_no_vendor_names_in_packages():
    for pkg, _name, _allowed in PACKAGES:
        for path in pathlib.Path(pkg.__file__).parent.rglob("*.py"):
            text = path.read_text().lower()
            for word in VENDORS:
                assert word not in text, (path, word)


def test_fake_clock_and_tool_runner_reject_bad_values():
    with pytest.raises(ValueError):
        FakeClock(float("nan"))
    with pytest.raises(ValueError):
        FakeClock().sleep(10 ** 400)
    r = RecordingToolRunner({"t": lambda a: 5})
    assert r.run("t", {}, ["t"]).is_error
