"""Stale-edit guard: edit_file refuses files changed on disk since the agent last read/wrote them."""

import os

import pytest

from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext, ToolError


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("stale", cfg)
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared)
    return k.tools, ctx, project.shared


def _bump_mtime(p, seconds=5):
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 10**9))


def test_read_then_external_change_is_refused(env):
    t, ctx, root = env
    (root / "a.txt").write_text("alpha beta", encoding="utf-8")
    t["read_file"].fn(ctx, path="a.txt")
    (root / "a.txt").write_text("alpha gamma, longer", encoding="utf-8")
    _bump_mtime(root / "a.txt")
    with pytest.raises(ToolError, match="read_file"):
        t["edit_file"].fn(ctx, path="a.txt", old="alpha", new="one")
    assert (root / "a.txt").read_text(encoding="utf-8") == "alpha gamma, longer"


def test_same_size_edit_with_touched_mtime_is_refused(env):
    t, ctx, root = env
    (root / "a.txt").write_text("alpha beta", encoding="utf-8")
    t["read_file"].fn(ctx, path="a.txt")
    (root / "a.txt").write_text("alpha BETA", encoding="utf-8")        # same size
    _bump_mtime(root / "a.txt")
    with pytest.raises(ToolError):
        t["edit_file"].fn(ctx, path="a.txt", old="alpha", new="one")
    assert (root / "a.txt").read_text(encoding="utf-8") == "alpha BETA"


def test_touch_only_is_refused(env):
    t, ctx, root = env
    (root / "a.txt").write_text("alpha beta", encoding="utf-8")
    t["read_file"].fn(ctx, path="a.txt")
    _bump_mtime(root / "a.txt")
    with pytest.raises(ToolError):
        t["edit_file"].fn(ctx, path="a.txt", old="alpha", new="one")


def test_reread_then_edit_succeeds(env):
    t, ctx, root = env
    (root / "a.txt").write_text("alpha beta", encoding="utf-8")
    t["read_file"].fn(ctx, path="a.txt")
    (root / "a.txt").write_text("alpha gamma, longer", encoding="utf-8")
    _bump_mtime(root / "a.txt")
    t["read_file"].fn(ctx, path="a.txt")
    t["edit_file"].fn(ctx, path="a.txt", old="alpha", new="one")
    assert (root / "a.txt").read_text(encoding="utf-8") == "one gamma, longer"


def test_write_then_edit_succeeds(env):
    t, ctx, root = env
    t["write_file"].fn(ctx, path="w.txt", content="alpha beta")
    t["edit_file"].fn(ctx, path="w.txt", old="alpha", new="one")
    assert (root / "w.txt").read_text(encoding="utf-8") == "one beta"


def test_external_change_after_write_is_refused(env):
    t, ctx, root = env
    t["write_file"].fn(ctx, path="w.txt", content="alpha beta")
    (root / "w.txt").write_text("alpha BETA", encoding="utf-8")
    _bump_mtime(root / "w.txt")
    with pytest.raises(ToolError):
        t["edit_file"].fn(ctx, path="w.txt", old="alpha", new="one")


def test_never_read_file_edits_as_before(env):
    t, ctx, root = env
    (root / "n.txt").write_text("abc", encoding="utf-8")
    t["edit_file"].fn(ctx, path="n.txt", old="abc", new="d")
    assert (root / "n.txt").read_text(encoding="utf-8") == "d"


def test_consecutive_agent_edits_succeed(env):
    t, ctx, root = env
    t["write_file"].fn(ctx, path="c.txt", content="one two three")
    for old, new in (("one", "1"), ("two", "2"), ("three", "3")):
        t["edit_file"].fn(ctx, path="c.txt", old=old, new=new)
    assert (root / "c.txt").read_text(encoding="utf-8") == "1 2 3"
