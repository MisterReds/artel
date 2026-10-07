"""Path grants: ToolContext.resolve enforces read/write allow-lists."""
import pytest

from artel.kernel import Kernel
from artel.project import Project
from artel.tooling import READONLY_ROLES, ToolContext, ToolError, effective_grants
from artel.runtimes.claude_code import build_dir_args
from artel.runtimes.codex import build_args


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "a.txt").write_text("x")
    extra = tmp_path / "extra"
    extra.mkdir()
    k = Kernel()
    project = Project.create("goal", k.config, name="g", refs=(ref,))
    ws = project.root / "workers" / "w" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return k, project, ws, ref, extra


def ctx_for(env, role="worker"):
    k, project, ws, *_ = env
    return ToolContext(k, project, role=role, workspace=ws)


def test_defaults(env):
    c = ctx_for(env)
    assert c.resolve("x.txt", write=True).parent == env[2].resolve()
    c.resolve("shared/out.md", write=True)
    c.resolve("shared/out.md")
    assert c.resolve(str(env[3] / "a.txt")).name == "a.txt"


def test_write_outside_refused_and_names_grant(env):
    c = ctx_for(env)
    with pytest.raises(ToolError) as e:
        c.resolve(str(env[1].root / "other.txt"), write=True)
    assert "write to" in str(e.value) and "not in write grants" in str(e.value) and str(env[2].resolve()) in str(e.value)
    with pytest.raises(ToolError, match="outside|not in read grants"):
        c.resolve("/etc/passwd")


def test_ref_read_only(env):
    c = ctx_for(env)
    with pytest.raises(ToolError, match="not in write grants"):
        c.resolve(str(env[3] / "a.txt"), write=True)
    assert str(env[3].resolve()) not in effective_grants(env[1], "worker", env[2])["write"]


def test_project_and_role_overrides(env):
    k, project, ws, ref, extra = env
    project.set_meta(grants={"write": [str(extra)], "roles": {"worker": {"write": ["shared/sub"]}}})
    c = ctx_for(env)
    c.resolve(str(extra / "f"), write=True)
    c.resolve("shared/sub/f", write=True)
    assert effective_grants(project, "reviewer")["write"] == []
    # a ref listed as a write grant is still refused
    project.set_meta(grants={"write": [str(ref)]})
    with pytest.raises(ToolError, match="not in write grants"):
        ctx_for(env).resolve(str(ref / "a.txt"), write=True)


def test_readonly_role_and_protected(env):
    assert "reviewer" in READONLY_ROLES
    with pytest.raises(ToolError, match="not in write grants"):
        ctx_for(env, "reviewer").resolve("shared/x", write=True)
    with pytest.raises(ToolError, match="managed by the harness"):
        ToolContext(env[0], env[1], role="worker", workspace=env[1].root).resolve("board.db", write=True)


def test_runtime_args(env, tmp_path):
    k, project, ws, ref, extra = env
    project.set_meta(grants={"write": [str(extra)]})
    g = effective_grants(project, "worker", ws)
    cc = {"binary": "codex", "sandbox": "workspace-write"}
    kw = dict(workspace=ws, shared=project.shared, role="worker", bridge_url="u", bridge_token="t", effort=None,
              thread_id=None, last_message=tmp_path / "m", grants=g)
    a = build_args(cc, **kw)
    dirs = [a[i + 1] for i, x in enumerate(a) if x == "--add-dir"]
    assert str(extra.resolve()) in dirs and str(ref.resolve()) not in dirs
    assert "--add-dir" not in build_args(cc, **{**kw, "role": "reviewer", "grants": effective_grants(project, "reviewer", ws)})
    cd = build_dir_args(g)
    assert str(ref.resolve()) in cd and str(extra.resolve()) in cd
