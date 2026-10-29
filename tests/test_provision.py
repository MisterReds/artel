"""Item 9: tools without the owner. No real root, no real network: a local HTTP server, fake micromamba, fake wrapper."""
import hashlib
import io
import os
import re
import stat
import sys
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from artel import provision, tools

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake POSIX scripts")
PLATFORMS = {"linux-x86_64", "linux-aarch64", "macos-arm64", "macos-x86_64", "windows-x86_64"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    monkeypatch.setenv("PATH", str(sysbin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(provision, "ROOT_WRAPPER", str(tmp_path / "no-such-wrapper"))
    monkeypatch.setattr(provision, "SUDO", [])
    monkeypatch.setattr(provision, "platform_key", lambda: "linux-x86_64")
    provision.reset()
    real = tools._download

    def local_only(url, dest, *a, **k):  # the suite never reaches the network
        if not url.startswith("http://127.0.0.1"):
            raise OSError(f"network disabled in tests: {url}")
        return real(url, dest, *a, **k)

    monkeypatch.setattr(tools, "_download", local_only)
    return tmp_path


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(root=root, url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def catalogue(url, sha, member=None, name="fakerg", binary="fakerg"):
    entry = {"version": "1.2.3", "bin": binary, "platforms": {"linux-x86_64": {"url": url, "sha256": sha}}}
    if member:
        entry["platforms"]["linux-x86_64"]["member"] = member
    return {name: entry}


def events_of(log):
    return [(k, p) for k, p in log]


def test_catalogue_schema_and_every_entry_has_sha256():
    cat = provision.catalogue()
    assert {"ripgrep", "jq", "gh", "uv", "micromamba"} <= set(cat)
    for name, e in cat.items():
        assert re.fullmatch(r"[a-z0-9._-]+", name) and e["version"] and e["bin"]
        assert e["platforms"], name
        for plat, p in e["platforms"].items():
            assert plat in PLATFORMS, (name, plat)
            assert p["url"].startswith("https://github.com/"), (name, plat)
            assert re.fullmatch(r"[0-9a-f]{64}", p["sha256"]), (name, plat)
            assert e["version"].split("-")[0] in p["url"], (name, plat)  # the url is the pinned version


def test_catalogue_covers_all_five_platforms_for_core_tools():
    cat = provision.catalogue()
    for name in ("ripgrep", "jq", "gh", "uv", "micromamba"):
        assert set(cat[name]["platforms"]) == PLATFORMS, name


@posix_only
def test_already_on_path_is_used_and_nothing_is_installed(home):
    exe = home / "sysbin" / "fakerg"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    log = []
    assert provision.ensure("fakerg", log=lambda k, **p: log.append((k, p))) == str(exe)
    assert log == [] and not tools.bin_dir().exists()


@posix_only
def test_download_verified_and_placed_with_decision_event(home, server, monkeypatch):
    data = b"#!/bin/sh\necho fakerg 1.2.3\n"
    (server.root / "fakerg").write_bytes(data)
    monkeypatch.setattr(provision, "catalogue",
                        lambda: catalogue(server.url + "/fakerg", hashlib.sha256(data).hexdigest()))
    log = []
    path = provision.ensure("fakerg", why="check needs it", log=lambda k, **p: log.append((k, p)))
    assert path == str(tools.bin_dir() / "fakerg")
    assert os.access(path, os.X_OK) and Path(path).read_bytes() == data
    [(kind, ev)] = log
    assert kind == "provision/install"
    assert ev["tool"] == "fakerg" and ev["method"] == "download" and ev["version"] == "1.2.3"
    assert ev["path"] == path and ev["why"] == "check needs it"
    assert tools.resolve("fakerg") == path  # agents' shells find it through the tools dir


@posix_only
def test_wrong_checksum_is_refused_and_nothing_is_left(home, server, monkeypatch):
    (server.root / "fakerg").write_bytes(b"#!/bin/sh\necho evil\n")
    monkeypatch.setattr(provision, "catalogue", lambda: catalogue(server.url + "/fakerg", "0" * 64))
    monkeypatch.setattr(provision, "_conda", lambda *a, **k: (None, "conda off"))
    log = []
    assert provision.ensure("fakerg", log=lambda k, **p: log.append((k, p))) is None
    assert not list(tools.bin_dir().glob("*")) if tools.bin_dir().exists() else True
    leftovers = [p for p in (home / "home").rglob("*") if p.is_file() and p.name not in ("provision.jsonl",)]
    assert leftovers == []
    assert log[-1][0] == "provision/failed" and "checksum" in log[-1][1]["reason"]
    assert "fakerg" in provision.last_reason("fakerg") or "checksum" in provision.last_reason("fakerg")


@posix_only
def test_archive_member_is_extracted_by_name(home, server, monkeypatch):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        body = b"#!/bin/sh\necho in archive\n"
        info = tarfile.TarInfo("fakerg-1.2.3-linux/fakerg")
        info.size, info.mode = len(body), 0o644
        t.addfile(info, io.BytesIO(body))
        other = tarfile.TarInfo("fakerg-1.2.3-linux/README")
        other.size = 1
        t.addfile(other, io.BytesIO(b"x"))
    (server.root / "fakerg.tar.gz").write_bytes(buf.getvalue())
    monkeypatch.setattr(provision, "catalogue", lambda: catalogue(
        server.url + "/fakerg.tar.gz", hashlib.sha256(buf.getvalue()).hexdigest(), member="fakerg"))
    path = provision.ensure("fakerg")
    assert path and Path(path).read_bytes() == body and os.access(path, os.X_OK)
    assert sorted(p.name for p in tools.bin_dir().iterdir()) == ["fakerg"]


@posix_only
def test_unpinned_platform_skips_download(home, monkeypatch):
    monkeypatch.setattr(provision, "platform_key", lambda: "linux-riscv64")
    monkeypatch.setattr(provision, "catalogue", lambda: catalogue("http://127.0.0.1:1/x", "0" * 64))
    assert provision.ensure("fakerg") is None
    assert "no pinned build" in provision.last_reason("fakerg")


def fake_micromamba(home):
    """Creates PREFIX/bin/<pkg> on `install`, deletes it on `remove`; logs every call."""
    mm = tools.bin_dir() / "micromamba"
    tools.bin_dir().mkdir(parents=True, exist_ok=True)
    mm.write_text(
        '#!/bin/sh\necho "$*" >> "%s"\n'
        'cmd=$1; shift; prefix=""; pkgs=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -p) prefix=$2; shift;; -c|--channel) shift;; -*) ;; *) pkgs="$pkgs $1";; esac; shift; done\n'
        'case "$cmd" in\n'
        '  create|install) for p in $pkgs; do [ "$p" = broken ] && exit 1; mkdir -p "$prefix/bin"; printf "#!/bin/sh\\n" > "$prefix/bin/$p"; chmod 755 "$prefix/bin/$p"; done;;\n'
        '  remove) for p in $pkgs; do rm -f "$prefix/bin/$p"; done;;\n'
        'esac\n' % (home / "mm.log"))
    mm.chmod(0o755)
    return mm


@posix_only
def test_conda_install_for_tool_not_in_catalogue(home):
    fake_micromamba(home)
    log = []
    path = provision.ensure("pdftk", why="pdf split", log=lambda k, **p: log.append((k, p)))
    env = tools.tools_dir() / "conda" / "envs" / "artel"
    assert path == str(env / "bin" / "pdftk")
    [(kind, ev)] = log
    assert kind == "provision/install" and ev["method"] == "conda" and ev["packages"] == ["pdftk"] and ev["why"] == "pdf split"
    call = (home / "mm.log").read_text()
    assert "conda-forge" in call and "--override-channels" in call and str(env) in call
    assert str(env / "bin") in tools.env_with_tools()["PATH"].split(os.pathsep)  # the shells of agents see it
    rec = provision.installed()["pdftk"]
    assert rec["method"] == "conda" and rec["packages"] == ["pdftk"]


@posix_only
def test_conda_uses_package_mapping_and_rejects_bad_names(home, monkeypatch):
    fake_micromamba(home)
    monkeypatch.setattr(provision, "packages", lambda: {"mytool": {"conda": "real-pkg"}})
    assert provision.ensure("mytool") is None  # fake creates bin/real-pkg, not bin/mytool: the binary is not there
    assert "real-pkg" in (home / "mm.log").read_text()
    for bad in ("../x", "a b", "-rf", "x;id", "", "A$B", "a\nb"):
        before = (home / "mm.log").read_text()
        assert provision.ensure(bad) is None
        assert (home / "mm.log").read_text() == before


@posix_only
def test_remove_undoes_artel_installs_only(home, server, monkeypatch):
    data = b"#!/bin/sh\n"
    (server.root / "fakerg").write_bytes(data)
    monkeypatch.setattr(provision, "catalogue", lambda: catalogue(server.url + "/fakerg", hashlib.sha256(data).hexdigest()))
    fake_micromamba(home)
    p1 = provision.ensure("fakerg")
    p2 = provision.ensure("pdftk")
    log = []
    assert provision.remove("fakerg", log=lambda k, **p: log.append((k, p)))["ok"] and not Path(p1).exists()
    assert provision.remove("pdftk")["ok"] and not Path(p2).exists()
    assert "fakerg" not in provision.installed() and "pdftk" not in provision.installed()
    assert log[0][0] == "provision/remove"
    res = provision.remove("never-installed")
    assert not res["ok"] and "not installed by Artel" in res["error"]
    sysexe = home / "sysbin" / "sysonly"
    sysexe.write_text("#!/bin/sh\n")
    sysexe.chmod(0o755)
    assert not provision.remove("sysonly")["ok"] and sysexe.exists()


@posix_only
def test_system_package_through_wrapper_only_when_it_exists(home, monkeypatch):
    assert provision.ensure("pdftk") is None  # no micromamba, no wrapper
    assert "no system package manager route" in provision.last_reason("pdftk") or "wrapper" in provision.last_reason("pdftk")
    wrapper = home / "artel-root"
    wrapper.write_text('#!/bin/sh\necho "$*" >> "%s"\nprintf "#!/bin/sh\\n" > "%s/$2"; chmod 755 "%s/$2"\n'
                       % (home / "root.log", home / "sysbin", home / "sysbin"))
    wrapper.chmod(0o755)
    monkeypatch.setattr(provision, "ROOT_WRAPPER", str(wrapper))
    log = []
    path = provision.ensure("pdftk", why="w", log=lambda k, **p: log.append((k, p)))
    assert path == str(home / "sysbin" / "pdftk")
    assert (home / "root.log").read_text().strip() == "apt-install pdftk"
    [(kind, ev)] = log
    assert kind == "provision/install" and ev["method"] == "apt"
    assert "pdftk" in provision.installed()
    res = provision.remove("pdftk")
    assert not res["ok"] and "apt" in res["error"] and "pdftk" in provision.installed()


@posix_only
def test_wrapper_called_via_sudo_n(home, monkeypatch):
    wrapper = home / "artel-root"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(provision, "ROOT_WRAPPER", str(wrapper))
    monkeypatch.setattr(provision, "SUDO", ["sudo", "-n"])
    seen = []
    monkeypatch.setattr(provision.subprocess, "run", lambda argv, **k: seen.append(argv) or SimpleNamespace(returncode=1, stdout="", stderr="no"))
    provision.ensure("pdftk")
    assert seen == [["sudo", "-n", str(wrapper), "apt-install", "pdftk"]]  # no micromamba: only the wrapper ran


@posix_only
def test_order_download_then_conda_then_system(home, server, monkeypatch):
    order = []
    monkeypatch.setattr(provision, "_download_tool", lambda *a, **k: order.append("download") or (None, "x"))
    monkeypatch.setattr(provision, "_conda", lambda *a, **k: order.append("conda") or (None, "x"))
    monkeypatch.setattr(provision, "_system", lambda *a, **k: order.append("system") or (None, "x", ""))
    monkeypatch.setattr(provision, "_find", lambda t: None)
    assert provision.ensure("ripgrep") is None and order == ["download", "conda", "system"]


@posix_only
def test_sandbox_fix_calls_apparmor_once_and_logs(home, monkeypatch):
    assert provision.fix_bwrap("bwrap: setting up uid map: Permission denied") is False  # no wrapper
    wrapper = home / "artel-root"
    wrapper.write_text('#!/bin/sh\necho "$*" >> "%s"\n' % (home / "root.log"))
    wrapper.chmod(0o755)
    monkeypatch.setattr(provision, "ROOT_WRAPPER", str(wrapper))
    assert provision.fix_bwrap("some other error") is False
    assert provision.fix_bwrap("bwrap: setting up uid map: Permission denied") is True
    assert provision.fix_bwrap("bwrap: setting up uid map: Permission denied") is False  # once
    assert (home / "root.log").read_text().strip() == "apparmor-bwrap"
    journal = (tools.tools_dir() / "provision.jsonl").read_text()
    assert "apparmor-bwrap" in journal


@posix_only
def test_sandbox_retries_after_fix(home, monkeypatch):
    from artel import sandbox

    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda b: "/usr/bin/bwrap")
    sandbox._probe_cache.clear()
    answers = iter([(False, "bwrap: setting up uid map: Permission denied"), (True, "")])
    monkeypatch.setattr(sandbox, "_probe", lambda argv: next(answers))
    fixed = []
    monkeypatch.setattr(provision, "fix_bwrap", lambda err: fixed.append(err) or True)
    assert sandbox.bwrap_status() == (True, "/usr/bin/bwrap") and len(fixed) == 1


def test_prepare_checks_extracts_first_programs(home):
    progs = provision.first_programs(["cd sub && rg -n foo | jq .", "FOO=1 uv run pytest", "echo hi; ./run.sh",
                                      "python3 -m pytest -q", "$(which x) y", "if [ -f a ]; then gh pr list; fi"])
    assert progs >= {"rg", "jq", "uv", "python3", "gh"}
    assert not progs & {"cd", "echo", "FOO=1", "./run.sh", "if", "["}


@posix_only
def test_prepare_checks_ensures_known_missing_and_reports_unknown(home, server, monkeypatch):
    data = b"#!/bin/sh\n"
    (server.root / "fakerg").write_bytes(data)
    monkeypatch.setattr(provision, "catalogue", lambda: catalogue(server.url + "/fakerg", hashlib.sha256(data).hexdigest()))
    log = []
    missing = provision.prepare_checks(["fakerg -n x | nosuchprog_zz", "ls"], log=lambda k, **p: log.append((k, p)))
    assert missing == ["nosuchprog_zz"]  # unknown names are never installed blindly
    assert (tools.bin_dir() / "fakerg").exists() and [k for k, _ in log] == ["provision/install"]


@posix_only
def test_gate_runner_provisions_before_checks(home, tmp_path, monkeypatch):
    from artel.manager import Manager
    from artel.project import DEFAULT_CONFIG, Project, deep_merge

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("g", cfg, name="gates", workdir=str(tmp_path / "wd"))
    mgr = Manager.__new__(Manager)
    mgr.project, mgr.cfg = project, cfg
    seen = []
    monkeypatch.setattr(provision, "prepare_checks", lambda checks, cfg=None, log=None: seen.append(list(checks)) or [])
    mgr.run_gates({"id": "t1", "checks": ["echo ok"]})
    assert seen == [["echo ok"]]
    seen.clear()
    mgr.cfg = deep_merge(cfg, {"provision": {"auto": False}})
    mgr.run_gates({"id": "t1", "checks": ["echo ok"]})
    assert seen == []


@posix_only
def test_gate_finds_tool_installed_by_provisioning(home, tmp_path, monkeypatch):
    from artel.manager import Manager
    from artel.project import DEFAULT_CONFIG, Project, deep_merge

    tools.bin_dir().mkdir(parents=True)
    exe = tools.bin_dir() / "artelprobe"
    exe.write_text("#!/bin/sh\necho probe-ok\n")
    exe.chmod(0o755)
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("g", cfg, name="gates", workdir=str(tmp_path / "wd"))
    mgr = Manager.__new__(Manager)
    mgr.project, mgr.cfg = project, cfg
    res = mgr.run_gates({"id": "t1", "checks": ["artelprobe"]})
    assert res[0]["exit"] == 0 and "probe-ok" in res[0]["output"]


def test_tool_install_roles():
    from artel.manager import new_kernel
    from artel.project import DEFAULT_CONFIG, deep_merge

    k = new_kernel(deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}))
    allowed = {"worker", "tester", "manager"}
    for role in ("worker", "tester", "manager", "reviewer", "auditor", "planner", "librarian", "doctor"):
        names = {t.name for t in k.tools_for(role)}
        assert ("tool_install" in names) == (role in allowed), role


@posix_only
def test_tool_install_returns_path_or_reason_and_logs(home, tmp_path, monkeypatch):
    from artel.plugins import provision_tools

    events = []
    project = SimpleNamespace(root=tmp_path, shared=tmp_path, log=SimpleNamespace(append=lambda k, **p: events.append((k, p))))
    ctx = SimpleNamespace(project=project, role="worker", actor="worker", task={"id": "t-1"}, kernel=SimpleNamespace(config={}))
    exe = home / "sysbin" / "have"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert provision_tools.tool_install(ctx, "have", "need it") == str(exe)
    out = provision_tools.tool_install(ctx, "zz-nothing", "need it")
    assert out.startswith("not installed") and "zz-nothing" in out
    from artel.tooling import ToolError

    with pytest.raises(ToolError):
        provision_tools.tool_install(ctx, "x; rm -rf /", "r")
    with pytest.raises(ToolError):
        provision_tools.tool_install(ctx, "have", "")  # a reason is required: it goes to the decision log
    ctx.kernel.config = {"provision": {"auto": False}}
    assert "disabled" in provision_tools.tool_install(ctx, "zz-nothing", "r")


@posix_only
def test_tool_install_decision_events_carry_task_and_why(home, tmp_path, monkeypatch):
    from artel.plugins import provision_tools

    fake_micromamba(home)
    events = []
    project = SimpleNamespace(root=tmp_path, shared=tmp_path, log=SimpleNamespace(append=lambda k, **p: events.append((k, p))))
    ctx = SimpleNamespace(project=project, role="tester", actor="tester", task={"id": "t-9"}, kernel=SimpleNamespace(config={}))
    path = provision_tools.tool_install(ctx, "pdftk", "split pdf for test")
    assert path.endswith("pdftk")
    [(kind, ev)] = events
    assert kind == "provision/install" and ev["task_id"] == "t-9" and ev["why"] == "split pdf for test" and ev["actor"] == "tester"


def test_agent_shell_path_includes_tools_dirs(home, monkeypatch):
    from artel.agent_env import agent_env

    tools.bin_dir().mkdir(parents=True)
    env = agent_env("native")
    assert env["PATH"].split(os.pathsep)[0] == str(tools.bin_dir())
    assert env["PATH"].split(os.pathsep).count(str(tools.bin_dir())) == 1
