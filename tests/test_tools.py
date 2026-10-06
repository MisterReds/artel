import os
import stat
from pathlib import Path

import pytest

from artel import tools


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    # a stand-in npm: `npm install -g --prefix P pkg` creates P/bin/<cli>; `npm uninstall` removes it
    fake = tmp_path / "fakebin"
    fake.mkdir()
    npm = fake / "npm"
    npm.write_text("""#!/usr/bin/env python3
import sys, os
from pathlib import Path
a = sys.argv[1:]
prefix = Path(a[a.index('--prefix') + 1])
pkg = a[-1]
name = {'@openai/codex': 'codex', '@anthropic-ai/claude-code': 'claude'}[pkg]
b = prefix / 'bin' / name
if a[0] == 'install':
    b.parent.mkdir(parents=True, exist_ok=True); b.write_text('#!/bin/sh\\necho fake ' + name + '\\n'); b.chmod(0o755)
else:
    b.unlink()
""")
    npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")  # no real codex / claude / npm on PATH
    return tmp_path


def test_cli_installed_on_demand_into_artel_tools_and_removed(home):
    assert tools.resolve("codex") is None
    log = []
    res = tools.install("codex", log=log.append)
    assert res["ok"] and res["path"].startswith(str(home / "home" / "tools"))
    assert tools.resolve("codex") == res["path"] and tools.installed_by_artel("codex")
    assert str(home / "home" / "tools" / "npm" / "bin") in tools.env_with_tools()["PATH"].split(os.pathsep)[0]
    assert tools.resolve("claude") is None  # only what was asked for
    assert tools.uninstall("codex")["ok"] and tools.resolve("codex") is None


def test_system_cli_is_used_not_reinstalled(home):
    sysbin = home / "fakebin" / "codex"
    sysbin.write_text("#!/bin/sh\n")
    sysbin.chmod(0o755)
    res = tools.install("codex", log=lambda m: None)
    assert res["already"] and res["path"] == str(sysbin)
    assert not tools.uninstall("codex")["ok"]  # never removes what Artel did not install


def test_node_asset_picks_latest_lts_for_platform():
    index = [{"version": "v25.0.0", "lts": False, "files": ["linux-x64"]},
             {"version": "v24.9.0", "lts": "Krypton", "files": ["linux-x64", "osx-arm64-tar", "win-x64-zip"]}]
    assert tools.node_asset(index, "linux", "x86_64") == ("v24.9.0", "node-v24.9.0-linux-x64.tar.gz")
    assert tools.node_asset(index, "darwin", "arm64") == ("v24.9.0", "node-v24.9.0-darwin-arm64.tar.gz")
    assert tools.node_asset(index, "win32", "AMD64") == ("v24.9.0", "node-v24.9.0-win-x64.zip")


def test_codex_runtime_explains_how_to_install_when_missing(home):
    from artel.manager import new_kernel, run_goal
    from artel.project import DEFAULT_CONFIG, deep_merge
    from artel.runtimes import run_agent
    from artel.tooling import ToolContext

    base = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("x", base)
    cfg = deep_merge(base, {"runtime_by_role": {"worker": "codex"}})
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared, task={"id": "t"})
    out = run_agent(k, project, role="worker", system="s", first_user="u", ctx=ctx, max_turns=5, purpose="p")
    assert out.status == "error" and "artel providers install codex" in out.error
