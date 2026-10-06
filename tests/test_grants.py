"""Path grants: ToolContext.resolve enforces read/write grants per role and project."""

import pytest

from artel.manager import new_kernel
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.tooling import READONLY_ROLES, ToolContext, ToolError, effective_grants


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "src.txt").write_text("ref data")
    extra = tmp_path / "extra"
    extra.mkdir()
    project = Project.create("g", cfg, refs=(ref,))
    ws = project.worker_dir("w1") / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return new_kernel(cfg), project, ws, ref, extra


def ctx_for(env, role="worker"):
    k, project, ws, _, _ = env
    return ToolContext(k, project, role=role, workspace=ws)


def test_write_outside_grants_refused_and_names_grant(env):
    ctx = ctx_for(env)
    other = env[1].root / "workers" / "other.txt"
    with pytest.raises(ToolError) as e:
        ctx.resolve(str(other), write=True)
    assert "not in write grants" in str(e.value) and str(env[2].resolve()) in str(e.value)
    assert ctx.resolve("a.txt", write=True) == env[2].resolve() / "a.txt"
    assert ctx.resolve("shared/d.txt", write=True).parent == env[1].shared.resolve()
    with pytest.raises(ToolError):
        ctx.resolve("/etc/passwd")


def test_refs_readable_not_writable(env):
    ctx = ctx_for(env)
    assert ctx.resolve(str(env[3] / "src.txt")).name == "src.txt"
    with pytest.raises(ToolError, match="write grants"):
        ctx.resolve(str(env[3] / "src.txt"), write=True)
    assert str(env[3].resolve()) not in effective_grants(env[1], "worker", env[2])["write"]


def test_ref_not_writable_even_if_override_asks(env):
    project = env[1]
    project.set_meta(grants={"write": [str(env[3])]})
    with pytest.raises(ToolError):
        ctx_for(env).resolve(str(env[3] / "x"), write=True)


def test_extra_grants_honored(env):
    project, extra = env[1], env[4]
    with pytest.raises(ToolError):
        ctx_for(env).resolve(str(extra / "f"))
    project.set_meta(grants={"read": [str(extra)], "roles": {"worker": {"write": [str(extra)]}}})
    assert ctx_for(env).resolve(str(extra / "f"), write=True).parent == extra.resolve()
    assert ctx_for(env, "reviewer").resolve(str(extra / "f")).parent == extra.resolve()
    with pytest.raises(ToolError):
        ctx_for(env, "librarian").resolve(str(extra / "f"), write=True)


def test_symlink_escape_refused(env, tmp_path):
    ctx = ctx_for(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (env[2] / "link").symlink_to(outside)
    with pytest.raises(ToolError, match="write grants"):
        ctx.resolve("link/x.txt", write=True)
    with pytest.raises(ToolError, match="read grants"):
        ctx.resolve("link/x.txt")


def test_readonly_roles_get_no_write_grants(env):
    for role in READONLY_ROLES:
        assert effective_grants(env[1], role, env[1].shared)["write"] == []
        with pytest.raises(ToolError, match=r"write grants \[\]"):
            ctx_for(env, role).resolve("shared/x", write=True)
    assert ctx_for(env, "reviewer").resolve("shared/x").name == "x"


def test_effective_grants_plain_lists(env):
    g = effective_grants(env[1], "worker", env[2])
    assert set(g) == {"read", "write"}
    assert all(isinstance(x, str) for x in g["read"] + g["write"])
    assert str(env[1].root.resolve()) in g["read"] and str(env[3].resolve()) in g["read"]
    assert set(g["write"]) <= set(g["read"])


def test_protected_harness_files_still_refused(env):
    ctx = ToolContext(env[0], env[1], role="worker", workspace=env[1].root)
    with pytest.raises(ToolError, match="managed by the harness"):
        ctx.resolve("board.db", write=True)
