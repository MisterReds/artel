"""Oversized tool output spills to a file under the workspace; a head+tail preview and the path are returned."""

import pytest

from artel.manager import new_kernel, run_goal
from artel.plugins.builtin_tools import MAX_BYTES, MAX_LINES, spill_output
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.tooling import ToolContext


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("spill", cfg)
    k = new_kernel(cfg)
    ws = project.root / "ws"
    ws.mkdir()
    return k.tools, ToolContext(k, project, role="worker", workspace=ws), ws


def test_small_output_unchanged(env):
    _, ctx, ws = env
    assert spill_output(ctx, "a\nb\nc", "bash") == "a\nb\nc"
    assert not (ws / ".spill").exists()


def test_over_lines_spills(env):
    _, ctx, ws = env
    text = "\n".join(f"line{i}" for i in range(MAX_LINES + 500))
    out = spill_output(ctx, text, "bash")
    files = list((ws / ".spill").iterdir())
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == text
    assert out.startswith("line0") and f"line{MAX_LINES + 499}" in out
    assert f".spill/{files[0].name}" in out and "read_file" in out
    assert len(out) < len(text)


def test_over_bytes_spills_single_line(env):
    _, ctx, ws = env
    text = "H" + "x" * (MAX_BYTES + 10) + "T"
    out = spill_output(ctx, text, "read_file")
    (f,) = (ws / ".spill").iterdir()
    assert f.read_text(encoding="utf-8") == text
    assert out.startswith("H") and "T\n" in out and f.name in out


def test_bash_spills_and_is_readable_via_path(env):
    t, ctx, ws = env
    out = t["bash"].fn(ctx, command="seq 1 3000")
    assert "[exit 0]" in out and "\n3000" in out and ".spill/bash-" in out
    path = out.split("saved to ")[1].split(".txt")[0] + ".txt"
    assert "3000" in t["read_file"].fn(ctx, path=path, offset=2999, limit=1)
    assert ".spill" not in t["list_files"].fn(ctx)


def test_exact_limits_not_spilled(env):
    _, ctx, ws = env
    text = "\n".join("x" for _ in range(MAX_LINES))
    assert spill_output(ctx, text, "bash") == text
    assert spill_output(ctx, "y" * MAX_BYTES, "bash") == "y" * MAX_BYTES
