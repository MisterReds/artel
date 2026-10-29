"""Stage B item 2: one usage shape {in, out, cached} from every runtime, and a missing usage that is counted, not zero."""
import json
import sys

import pytest

from artel import acceptance
from artel.adapters.agent import claude_code, codex
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


# --- item 2: one usage shape ---------------------------------------------------------------------------------------
def shape(events):
    return [(e.kind, dict(e.data)) for e in events]


def test_claude_usage_in_is_uncached_input_and_cached_is_cache_reads():
    res = json.dumps({"type": "result", "session_id": "s", "is_error": False, "result": "d", "total_cost_usd": 0.5,
                      "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                "cache_read_input_tokens": 20, "output_tokens": 7}})
    usage = [d for k, d in shape(claude_code.parse(res)) if k == "usage"][0]
    assert (usage["input_tokens"], usage["output_tokens"], usage["cached"]) == (15, 7, 20)


def test_codex_usage_in_excludes_the_cached_part():
    line = json.dumps({"type": "turn.completed",
                       "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}})
    usage = shape(codex.parse(line))[0][1]
    assert (usage["input_tokens"], usage["output_tokens"], usage["cached"]) == (60, 20, 40)


def test_codex_run_reports_in_out_cached_through_the_port(codex_env):
    cfg, project = codex_env
    out = run_codex(cfg, project)
    assert (out.usage["input_tokens"], out.usage["output_tokens"], out.usage["cached"]) == (600, 200, 400)
    assert project.meta["usage"]["cached"] == 400
    end = [e for e in project.log.read() if e["kind"] == "agent/end" and e.get("runtime") == "codex"][-1]
    assert end["usage"]["cached"] == 400


def test_anthropic_usage_in_is_uncached_input_cached_is_cache_reads():
    from artel.plugins.llm_providers import anthropic_usage

    class U:
        input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 10, 5, 30, 4

    assert anthropic_usage(U()) == {"input_tokens": 14, "output_tokens": 5, "cached": 30}

    class Bare:
        input_tokens, output_tokens = None, 2

    assert anthropic_usage(Bare()) == {"input_tokens": 0, "output_tokens": 2, "cached": 0}


def test_openai_compat_usage_in_excludes_cached_tokens():
    from artel.plugins.llm_providers import openai_usage

    u = {"prompt_tokens": 100, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 60}}
    assert openai_usage(u) == {"input_tokens": 40, "output_tokens": 9, "cached": 60}
    assert openai_usage({}) == {"input_tokens": 0, "output_tokens": 0, "cached": 0}


def test_native_run_with_cached_usage_records_it_everywhere(scripted, monkeypatch):
    from artel.plugins.scripted import ScriptedProvider

    orig = ScriptedProvider.chat

    def chat(self, **kw):
        r = orig(self, **kw)
        r.usage = dict(r.usage, cached=7)
        return r

    monkeypatch.setattr(ScriptedProvider, "chat", chat)
    project = run_goal("write a hello world script", scripted)[0]
    ends = [e for e in project.log.read() if e["kind"] == "agent/end" and not e.get("runtime")]
    assert ends and all(e["usage"]["cached"] > 0 for e in ends)
    assert project.meta["usage"]["cached"] == sum(e["usage"]["cached"] for e in ends)
    m = acceptance.measure_project(project)
    assert m["cached_tokens"] == project.meta["usage"]["cached"]


def test_native_run_with_no_usage_is_flagged(scripted, monkeypatch):
    from artel.plugins.scripted import ScriptedProvider

    orig = ScriptedProvider.chat

    def chat(self, **kw):
        r = orig(self, **kw)
        r.usage = {}
        return r

    monkeypatch.setattr(ScriptedProvider, "chat", chat)
    project = run_goal("write a hello world script", scripted)[0]
    ends = [e for e in project.log.read() if e["kind"] == "agent/end"]
    assert ends and all(e.get("usage_missing") is True for e in ends)


FAKE_CLAUDE_RUN = """#!{py}
import json, os, sys
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s1", "model": "m"}}), flush=True)
res = {{"type": "result", "session_id": "s1", "is_error": False, "result": "done"}}
if os.environ.get("FAKE_CC_USAGE"):
    res["usage"] = {{"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 20, "output_tokens": 7}}
print(json.dumps(res), flush=True)
"""


def run_claude(scripted, tmp_path, monkeypatch, usage):
    from artel.runtimes import run_agent

    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE_RUN.format(py=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    if usage:
        monkeypatch.setenv("FAKE_CC_USAGE", "1")
    cfg = deep_merge(scripted, {"claude_code": {"binary": str(fake), "max_resumes": 0},
                                "agent_env": {"pass": ["FAKE_CC_USAGE"]}, "runtime_by_role": {"worker": "claude_code"}})
    project = run_goal("claude", cfg)[0]
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared, task={"id": "t-1"})
    out = run_agent(k, project, role="worker", system="s", first_user="u", ctx=ctx, max_turns=5, purpose="test")
    end = [e for e in project.log.read() if e["kind"] == "agent/end" and e.get("runtime") == "claude_code"][-1]
    return out, end


@pytest.mark.skipif(sys.platform == "win32", reason="shebang fake")
def test_claude_run_without_usage_is_flagged_by_the_shared_loop(scripted, tmp_path, monkeypatch):
    out, end = run_claude(scripted, tmp_path, monkeypatch, usage=False)
    assert end["usage_missing"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="shebang fake")
def test_claude_run_with_usage_reports_cached_in_agent_end(scripted, tmp_path, monkeypatch):
    out, end = run_claude(scripted, tmp_path, monkeypatch, usage=True)
    assert not end.get("usage_missing")
    assert (end["usage"]["input_tokens"], end["usage"]["output_tokens"], end["usage"]["cached"]) == (15, 7, 20)


def test_metrics_count_runs_without_usage_and_mark_tokens_incomplete():
    def ev(kind, ts, **f):
        return {"seq": 0, "ts": ts, "kind": kind, **f}

    events = [ev("agent/end", 1, runtime="codex", usage={"input_tokens": 10, "output_tokens": 5, "cached": 20}),
              ev("agent/end", 2, runtime="codex", usage={"input_tokens": 0, "output_tokens": 0}, usage_missing=True),
              ev("agent/end", 3, runtime="claude_code", usage={"input_tokens": 1, "output_tokens": 1})]
    m = acceptance.measure(events, [], {})
    assert m["usage_missing_runs"] == 1 and m["tokens_incomplete"] is True
    assert m["tokens_total"] == 10 + 5 + 20 + 2 and m["cached_tokens"] == 20
    clean = acceptance.measure(events[:1] + events[2:], [], {})
    assert clean["usage_missing_runs"] == 0 and clean["tokens_incomplete"] is False


def test_routing_record_accepts_cached(project):
    from artel.routing import record

    t0 = project.meta.get("tiers") or {}
    record(project, "cheap", {"input_tokens": 5, "output_tokens": 3, "cached": 10})
    t = project.meta["tiers"]
    assert t["cheap_tokens"] - int(t0.get("cheap_tokens") or 0) == 18
    assert t["cheap_cached"] - int(t0.get("cheap_cached") or 0) == 10


def test_project_add_usage_accepts_cached(project):
    before = project.meta["usage"].get("cached", 0)
    u = project.add_usage(1, 2, cached=9)
    assert u["cached"] == before + 9
