"""Oversized tool output spills to a file under the workspace; the model gets a head/tail preview and the path."""

from types import SimpleNamespace

from artel.plugins.builtin_tools import MAX_BYTES, MAX_LINES, SPILL_DIR, bash, read_file, spill_output
from artel.tooling import ToolContext


def _spilled(ws):
    d = ws / SPILL_DIR
    return sorted(d.iterdir()) if d.exists() else []


def _lines(n):
    return "\n".join(f"line {i}" for i in range(n))


def test_small_output_unchanged(tmp_path):
    ctx = SimpleNamespace(workspace=tmp_path)
    assert spill_output(ctx, "a\nb\nc") == "a\nb\nc"
    exact = _lines(MAX_LINES)
    assert len(exact.encode()) < MAX_BYTES
    assert spill_output(ctx, exact) == exact
    assert _spilled(tmp_path) == []


def test_line_threshold_spills_with_head_tail_and_path(tmp_path):
    ctx = SimpleNamespace(workspace=tmp_path)
    text = _lines(MAX_LINES + 1)
    out = spill_output(ctx, text)
    files = _spilled(tmp_path)
    assert len(files) == 1 and files[0].read_text() == text
    assert "line 0\n" in out and f"line {MAX_LINES}" in out
    assert f"line {MAX_LINES // 2}\n" not in out
    assert str(files[0].relative_to(tmp_path)) in out
    assert len(out) < 10_000


def test_byte_threshold_spills(tmp_path):
    ctx = SimpleNamespace(workspace=tmp_path)
    text = "\n".join("x" * 100 for _ in range(MAX_BYTES // 100 + 10))
    assert len(text.split("\n")) < MAX_LINES and len(text.encode()) > MAX_BYTES
    out = spill_output(ctx, text)
    files = _spilled(tmp_path)
    assert len(files) == 1 and files[0].read_text() == text
    assert len(out) < len(text) and files[0].name in out
    one_line = "y" * (MAX_BYTES + 1)
    assert spill_output(ctx, one_line) != one_line
    assert len(_spilled(tmp_path)) == 2


def test_bash_end_to_end_through_tool_context(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    project = SimpleNamespace(shared=tmp_path / "shared", root=tmp_path)
    kernel = SimpleNamespace(config={})
    ctx = ToolContext(kernel, project, role="worker", workspace=ws)
    res = bash(ctx, f"seq 1 {MAX_LINES + 100}")
    assert res.startswith("[exit 0]")
    files = _spilled(ws)
    assert len(files) == 1
    assert files[0].read_text().split() == [str(i) for i in range(1, MAX_LINES + 101)]
    assert str(files[0].relative_to(ws)) in res
    assert "\n1\n" in res and str(MAX_LINES + 100) in res
    small = bash(ctx, "echo hi")
    assert small == "[exit 0]\nhi\n"
    assert len(_spilled(ws)) == 1


def test_read_file_spills_large_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text(_lines(MAX_LINES + 50))
    project = SimpleNamespace(shared=tmp_path / "shared", root=tmp_path)
    ctx = ToolContext(SimpleNamespace(config={}), project, role="worker", workspace=ws)
    out = read_file(ctx, "big.txt")
    assert len(_spilled(ws)) == 1 and "line 0" in out and f"line {MAX_LINES + 49}" in out
