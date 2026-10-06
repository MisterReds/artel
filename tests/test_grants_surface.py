"""Grants on the project API/page and an end-to-end run with --ref (scripted provider)."""

import pytest

from artel.agent import execute_tool
from artel.cli import main
from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.tooling import ToolContext
from artel.web.server import project_view


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


class _Runner:
    active: dict = {}


def test_project_view_has_grants(cfg, tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    project = Project.create("g", cfg, refs=(ref,))
    g = project_view(project, _Runner())["grants"]
    assert str(project.shared.resolve()) in g["write"]
    assert str(ref.resolve()) in g["read"] and g["refs"] == [str(ref.resolve())]
    assert str(ref.resolve()) not in g["write"]


def test_index_renders_grants():
    from pathlib import Path
    import artel.web
    html = (Path(artel.web.__file__).parent / "index.html").read_text()
    assert "v.grants" in html and "refs" in html


def test_e2e_run_with_ref_and_ref_write_refused(cfg, tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "a.txt").write_text("x")
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg, refs=(ref,))
    assert status == "done"
    ws = project.worker_dir("Ada") / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    kernel = new_kernel(cfg)
    ctx = ToolContext(kernel, project, role="worker", workspace=ws)
    out, err = execute_tool(kernel, ctx, "write_file", {"path": str(ref / "a.txt"), "content": "boom"})
    assert err and "write grants" in out
    assert (ref / "a.txt").read_text() == "x"
    out, err = execute_tool(kernel, ctx, "read_file", {"path": str(ref / "a.txt")})
    assert not err and "x" in out


def test_cli_contract_with_ref(cfg, tmp_path, monkeypatch):
    ref = tmp_path / "ref"
    ref.mkdir()
    wd = tmp_path / "wd"
    rc = main(["run", "Write a short guide to SQLite WAL mode", "--name", "gs", "--seed", str(ref),
               "--workdir", str(wd), "--ref", str(ref), "--provider", "scripted"])
    assert rc == 0
