"""W2-sandbox: OS sandbox for agents' bash (bubblewrap on Linux, Seatbelt on macOS, clear fallback elsewhere)."""

import shutil
import uuid
from pathlib import Path

import pytest

from artel import sandbox
from artel.manager import Manager, new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.tooling import ToolContext, ToolError

BWRAP_OK, BWRAP_WHY = sandbox.bwrap_status()
needs_bwrap = pytest.mark.skipif(not BWRAP_OK, reason=f"bubblewrap unusable here: {BWRAP_WHY}")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


@pytest.fixture
def outside():
    """A directory outside every temp dir (tmp_path is writable inside the sandbox by design)."""
    d = Path(__file__).resolve().parent / f".sbx-outside-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _ctx(cfg, root: Path):
    project = Project.create("g", cfg, name="sbx", workdir=str(root / "deliv"))
    ws = root / "ws"
    ws.mkdir(parents=True)
    k = new_kernel(cfg)
    return k, project, ToolContext(k, project, role="worker", workspace=ws)


def test_default_is_off_and_unchanged(cfg, tmp_path):
    k, project, ctx = _ctx(cfg, tmp_path)
    p = sandbox.plan(cfg, "echo hi", cwd=tmp_path, writable=[tmp_path])
    assert p.backend == "none" and not p.note
    assert k.tools["bash"].fn(ctx, command="echo hi > f.txt && cat f.txt").startswith("[exit 0]\nhi")


def test_bwrap_args_shape():
    args = sandbox.bwrap_args("echo x", cwd="/w", writable=["/w", "/tmp"], network=False)
    assert args[:1] == ["bwrap"] and args[-3:] == ["/bin/sh", "-c", "echo x"]
    assert args.index("--ro-bind") < args.index("--bind")  # writable re-binds come after the read-only root
    assert "--unshare-net" in args
    assert "--unshare-net" not in sandbox.bwrap_args("x", cwd="/", writable=[], network=True)


def test_seatbelt_profile_denies_writes_outside_allowed():
    prof = sandbox.seatbelt_profile(["/Users/a/wt", '/tmp/we"ird'], network=False)
    assert "(deny file-write*)" in prof and '(subpath "/Users/a/wt")' in prof and '\\"ird' in prof
    assert "(deny network-outbound)" in prof
    assert "network" not in sandbox.seatbelt_profile(["/x"], network=True)


def test_fallback_when_unavailable_has_clear_note(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_status", lambda network=True: (False, "bwrap is not installed"))
    monkeypatch.setattr(sandbox, "seatbelt_status", lambda: (False, "macOS-only"))
    cfg = deep_merge(cfg, {"sandbox": {"mode": "auto"}})
    k, project, ctx = _ctx(cfg, tmp_path)
    out = k.tools["bash"].fn(ctx, command="echo ran")
    assert out.startswith("[exit 0]\n[note: sandbox `auto` unavailable") and "WITHOUT a sandbox" in out and "ran" in out
    k.tools["bash"].fn(ctx, command="true")
    notes = [e for e in project.log.read() if e["kind"] == "sandbox/unavailable"]
    assert len(notes) == 1 and "bwrap is not installed" in notes[0]["note"]
    strict = deep_merge(cfg, {"sandbox": {"require": True}})
    k2, _, ctx2 = _ctx(strict, tmp_path / "strict")
    with pytest.raises(ToolError, match="NOT run"):
        k2.tools["bash"].fn(ctx2, command="echo nope")


def test_policy_still_applies_under_sandbox(cfg, tmp_path):
    cfg = deep_merge(cfg, {"sandbox": {"mode": "auto"}})
    k, project, ctx = _ctx(cfg, tmp_path)
    with pytest.raises(ToolError, match="blocked by policy"):
        k.tools["bash"].fn(ctx, command="rm -rf /")


@needs_bwrap
def test_bwrap_blocks_writes_outside_allowed_paths(cfg, outside):
    cfg = deep_merge(cfg, {"sandbox": {"mode": "bwrap", "temp": False}})
    k, project, ctx = _ctx(cfg, outside / "proj")
    victim = outside / "victim.txt"
    out = k.tools["bash"].fn(ctx, command=f"echo pwned > {victim}; echo rc=$?")
    assert not victim.exists() and "Read-only file system" in out
    out = k.tools["bash"].fn(ctx, command=f"touch {project.root / 'events.jsonl.x'}; echo rc=$?")
    assert "rc=1" in out  # the project root (logs, board) is read-only too
    out = k.tools["bash"].fn(ctx, command='echo ok > own.txt && echo d > "$ARTEL_SHARED/d.txt" && cat own.txt')
    assert out.startswith("[exit 0]") and "ok" in out
    assert (ctx.workspace / "own.txt").exists() and (project.shared / "d.txt").read_text() == "d\n"
    assert "ok" in k.tools["bash"].fn(ctx, command="cat /etc/hostname >/dev/null && echo ok")  # system is readable
    assert not [e for e in project.log.read() if e["kind"] == "sandbox/unavailable"]


@needs_bwrap
def test_bwrap_temp_and_network_settings(cfg, tmp_path):
    k, project, ctx = _ctx(deep_merge(cfg, {"sandbox": {"mode": "bwrap"}}), tmp_path)
    tmp = tmp_path / "t.txt"
    assert "[exit 0]" in k.tools["bash"].fn(ctx, command=f"echo t > {tmp}")
    assert tmp.exists()
    offline = deep_merge(cfg, {"sandbox": {"mode": "bwrap", "network": False}})
    k2, _, ctx2 = _ctx(offline, tmp_path / "off")
    out = k2.tools["bash"].fn(ctx2, command="cat /proc/net/dev | grep -c : ")
    assert out.strip().endswith("1")  # only the loopback interface inside a fresh network namespace


@needs_bwrap
def test_full_run_with_sandbox_and_isolation_and_sandboxed_gates(cfg):
    cfg = deep_merge(cfg, {"sandbox": {"mode": "auto", "gates": True}, "isolation": {"worktrees": True}})
    project, status = run_goal("Sandboxed isolated delivery", cfg)
    assert status == "done"
    assert (project.shared / "report.md").exists()
    assert not [e for e in project.log.read() if e["kind"] == "sandbox/unavailable"]
    gates = [e for e in project.log.read() if e["kind"] == "gate/result"]
    assert gates and all(g["exit"] == 0 for g in gates)


@needs_bwrap
def test_sandboxed_gate_cannot_write_outside(cfg, outside):
    cfg = deep_merge(cfg, {"sandbox": {"mode": "bwrap", "gates": True, "temp": False}})
    project = Project.create("g", cfg, name="gate", workdir=str(outside / "deliv"))
    m = Manager(new_kernel(cfg), project)
    victim = outside / "gate-victim.txt"
    res = m.run_gates({"id": "x", "checks": [f"echo x > {victim}", "echo ok > inside.txt"]})
    assert res[0]["exit"] != 0 and not victim.exists()
    assert res[1]["exit"] == 0 and (project.shared / "inside.txt").exists()
