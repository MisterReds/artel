"""edit_file fuzzy fallbacks: line-trimmed, block-anchor, and ambiguity refusal."""

import pytest

from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext, ToolError


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("fuzzy", cfg)
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared)
    return k.tools["edit_file"].fn, ctx, project.shared


def test_exact_unchanged(env):
    edit, ctx, root = env
    (root / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    edit(ctx, path="a.py", old="x = 1", new="x = 9")
    assert (root / "a.py").read_text(encoding="utf-8") == "x = 9\ny = 2\n"


def test_exact_multiple_still_errors(env):
    edit, ctx, root = env
    (root / "a.py").write_text("a\na\n", encoding="utf-8")
    with pytest.raises(ToolError, match="matched 2"):
        edit(ctx, path="a.py", old="a", new="b")


def test_line_trimmed(env):
    edit, ctx, root = env
    (root / "a.py").write_text("def f():\n    x = 1   \n    return x\n", encoding="utf-8")
    edit(ctx, path="a.py", old="x = 1\nreturn x", new="    return 5")
    assert (root / "a.py").read_text(encoding="utf-8") == "def f():\n    return 5\n"


def test_line_trimmed_ambiguous_refused(env):
    edit, ctx, root = env
    src = "  a = 1\n  b = 2\nzz\n    a = 1\n    b = 2\n"
    (root / "a.py").write_text(src, encoding="utf-8")
    with pytest.raises(ToolError, match="2 line-trimmed matches"):
        edit(ctx, path="a.py", old="a = 1\nb = 2", new="c")
    assert (root / "a.py").read_text(encoding="utf-8") == src


def test_block_anchor(env):
    edit, ctx, root = env
    (root / "a.py").write_text("def f():\n    total = compute(items)\n    log(total)\n    return total\nend\n", encoding="utf-8")
    old = "def f():\n    total = compute(item)\n    log(total)\n    return total"
    edit(ctx, path="a.py", old=old, new="def f():\n    return 0")
    assert (root / "a.py").read_text(encoding="utf-8") == "def f():\n    return 0\nend\n"


def test_block_anchor_ambiguous_refused(env):
    edit, ctx, root = env
    src = "{\n  one = 1\n}\nx\n{\n  one = 1\n}\n"
    (root / "a.py").write_text(src, encoding="utf-8")
    with pytest.raises(ToolError, match="block-anchor matches"):
        edit(ctx, path="a.py", old="{\n  one = 2\n}", new="{}")
    assert (root / "a.py").read_text(encoding="utf-8") == src


def test_dissimilar_middle_not_found(env):
    edit, ctx, root = env
    src = "{\n  completely different\n}\n"
    (root / "a.py").write_text(src, encoding="utf-8")
    with pytest.raises(ToolError, match="matched 0"):
        edit(ctx, path="a.py", old="{\n  zzzzzzzzzzzzzzzzzz\n}", new="{}")
    assert (root / "a.py").read_text(encoding="utf-8") == src


def test_no_match_at_all(env):
    edit, ctx, root = env
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="matched 0"):
        edit(ctx, path="a.py", old="nothing here", new="y")
    assert (root / "a.py").read_text(encoding="utf-8") == "x = 1\n"
