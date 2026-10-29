"""W2-sandbox: OS sandbox for agents' bash (bubblewrap on Linux, Seatbelt on macOS, clear fallback elsewhere)."""

import os
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


@pytest.mark.real_sandbox_default
def test_default_mode_is_auto_and_fail_closed():
    assert DEFAULT_CONFIG["sandbox"]["mode"] == "auto"
    s = sandbox.settings({})
    assert s["mode"] == "auto" and s["require"] is True and s["gates"] is True


def test_mode_off_keeps_the_old_behaviour(cfg, tmp_path):
    cfg = deep_merge(cfg, {"sandbox": {"mode": "off"}})
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
    strict = deep_merge(cfg, {"sandbox": {"mode": "bwrap", "require": True}})
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


def test_windows_commands_run_in_git_bash_with_utf8(cfg, tmp_path, monkeypatch):
    """Agents and task checks write POSIX shell; on Windows the plain (unsandboxed) plan must use Git Bash, never
    /bin/sh, and fall back to cmd.exe only when Git Bash is missing."""
    from artel import tools

    monkeypatch.setattr(tools, "WIN", True)
    monkeypatch.setattr(tools, "posix_shell", lambda: r"C:\Program Files\Git\bin\bash.exe")
    run = sandbox.plan(cfg, "ls -la && echo ok", cwd=tmp_path, writable=[tmp_path], mode="off")
    assert run.argv == [r"C:\Program Files\Git\bin\bash.exe", "-c", "ls -la && echo ok"] and run.backend == "none"
    assert tools.shell_env({"A": "1"}) == {"A": "1", "PYTHONUTF8": "1"}
    monkeypatch.setattr(tools, "posix_shell", lambda: None)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    assert sandbox.plan(cfg, "dir", cwd=tmp_path, writable=[tmp_path], mode="off").argv[:2] == \
        [r"C:\Windows\system32\cmd.exe", "/d"]
    monkeypatch.setattr(tools, "WIN", False)
    assert tools.shell_argv("x") == ["/bin/sh", "-c", "x"] and "PYTHONUTF8" not in tools.shell_env({})


# --- stage B item 4: reads of secrets denied, fail closed, honest status ------------------------------------------
HOME_SECRETS = [".ssh", ".aws", ".gnupg", ".config/gh", ".netrc", ".docker/config.json", ".kube"]


def _mounts(args, flag):
    return [args[i + 1:i + 3] for i, a in enumerate(args) if a == flag]


def test_bwrap_args_hide_secrets_and_other_projects(tmp_path):
    home, ah, proj = tmp_path / "h", tmp_path / "ah", tmp_path / "ah" / "projects" / "p"
    for d in [home / ".ssh", home / ".aws", home / ".kube", ah / "shims", ah / "tools", proj]:
        d.mkdir(parents=True)
    (home / ".netrc").write_text("x")
    deny = [str(home / ".ssh"), str(home / ".netrc"), str(ah)]
    args = sandbox.bwrap_args("true", cwd=str(proj), writable=[str(proj / "ws")], network=True, deny=deny,
                              expose=[str(proj), str(ah / "shims"), str(ah / "tools")])
    assert [str(home / ".ssh")] in [m[:1] for m in _mounts(args, "--tmpfs")]
    assert ["/dev/null", str(home / ".netrc")] in _mounts(args, "--ro-bind")  # a file is emptied, not hidden
    assert [str(ah)] in [m[:1] for m in _mounts(args, "--tmpfs")]
    exposed = [m[0] for m in _mounts(args, "--ro-bind")]
    assert str(proj) in exposed and str(ah / "shims") in exposed and str(ah / "tools") in exposed
    # order: the denials come after the broad writable binds and before the re-exposure
    assert args.index("--tmpfs") < args.index(str(ah / "tools")) < len(args)


@pytest.mark.skipif(os.name == "nt", reason="bubblewrap is Linux-only; the fake backend is a shell script")
def test_bwrap_args_protected_paths_are_read_only_inside_writable_roots(tmp_path):
    w = tmp_path / "w"
    (w / "skills").mkdir(parents=True)
    (w / "policy.yaml").write_text("rules: []")
    args = sandbox.bwrap_args("true", cwd=str(w), writable=[str(w)], network=True,
                              protected=[str(w / "policy.yaml"), str(w / "skills"), str(w / "artel.yaml")])
    ro = [m[0] for m in _mounts(args, "--ro-bind")]
    assert str(w / "policy.yaml") in ro and str(w / "skills") in ro
    assert str(w / "artel.yaml") not in ro  # missing paths cannot be bound
    assert args.index("--bind") < args.index(str(w / "skills"), args.index("--bind") + 2)


def test_seatbelt_profile_denies_secret_reads_and_protected_writes():
    prof = sandbox.seatbelt_profile(["/w"], network=True, deny=["/Users/a/.ssh", "/Users/a/.artel"],
                                    expose=["/Users/a/.artel/projects/p"], protected=["/Users/a/.artel/policy.yaml"])
    assert '(deny file-read* (subpath "/Users/a/.ssh"))' in prof
    assert prof.index('(deny file-read* (subpath "/Users/a/.artel"))') < \
        prof.index('(allow file-read* (subpath "/Users/a/.artel/projects/p"))')
    assert prof.rindex("(allow file-write*") < prof.index('(deny file-write* (subpath "/Users/a/.artel/policy.yaml"))')


def test_denied_reads_cover_every_secret_and_artel_home(tmp_path, monkeypatch):
    home = tmp_path / "h"
    for rel in HOME_SECRETS:
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("s") if rel in (".netrc", ".docker/config.json") else p.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # what expanduser reads on Windows
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "ah"))
    proj = tmp_path / "ah" / "projects" / "p"
    for d in ("shims", "tools"):
        (tmp_path / "ah" / d).mkdir(parents=True)
    proj.mkdir(parents=True)
    deny, expose = sandbox.read_policy(project_root=proj)
    real = lambda p: str((tmp_path / p).resolve())  # noqa: E731
    for rel in HOME_SECRETS:
        assert real(f"h/{rel}") in deny, rel
    assert real("ah") in deny
    assert set(expose) == {real("ah/projects/p"), real("ah/shims"), real("ah/tools")}


@pytest.mark.skipif(os.name == "nt", reason="bubblewrap is Linux-only; the fake backend is a shell script")
def test_cannot_start_is_fail_closed_with_an_environment_error(cfg, tmp_path, monkeypatch):
    """The backend is present but fails to start for a command: the command must NOT run unsandboxed."""
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox, "bwrap_status", lambda network=True: (True, str(fake)))
    cfg = deep_merge(cfg, {"sandbox": {"mode": "auto"}})
    k, project, ctx = _ctx(cfg, tmp_path)
    marker = tmp_path / "ran"
    with pytest.raises(ToolError, match="sandbox cannot start commands") as exc:
        k.tools["bash"].fn(ctx, command=f"touch {marker}")
    assert not marker.exists()
    from artel.failures import classify
    assert classify(str(exc.value)) == "setup"  # an environment failure, never the worker's fault
    assert "uid map" in sandbox.status(cfg)["failure"]  # the dashboard and `artel providers` show why
    # require: false is the explicit way back to running without isolation
    loose = deep_merge(cfg, {"sandbox": {"require": False}})
    k2, _, ctx2 = _ctx(loose, tmp_path / "loose")
    out = k2.tools["bash"].fn(ctx2, command="echo ran-plain")
    assert out.startswith("[exit 0]") and "ran-plain" in out and "WITHOUT a sandbox" in out


@pytest.mark.skipif(os.name == "nt", reason="bubblewrap is Linux-only; the fake backend is a shell script")
def test_task_checks_fail_closed_too(cfg, tmp_path, monkeypatch):
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox, "bwrap_status", lambda network=True: (True, str(fake)))
    cfg = deep_merge(cfg, {"sandbox": {"mode": "auto"}})
    project = Project.create("g", cfg, name="gate", workdir=str(tmp_path / "deliv"))
    m = Manager(new_kernel(cfg), project)
    res = m.run_gates({"id": "x", "checks": [f"touch {tmp_path / 'ran'}"]})
    assert res[0]["exit"] != 0 and "sandbox cannot start commands" in res[0]["output"]
    assert not (tmp_path / "ran").exists()


@pytest.mark.real_sandbox_default
def test_status_says_plainly_when_there_is_no_backend(cfg, monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_status", lambda network=True: (False, "bubblewrap (bwrap) is not installed"))
    monkeypatch.setattr(sandbox, "seatbelt_status", lambda: (False, "macOS-only"))
    st = sandbox.status(cfg)
    assert st["active"] is False and "команды агентов выполняются без изоляции" in st["message"]
    assert "bubblewrap" in st["message"]  # how to enable
    off = sandbox.status(deep_merge(cfg, {"sandbox": {"mode": "off"}}))
    assert "без изоляции" in off["message"] and "mode: off" in off["message"]


def test_bwrap_shares_the_host_network_so_a_loopback_proxy_stays_reachable():
    on = sandbox.bwrap_args("x", cwd="/", writable=[], network=True)
    assert "--share-net" in on and "--unshare-net" not in on
    off = sandbox.bwrap_args("x", cwd="/", writable=[], network=False)
    assert "--unshare-net" in off and "--share-net" not in off


@needs_bwrap
def test_sandboxed_reads_of_secrets_fail_while_normal_reads_work(cfg, tmp_path, monkeypatch):
    home = tmp_path / "home-dir"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("PRIVATE")
    (home / ".netrc").write_text("machine x password y")
    (home / "notes.txt").write_text("fine")
    monkeypatch.setenv("HOME", str(home))
    cfg = deep_merge(cfg, {"sandbox": {"mode": "bwrap"}})
    k, project, ctx = _ctx(cfg, tmp_path / "p")
    ah = Path(__import__("os").environ["ARTEL_HOME"])
    (ah / "other-secret").write_text("tokens")
    out = k.tools["bash"].fn(ctx, command=f"cat {home}/.ssh/id_test; echo rc=$?")
    assert "PRIVATE" not in out and "rc=1" in out
    assert "machine" not in k.tools["bash"].fn(ctx, command=f"cat {home}/.netrc; true")
    assert "fine" in k.tools["bash"].fn(ctx, command=f"cat {home}/notes.txt")
    assert "tokens" not in k.tools["bash"].fn(ctx, command=f"cat {ah}/other-secret; true")
    assert "rc=0" in k.tools["bash"].fn(ctx, command=f"ls {project.root} >/dev/null; echo rc=$?")  # own project: readable


@needs_bwrap
def test_sandboxed_command_reaches_a_loopback_proxy(cfg, tmp_path):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # a forward proxy sees the absolute URL
            body = b"via-proxy"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        k, project, ctx = _ctx(deep_merge(cfg, {"sandbox": {"mode": "bwrap", "network": True}}), tmp_path)
        py = ("import urllib.request as u;"
              f"o=u.build_opener(u.ProxyHandler({{'http':'http://127.0.0.1:{port}'}}));"
              "print(o.open('http://example.invalid/x',timeout=10).read().decode())")
        out = k.tools["bash"].fn(ctx, command=f"python3 -c \"{py}\"")
        assert "via-proxy" in out, out
        k2, _, ctx2 = _ctx(deep_merge(cfg, {"sandbox": {"mode": "bwrap", "network": False}}), tmp_path / "off")
        assert "via-proxy" not in k2.tools["bash"].fn(ctx2, command=f"python3 -c \"{py}\"")
    finally:
        srv.shutdown()


@pytest.mark.real_sandbox_default
def test_providers_overview_and_cli_show_the_sandbox_state(cfg, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "bwrap_status", lambda network=True: (False, "bubblewrap (bwrap) is not installed"))
    monkeypatch.setattr(sandbox, "seatbelt_status", lambda: (False, "macOS-only"))
    from artel import cli, providers
    monkeypatch.setattr(providers, "codex_status", lambda c: {"installed": False, "logged_in": False})
    monkeypatch.setattr(providers, "claude_code_status", lambda c: {"installed": False, "logged_in": False})
    assert "без изоляции" in providers.overview(cfg)["sandbox"]["message"]
    import argparse
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg, raising=False)
    cli.cmd_providers(argparse.Namespace(action=None, name=None, config=None, args=[], rest=[]))
    assert "без изоляции" in capsys.readouterr().out
