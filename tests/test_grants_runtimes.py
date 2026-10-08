"""Grants mapped to runtime arg lists (no binaries launched)."""
import pytest

from artel.kernel import Kernel
from artel.project import Project
from artel.runtimes.claude_code import WRITE_TOOLS, build_dir_args
from artel.runtimes.codex import build_args
from artel.tooling import effective_grants


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    ref = tmp_path / "ref"
    ref.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    k = Kernel()
    project = Project.create("goal", k.config, name="g", refs=(ref,))
    ws = project.root / "workers" / "w" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return project, ws, ref, extra


def codex(project, ws, tmp_path, role="worker", grants=None):
    cc = {"binary": "codex", "sandbox": "workspace-write"}
    a = build_args(cc, workspace=ws, shared=project.shared, role=role, bridge_url="u", bridge_token="t", effort=None,
                   thread_id=None, last_message=tmp_path / "m", grants=grants)
    return a, [a[i + 1] for i, x in enumerate(a) if x == "--add-dir"]


def test_codex_default_shared_only(env, tmp_path):
    project, ws, ref, _ = env
    a, dirs = codex(project, ws, tmp_path, grants=effective_grants(project, "worker", ws))
    assert a[a.index("-s") + 1] == "workspace-write"
    assert dirs == [str(project.shared.resolve())]


def test_codex_ref_never_writable_extra_write_added(env, tmp_path):
    project, ws, ref, extra = env
    project.set_meta(grants={"write": [str(extra), str(ref)]})
    _, dirs = codex(project, ws, tmp_path, grants=effective_grants(project, "worker", ws))
    assert str(extra.resolve()) in dirs and str(ref.resolve()) not in dirs


def test_codex_no_write_grants_is_read_only(env, tmp_path):
    project, ws, *_ = env
    g = {"read": [str(project.root)], "write": []}
    a, dirs = codex(project, ws, tmp_path, grants=g)
    # no write grant: the sandbox's only writable root is the scratch cwd; no deliverable is ever an --add-dir
    assert a[a.index("-s") + 1] == "workspace-write" and dirs == []


def test_claude_dirs_include_refs_and_extras(env):
    project, ws, ref, extra = env
    project.set_meta(grants={"read": [str(extra)]})
    args = build_dir_args(effective_grants(project, "worker", ws))
    dirs = [args[i + 1] for i, x in enumerate(args) if x == "--add-dir"]
    assert str(ref.resolve()) in dirs and str(extra.resolve()) in dirs and str(project.shared.resolve()) in dirs
    assert build_dir_args(None) == []


def test_claude_no_write_grants_disallows_write_tools(env):
    project, ws, *_ = env
    assert effective_grants(project, "reviewer", ws)["write"] == []
    assert effective_grants(project, "worker", ws)["write"]
    assert set(WRITE_TOOLS) == {"Write", "Edit", "MultiEdit"}
