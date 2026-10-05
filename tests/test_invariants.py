"""Architecture invariants — the pillars Artel is built on (docs/ARCHITECTURE.md).

Evolution candidates must pass these. A change that removes or weakens a pillar fails here and is
never merged, however good its benchmark looks. Change a pillar only by a deliberate design decision
recorded in docs/ARCHITECTURE.md, never as a side effect.
"""

import time
import types

import pytest

from artel.kernel import Kernel, PluginError, ToolSpec, build_kernel
from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.skills import SkillError, SkillStore
from artel.tooling import ToolContext


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


def _mod(name, setup, manifest=None):
    m = types.ModuleType(name)
    m.setup = setup
    if manifest:
        m.PLUGIN = manifest
    return m


# --- Pillar: modularity (DeepSeek Harness) ------------------------------------------------------

def test_modularity_every_capability_is_a_plugin(cfg):
    k = build_kernel(cfg)
    for p in ("artel.plugins.llm_providers", "artel.plugins.runtimes", "artel.plugins.builtin_tools",
              "artel.plugins.skill_tools", "artel.plugins.board_tools"):
        assert p in k.records, p
    assert k.get("runtime:native") and k.get("runtime:claude_code")
    assert {"bash", "task_submit", "skill_manage", "review_verdict"} <= set(k.tools)


def test_modularity_unload_reverses_and_restores_overrides():
    k = Kernel({})
    k.load_plugin("base", _mod("base", lambda kk: (kk.provide("svc", "v1"),
                                                     kk.register_tool(ToolSpec("t", "d", {}, lambda c: "a")))))

    closed = []

    def override(kk):
        kk.provide("svc", "v2")
        kk.register_tool(ToolSpec("t", "d", {}, lambda c: "b"))
        kk.on("tool/execute", lambda c, n: n())
        return lambda: closed.append(True)

    k.load_plugin("over", _mod("over", override))
    assert k.get("svc") == "v2" and k.tools["t"].fn(None) == "b" and k.hooks["tool/execute"]
    assert k.provider_of("svc") == "over"
    k.unload_plugin("over")
    assert k.get("svc") == "v1" and k.tools["t"].fn(None) == "a" and not k.hooks["tool/execute"]
    assert closed == [True]  # teardown returned by setup ran
    k.unload_plugin("base")
    assert k.get("svc") is None and "t" not in k.tools


def test_modularity_failed_setup_rolls_back_and_requires_are_checked():
    k = Kernel({})

    def broken(kk):
        kk.provide("half", 1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        k.load_plugin("broken", _mod("broken", broken))
    assert k.get("half") is None and "broken" not in k.records
    with pytest.raises(PluginError):
        k.load_plugin("needy", _mod("needy", lambda kk: None, {"requires": ["service:missing"]}))


def test_modularity_runtime_is_swappable_by_plugin(cfg):
    from artel.runtimes import run_agent

    k = build_kernel(cfg)
    k.load_plugin("fake_rt", _mod("fake_rt", lambda kk: kk.register_runtime("native", lambda *a, **kw: "swapped")))
    assert run_agent(k, None, role="worker") == "swapped"
    k.unload_plugin("fake_rt")
    assert k.get("runtime:native").__name__ == "_native"


def test_modularity_hot_path_overhead_is_negligible():
    k = Kernel({})
    k.provide("x", 1)
    t0 = time.perf_counter()
    for _ in range(100_000):
        k.get("x")
        k.waterfall("tool/execute", {}, lambda c: 1)
    per_call_us = (time.perf_counter() - t0) / 100_000 * 1e6
    assert per_call_us < 5, f"kernel dispatch costs {per_call_us:.2f} µs per call"


# --- Pillar: skills (Hermes Agent) ---------------------------------------------------------------

def test_skills_learning_loop_and_guards(cfg, tmp_path):
    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1}, "skills": {"review_after_tool_calls": 1}})
    project, status = run_goal("Learn from a troubled task", cfg)
    kinds = [e["kind"] for e in project.log.read()]
    assert status == "done" and "skills/reviewed" in kinds and "skill/mutated" in kinds
    store = SkillStore(tmp_path / "s")
    store.create("x-skill", "---\nname: x-skill\ndescription: d.\n---\n# X\n", actor="user")
    with pytest.raises(SkillError):  # agents cannot edit skills they do not own
        store.patch("x-skill", "# X", "# Y", actor="agent:w")


def test_skills_read_before_write_is_enforced(cfg):
    from artel.tooling import ToolError

    project, _ = run_goal("rbw", cfg)
    k = new_kernel(cfg)
    k.get("skills").create("y-skill", "---\nname: y-skill\ndescription: d.\n---\n# Y\n", actor="agent:w")
    ctx = ToolContext(k, project, role="librarian", workspace=project.shared)
    with pytest.raises(ToolError, match="read-before-write"):
        k.tools["skill_manage"].fn(ctx, action="patch", name="y-skill", old="# Y", new="# Z")
    k.tools["skill_view"].fn(ctx, name="y-skill")
    k.tools["skill_manage"].fn(ctx, action="patch", name="y-skill", old="# Y", new="# Z")
    led = k.get("skills").ledger("y-skill")
    assert [e["action"] for e in led] == ["create", "patch"]


# --- Pillar: goal loop (Prime Agent) -------------------------------------------------------------

def test_loop_continuation_and_only_reviewers_accept(cfg):
    cfg = deep_merge(cfg, {"scripted": {"idle_task": "Build the deliverable"}})
    project, status = run_goal("Loop", cfg)
    ev = project.log.read()
    assert status == "done" and any(e["kind"] == "agent/continuation" for e in ev)
    for e in ev:
        if e["kind"] == "board/task_moved" and e["to"] == "done" and e["task_id"] != project.meta["root_task"]:
            assert e["actor"] == "reviewer"
    root_done = [e for e in ev if e["kind"] == "board/task_moved" and e["task_id"] == project.meta["root_task"]
                 and e["to"] == "done"]
    assert root_done and root_done[0]["actor"] == "auditor"


def test_loop_submit_requires_evidence(cfg):
    from artel.tooling import ToolError

    project, _ = run_goal("evidence", cfg)
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared)
    with pytest.raises(ToolError, match="evidence"):
        k.tools["task_submit"].fn(ctx, summary="done", evidence="  ")


# --- Pillar: bots with souls (OpenClaw) ----------------------------------------------------------

def test_workers_have_souls_and_workspaces(cfg):
    from artel.manager import Manager

    project, _ = run_goal("Souls", cfg)
    for p in project.personas():
        files = project.worker_files(p.name)
        assert {"SOUL.md", "IDENTITY.md", "RULES.md", "MEMORY.md"} <= set(files)
        assert (project.worker_dir(p.name) / "workspace").is_dir()
        system = Manager(new_kernel(cfg), project).worker_system(p)
        assert files["SOUL.md"].strip()[:40] in system


# --- Pillar: transparency (append-only event log) ------------------------------------------------

def test_event_log_is_complete_and_ordered(cfg):
    project, _ = run_goal("Log", cfg)
    ev = project.log.read()
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
    calls = {e["call_id"] for e in ev if e["kind"] == "tool/call"}
    results = {e["call_id"] for e in ev if e["kind"] == "tool/result"}
    assert calls == results
    actors_started = {e["actor"] for e in ev if e["kind"] == "agent/start"}
    assert {e["actor"] for e in ev if e["kind"] == "llm/response"} <= actors_started


# --- Pillar: loop detector ----------------------------------------------------------------------

def test_loop_detector_tool_calls():
    from artel.agent import TOOL_LOOP_THRESHOLD, LoopDetector

    d = LoopDetector()
    verdicts = [d.observe_tool("t", {"b": 1, "a": 2}) for _ in range(TOOL_LOOP_THRESHOLD)]
    assert verdicts == [None] * (TOOL_LOOP_THRESHOLD - 1) + ["warn"]
    assert d.observe_tool("t", {"a": 2, "b": 1}) == "stop"
    d = LoopDetector()
    for _ in range(TOOL_LOOP_THRESHOLD - 1):
        d.observe_tool("t", {"a": 1})
    assert d.observe_tool("t", {"a": 2}) is None          # a different call resets the counter
    assert d.observe_tool("t", {"a": 2}) is None


def test_loop_detector_text():
    from artel.agent import LoopDetector

    d = LoopDetector()
    assert d.observe_text("Let me think about this problem step by step, carefully. " * 3) is None
    assert d.observe_text("I will now try the exact same thing again and again. " * 14) == "warn"
    assert d.observe_text("I will now try the exact same thing again and again. " * 14) == "stop"
    prose = " ".join(f"word{i} and then some other text number {i * 7919}" for i in range(200))
    assert LoopDetector().observe_text(prose) is None
    assert LoopDetector().observe_text("-" * 400) is None


class _RepeatLLM:
    def __init__(self, blocks_fn):
        self.blocks_fn, self.seen = blocks_fn, []

    def chat(self, *, messages, **kw):
        from artel.plugins.llm_providers import LLMResponse

        self.seen.append(messages[-1])
        blocks = self.blocks_fn(len(self.seen))
        return LLMResponse(blocks=blocks, raw_content=blocks, stop_reason="tool_use", usage={}, model="x")


def _loop_run(cfg, blocks_fn):
    from artel.agent import run_loop

    project, _ = run_goal("loop", cfg)
    k = new_kernel(cfg)
    llm = _RepeatLLM(blocks_fn)
    k.provide(f"llm:{cfg['provider']}", llm)
    ctx = ToolContext(k, project, role="librarian", workspace=project.shared)
    out = run_loop(k, project, role="librarian", system="s", first_user="u", ctx=ctx, max_turns=30, purpose="t",
                   tool_names=["skills_list"])
    return project, llm, out


def test_run_loop_identical_tool_calls_warn_then_stall(cfg):
    project, llm, out = _loop_run(cfg, lambda n: [{"type": "tool_use", "id": f"c{n}", "name": "skills_list",
                                                   "input": {}}])
    assert out.status == "stalled" and "loop" in out.error and out.tool_calls == 5
    assert any(b.get("type") == "text" and "repeating yourself" in b["text"] for b in llm.seen[-1]["content"])
    ev = [e for e in project.log.read() if e["kind"] == "agent/loop"]
    assert [(e["loop_kind"], e["details"]["tool"], e["stage"]) for e in ev] == [
        ("tool", "skills_list", "warn"), ("tool", "skills_list", "stop")]


def test_run_loop_repeated_text_stalls(cfg):
    text = "I keep saying the very same sentence over and over. " * 12
    project, llm, out = _loop_run(cfg, lambda n: [{"type": "text", "text": text}])
    assert out.status == "stalled" and "loop" in out.error
    assert [e["stage"] for e in project.log.read() if e["kind"] == "agent/loop"] == ["warn", "stop"]


def test_loop_detector_negative_cases():
    from artel.agent import TEXT_LOOP_THRESHOLD, TOOL_LOOP_THRESHOLD, LoopDetector

    d = LoopDetector()
    assert [d.observe_tool("t", {"a": 1}) for _ in range(TOOL_LOOP_THRESHOLD - 1)] == [None] * (TOOL_LOOP_THRESHOLD - 1)
    d = LoopDetector()
    for _ in range(TOOL_LOOP_THRESHOLD * 2):              # alternating calls are never consecutive repeats
        assert d.observe_tool("t", {"a": 1}) is None
        assert d.observe_tool("u", {"a": 1}) is None
    sentence = "Checking the configuration file for the missing key now. "
    assert LoopDetector().observe_text(sentence * (TEXT_LOOP_THRESHOLD - 3)) is None
