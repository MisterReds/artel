"""Post-write diagnostics: write_file/edit_file append syntax errors for .py files."""

import pytest

from artel.manager import new_kernel, run_goal
from artel.plugins import builtin_tools
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("diag", cfg)
    k = new_kernel(cfg)
    return k.tools, ToolContext(k, project, role="worker", workspace=project.shared)


def test_write_syntax_error_reported(env):
    t, ctx = env
    out = t["write_file"].fn(ctx, path="bad.py", content="def f(:\n    pass\n")
    assert out.startswith("wrote ") and "Diagnostics:" in out and "SyntaxError" in out


def test_valid_and_non_py_unchanged(env):
    t, ctx = env
    assert "Diagnostics" not in t["write_file"].fn(ctx, path="ok.py", content="x = 1\n")
    assert t["write_file"].fn(ctx, path="n.txt", content="def f(:") == "wrote 7 chars to " + ctx.display(
        ctx.resolve("n.txt"))


def test_edit_reports_diagnostics(env):
    t, ctx = env
    t["write_file"].fn(ctx, path="e.py", content="x = 1\n")
    out = t["edit_file"].fn(ctx, path="e.py", old="x = 1", new="x = (")
    assert out.startswith("edited ") and "Diagnostics:" in out and "SyntaxError" in out


def test_missing_ruff_and_timeout_do_not_raise(env, monkeypatch, tmp_path):
    p = tmp_path / "t.py"
    p.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(builtin_tools.shutil, "which", lambda _n: None)
    assert builtin_tools._diagnostics(p) == ""
    monkeypatch.setattr(builtin_tools.shutil, "which", lambda _n: "/bin/sleep")
    def boom(*a, **k):
        raise builtin_tools.subprocess.TimeoutExpired("ruff", 1)
    monkeypatch.setattr(builtin_tools.subprocess, "run", boom)
    assert builtin_tools._diagnostics(p) == ""
    assert isinstance(builtin_tools._diagnostics(tmp_path / "missing.py"), str)


def test_syntax_error_reported_without_ruff(env, monkeypatch):
    t, ctx = env
    monkeypatch.setattr(builtin_tools.shutil, "which", lambda _n: None)
    out = t["write_file"].fn(ctx, path="nr.py", content="def f(:\n")
    assert "Diagnostics:" in out and "SyntaxError" in out
