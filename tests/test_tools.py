import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from artel import tools


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    if sys.platform != "win32":  # Windows keeps PATH: git and the system DLL directories live on it
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(tools, "resolve", lambda b, _r=tools.resolve: _r(b) if ("/" in b or os.sep in b) else
                        (str(tools.bin_dir() / (b + tools.EXE)) if (tools.bin_dir() / (b + tools.EXE)).exists() else None))
    return tmp_path


def _codex_archive() -> bytes:
    """A release archive in this platform's format (.zip on Windows, .tar.gz elsewhere)."""
    asset = tools.codex_asset()
    buf = io.BytesIO()
    if asset.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(asset.removesuffix(".zip"), b"MZ fake codex")
        return buf.getvalue()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"#!/bin/sh\necho codex-cli 9.9\n"
        info = tarfile.TarInfo(tools.codex_asset().removesuffix(".tar.gz"))
        info.size, info.mode = len(data), 0o755
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_platform_assets():
    assert tools.codex_asset("linux", "x86_64") == "codex-x86_64-unknown-linux-musl.tar.gz"
    assert tools.codex_asset("darwin", "arm64") == "codex-aarch64-apple-darwin.tar.gz"
    assert tools.codex_asset("win32", "AMD64") == "codex-x86_64-pc-windows-msvc.exe.zip"
    assert tools.claude_platform("linux", "aarch64", musl=False) == "linux-arm64"
    assert tools.claude_platform("linux", "x86_64", musl=True) == "linux-x64-musl"
    assert tools.claude_platform("darwin", "arm64") == "darwin-arm64"
    assert tools.claude_platform("win32", "AMD64") == "win32-x64"


def test_codex_installed_from_release_binary_and_removed(home, monkeypatch):
    monkeypatch.setattr(tools, "_download", lambda url, dest: Path(dest).write_bytes(_codex_archive()))
    res = tools.install("codex", log=lambda m: None)
    assert res["ok"] and Path(res["path"]) == tools.bin_dir() / ("codex" + tools.EXE) and tools.installed_by_artel("codex")
    assert str(tools.bin_dir()) == tools.env_with_tools()["PATH"].split(os.pathsep)[0]
    assert not tools.installed_by_artel("claude")  # only what was asked for
    assert tools.uninstall("codex")["ok"] and not tools.installed_by_artel("codex")


def test_claude_binary_must_match_manifest_checksum(home, monkeypatch):
    payload = b"claude native build"
    plat = tools.claude_platform()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout=0):
        if url.endswith("/latest"):
            return Resp(b"2.9.9\n")
        return Resp(json.dumps({"platforms": {plat: {"checksum": hashlib.sha256(payload).hexdigest()}}}).encode())

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(tools, "_download", lambda url, dest: Path(dest).write_bytes(b"tampered"))
    with pytest.raises(RuntimeError, match="контрольная сумма"):
        tools.install("claude", log=lambda m: None)
    assert not tools.installed_by_artel("claude")
    monkeypatch.setattr(tools, "_download", lambda url, dest: Path(dest).write_bytes(payload))
    assert tools.install("claude", log=lambda m: None)["ok"] and tools.installed_by_artel("claude")


def test_system_cli_is_used_not_reinstalled(home, monkeypatch):
    monkeypatch.setattr(tools, "resolve", lambda b: "/usr/local/bin/codex")
    res = tools.install("codex", log=lambda m: None)
    assert res["already"] and res["path"] == "/usr/local/bin/codex"
    assert not tools.uninstall("codex")["ok"]  # never removes what Artel did not install


def test_codex_runtime_explains_how_to_install_when_missing(home, monkeypatch):
    from artel.manager import new_kernel, run_goal
    from artel.project import DEFAULT_CONFIG, deep_merge
    from artel.runtimes import run_agent
    from artel.tooling import ToolContext

    base = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("x", base)
    cfg = deep_merge(base, {"runtime_by_role": {"worker": "codex"}, "codex": {"binary": "codex-missing"}})
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared, task={"id": "t"})
    out = run_agent(k, project, role="worker", system="s", first_user="u", ctx=ctx, max_turns=5, purpose="p")
    assert out.status == "error" and "artel providers install codex" in out.error


import subprocess as _sp
import sys as _sys
import time as _time

import pytest as _pytest


@_pytest.mark.skipif(_sys.platform == "win32", reason="process groups are POSIX")
def test_terminate_tree_kills_grandchildren_holding_stdout_and_stderr_is_drained():
    from artel.tools import StderrTail, group_popen_kwargs, terminate_tree

    code = ("import subprocess,sys,time\n"
            "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "print(g.pid,flush=True)\n"
            "sys.stderr.write('e'*1000000)\n"
            "time.sleep(60)\n")
    proc = _sp.Popen([_sys.executable, "-c", code], stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, **group_popen_kwargs())
    tail = StderrTail(proc, limit=1000)
    gpid = int(proc.stdout.readline())
    t0 = _time.time()
    assert terminate_tree(proc) is True
    assert proc.stdout.read() == ""  # the read loop ends: no one holds the pipe any more
    assert _time.time() - t0 < 8
    import os
    with _pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(gpid, 0)
            _time.sleep(0.05)
    assert len(tail.text()) <= 1000
