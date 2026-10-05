import pytest

from artel.agent import (COMPACT_PRUNE_TOKENS, COMPACT_SUMMARIZE_TOKENS, PRUNED_PLACEHOLDER, compact_messages,
                         estimate_tokens, prune_tool_results)
from artel.manager import run_goal
from artel.project import DEFAULT_CONFIG, deep_merge

GOAL = "GOAL: build the thing"


def convo(turns=30, size=4000):
    msgs = [{"role": "user", "content": GOAL}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"step {i}"},
                                                      {"type": "tool_use", "id": f"t{i}", "name": "read_file",
                                                       "input": {"path": f"f{i}"}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                  "content": f"out{i}" + "x" * size, "is_error": i % 7 == 0}]})
    return msgs


def assert_valid(msgs):
    assert msgs[0]["role"] == "user"
    assert all(a["role"] != b["role"] for a, b in zip(msgs, msgs[1:]))
    for a, b in zip(msgs, msgs[1:]):
        if a["role"] == "assistant":
            uses = [x["id"] for x in a["content"] if x["type"] == "tool_use"]
            results = [x["tool_use_id"] for x in b["content"] if x["type"] == "tool_result"]
            assert uses == results
    assert not (msgs[-1]["role"] == "assistant" and any(x["type"] == "tool_use" for x in msgs[-1]["content"]))


def results(msgs):
    return [b for m in msgs if isinstance(m["content"], list) for b in m["content"] if b["type"] == "tool_result"]


def test_prune_only_older_than_two_turns():
    msgs = convo()
    out, cleared = prune_tool_results(msgs)
    res = results(out)
    assert cleared == 28 and len(out) == len(msgs)
    assert all(r["content"] == PRUNED_PLACEHOLDER for r in res[:-2])
    assert [r["content"] for r in res[-2:]] == [r["content"] for r in results(msgs)[-2:]]
    assert [(r["tool_use_id"], r["is_error"]) for r in res] == [(r["tool_use_id"], r["is_error"]) for r in results(msgs)]
    assert out[0] == msgs[0] and [m for m in out if m["role"] == "assistant"] == [m for m in msgs if m["role"] == "assistant"]
    assert estimate_tokens(out) < estimate_tokens(msgs) // 5
    assert_valid(out)
    assert results(msgs)[0]["content"].startswith("out0")  # input not mutated


def test_prune_is_idempotent_and_noop_when_short():
    out, _ = prune_tool_results(convo())
    again, cleared = prune_tool_results(out)
    assert cleared == 0 and again == out
    short = convo(2)
    same, cleared = prune_tool_results(short)
    assert cleared == 0 and same == short


def test_below_threshold_is_not_compacted_by_defaults():
    assert estimate_tokens(convo(5, 1000)) < COMPACT_PRUNE_TOKENS < COMPACT_SUMMARIZE_TOKENS
    assert estimate_tokens(convo(100, 4000)) > COMPACT_PRUNE_TOKENS


def test_compact_collapses_head_keeps_goal_and_tail():
    msgs = convo()
    pruned, _ = prune_tool_results(msgs)
    seen = []
    out = compact_messages(pruned, lambda t: seen.append(t) or "SUMMARY-OK")
    assert out[0]["role"] == "user" and out[0]["content"][0]["text"] == GOAL
    assert "SUMMARY-OK" in out[0]["content"][1]["text"]
    assert out[1:] == msgs[-4:] or out[1:] == pruned[-4:]
    assert len(out) == 5 and out[1]["role"] == "assistant"
    assert "step 0" in seen[0] and "step 29" not in seen[0]
    assert sum("SUMMARY-OK" in str(m) for m in out) == 1
    assert_valid(out)
    assert estimate_tokens(out) < estimate_tokens(pruned)


def test_compact_fallback_and_noop():
    msgs = convo()
    for bad in (None, lambda t: "  ", lambda t: 1 / 0):
        out = compact_messages(msgs, bad)
        assert "step 0" in out[0]["content"][1]["text"] and out[0]["content"][0]["text"] == GOAL
        assert_valid(out)
    assert compact_messages(convo(2), lambda t: "s") is None


def test_compact_is_idempotent_in_shape():
    once = compact_messages(convo(), lambda t: "S1")
    twice = compact_messages(once, lambda t: "S2")
    assert twice is None  # only the kept tail remains: nothing left to fold
    assert_valid(once)
    longer = once + convo(6)[1:]
    out = compact_messages(longer, lambda t: "S3")
    assert out[0]["content"][0]["text"] == GOAL and "S1" in str(out[0]) and "S3" in str(out[0])
    assert_valid(out)


def test_run_loop_prunes_with_scripted_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05,
                                      "compact_prune_tokens": 1, "compact_summarize_tokens": 10**9})
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done"
    stages = {e.get("stage") for e in project.log.read() if e["kind"] == "agent/compaction"}
    assert "prune" in stages
