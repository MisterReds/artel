"""Stage B item 5: agents cannot write policy.yaml, artel.yaml or the skills library — not through the file tools,
not through the skills tools, not through bash (policy), not inside the OS sandbox."""

from pathlib import Path

import pytest

from artel import policy, sandbox
from artel.manager import new_kernel
from artel.project import DEFAULT_CONFIG, Project, artel_home, deep_merge
from artel.tooling import ToolContext, ToolError, effective_grants

BWRAP_OK, BWRAP_WHY = sandbox.bwrap_status()
needs_bwrap = pytest.mark.skipif(not BWRAP_OK, reason=f"bubblewrap unusable here: {BWRAP_WHY}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ARTEL_HOME", str(home))
    # the worst case: the project config grants write on the whole of ARTEL_HOME and on the project root
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("g", cfg, name="prot", workdir=str(tmp_path / "deliv"))
    cfg["grants"] = {"write": [str(artel_home()), str(project.root), str(artel_home() / "skills"),
                               str(artel_home() / "policy.yaml")]}
    project = Project.create("g", cfg, name="prot2", workdir=str(tmp_path / "deliv2"))
    ws = tmp_path / "ws"
    ws.mkdir()
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=ws)
    targets = {"home policy.yaml": artel_home() / "policy.yaml", "home artel.yaml": artel_home() / "artel.yaml",
               "skills library": artel_home() / "skills" / "mine" / "SKILL.md",
               "project policy.yaml": project.root / "policy.yaml", "project artel.yaml": project.root / "artel.yaml"}
    for p in targets.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("rules: []\n")
    return k, project, ctx, targets


@pytest.mark.parametrize("name", ["home policy.yaml", "home artel.yaml", "skills library", "project policy.yaml",
                                  "project artel.yaml"])
def test_file_tools_refuse_protected_paths_even_with_a_grant(env, name):
    k, project, ctx, targets = env
    path = targets[name]
    before = path.read_text()
    with pytest.raises(ToolError, match="protected"):
        k.tools["write_file"].fn(ctx, path=str(path), content="rules: [pwned]\n")
    with pytest.raises(ToolError, match="protected"):
        k.tools["edit_file"].fn(ctx, path=str(path), old="rules", new="pwned")
    assert path.read_text() == before


def test_protected_paths_are_outside_every_write_grant(env):
    k, project, ctx, targets = env
    g = effective_grants(project, "worker", ctx.workspace, None)
    assert g["write"], "the test needs grants to look at"
    inside_protected = [w for w in g["write"] for p in targets.values()
                        if Path(w).resolve() == p.resolve() or p.resolve() in Path(w).resolve().parents]
    assert not inside_protected  # grants ON the config are void (a grant on its parent is caught by resolve())


def test_ordinary_files_in_a_granted_directory_stay_writable(env):
    k, project, ctx, targets = env
    out = k.tools["write_file"].fn(ctx, path=str(artel_home() / "notes.txt"), content="hi")
    assert "wrote" in out


def test_skill_tools_cannot_escape_the_skill_directory(env):
    """patch() joined file_path without the escape check write_file has: ../../artel.yaml was patchable."""
    k, project, ctx, targets = env
    store = k.get("skills")
    store.create("mine-skill", "---\nname: mine-skill\ndescription: x\n---\nbody\n", actor="agent:t")
    victim = artel_home() / "artel.yaml"
    victim.write_text("alpha: 1\n")
    ctx.viewed.add(("mine-skill", "../../artel.yaml"))
    with pytest.raises(ToolError, match="escapes"):
        k.tools["skill_manage"].fn(ctx, action="patch", name="mine-skill", file_path="../../artel.yaml",
                                   old="alpha", new="pwned")
    assert victim.read_text() == "alpha: 1\n"
    with pytest.raises(ToolError, match="escapes|support files"):
        k.tools["skill_manage"].fn(ctx, action="write_file", name="mine-skill", file_path="references/../../../policy.yaml",
                                   content="x")


@pytest.mark.parametrize("cmd", [
    "echo 'rules: []' > {home}/policy.yaml",
    "echo x >> $ARTEL_HOME/artel.yaml",
    "echo x >> ${{ARTEL_HOME}}/artel.yaml",
    "cp /tmp/evil {home}/policy.yaml",
    "mv /tmp/evil $ARTEL_HOME/policy.yaml",
    "cat /tmp/x | tee {home}/artel.yaml",
    "sed -i 's/a/b/' {home}/artel.yaml",
    "sed -i 's/a/b/' ~/.artel/policy.yaml",
    "F=$ARTEL_HOME/policy.yaml; echo x > $F",
    "echo x > {home}/skills/mine/SKILL.md",
    "rm -rf $ARTEL_HOME/skills",
    "echo x > {root}/policy.yaml",
    "bash -c 'echo x > {home}/policy.yaml'",
    "echo x > ../../policy.yaml",
])
def test_policy_forbids_commands_that_write_protected_paths(env, cmd):
    k, project, ctx, targets = env
    cmd = cmd.format(home=artel_home(), root=project.root)
    assert policy.evaluate(cmd, project.root).decision == "forbidden", cmd
    with pytest.raises(ToolError, match="blocked by policy"):
        k.tools["bash"].fn(ctx, command=cmd)


@pytest.mark.parametrize("cmd", [
    "cat {home}/policy.yaml", "grep rules $ARTEL_HOME/artel.yaml", "ls {home}/skills", "echo x > notes/policy.yaml.txt",
    "echo x > out.yaml", "cp {home}/policy.yaml /tmp/copy",
])
def test_policy_allows_reading_protected_paths(env, cmd):
    k, project, ctx, targets = env
    assert policy.evaluate(cmd.format(home=artel_home()), project.root).decision == "allow", cmd


@needs_bwrap
@pytest.mark.parametrize("name", ["home policy.yaml", "home artel.yaml", "skills library", "project policy.yaml",
                                  "project artel.yaml"])
def test_sandbox_makes_protected_paths_read_only_even_when_writable_ancestors(env, name):
    """The writable list contains the parent of the protected file; the file itself stays read-only."""
    k, project, ctx, targets = env
    path = targets[name]
    before = path.read_text()
    run = sandbox.plan(deep_merge(k.config, {"sandbox": {"mode": "bwrap", "writable": [str(path.parent)]}}),
                       f"echo pwned > {path}; echo rc=$?", cwd=ctx.workspace, writable=[ctx.workspace],
                       project_root=project.root)
    assert run.sandboxed
    import subprocess
    p = subprocess.run(run.argv, capture_output=True, text=True)
    assert path.read_text() == before, (p.stdout, p.stderr)
