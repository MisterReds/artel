"""Grants mapped to runtime argument lists (pure functions; no CLI is spawned)."""

from pathlib import Path

import pytest

from artel.manager import new_kernel
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.runtimes import claude_code, codex
from artel.tooling import ToolContext


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    ref = tmp_path / "ref"
    ref.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    project = Project.create("g", cfg, refs=(ref,))
    project.set_meta(grants={"write": [str(extra)]})
    ws = project.worker_dir("w1") / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return new_kernel(cfg), project, ws, ref.resolve(), extra.resolve()


def _ctx(env, role):
    k, project, ws, _, _ = env
    return ToolContext(k, project, role=role, workspace=ws)


def _codex(env, role, grants):
    _, project, ws, _, _ = env
    return codex.build_args(codex.codex_settings({}), workspace=ws, shared=project.shared, role=role, bridge_url="u",
                            bridge_token="t", effort=None, thread_id=None, last_message=ws / "m", grants=grants)


def _add_dirs(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]


def test_codex_add_dir_for_write_grants_never_refs(env):
    _, project, ws, ref, extra = env
    a = _codex(env, "worker", _ctx(env, "worker").grants())
    dirs = _add_dirs(a)
    assert str(extra) in dirs and str(project.shared.resolve()) in dirs
    assert str(ref) not in dirs
    assert a[a.index("-s") + 1] == "workspace-write"
    # a ref forced into the grants by hand still must not become writable via the effective grants
    assert str(ref) not in _add_dirs(_codex(env, "worker", {"read": [str(ref)], "write": []}))


def test_codex_readonly_role_stays_readonly(env):
    a = _codex(env, "reviewer", _ctx(env, "reviewer").grants())
    assert a[a.index("-s") + 1] == "read-only" and "--add-dir" not in a


def test_codex_legacy_call_without_grants_keeps_shared(env):
    _, project, *_ = env
    assert _add_dirs(_codex(env, "worker", None)) == [str(project.shared)]


def _cc(env, role, grants):
    _, project, *_ = env
    return claude_code.build_args(claude_code.DEFAULTS, role=role, prompt="p", system="s", cfg_path=Path("c.json"),
                                  shared=project.shared, refs=project.refs, grants=grants)


def test_claude_code_adds_dirs_for_grants_including_refs(env):
    _, project, ws, ref, extra = env
    a = _cc(env, "worker", _ctx(env, "worker").grants())
    dirs = _add_dirs(a)
    assert {str(ref), str(extra), str(project.shared.resolve())} <= set(dirs)
    assert "--disallowedTools" not in a and "Write" in a[a.index("--allowedTools"):]


def test_claude_code_disallows_write_tools_without_write_grant(env):
    for a in (_cc(env, "reviewer", _ctx(env, "reviewer").grants()),
              _cc(env, "worker", {"read": ["/x"], "write": []})):
        assert a[a.index("--disallowedTools") + 1:] == list(claude_code.WRITE_TOOLS)
        allowed = a[a.index("--allowedTools"):a.index("--disallowedTools")]
        assert not set(claude_code.WRITE_TOOLS) & set(allowed)


def test_claude_code_legacy_call_without_grants(env):
    _, project, _, ref, _ = env
    a = _cc(env, "worker", None)
    assert _add_dirs(a) == [str(project.shared), str(ref)] and "--disallowedTools" not in a
    assert "--disallowedTools" in _cc(env, "reviewer", None)
