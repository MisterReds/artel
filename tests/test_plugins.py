"""Plugin packages (install from a path / git / pip, enable, disable, remove, update) and lifecycle events."""

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from artel import packages as P
from artel.kernel import Kernel, build_kernel
from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge

HELLO = Path(__file__).resolve().parent.parent / "examples" / "artel-plugin-hello"
LINE = "Hello from artel-plugin-hello"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    yield deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    for mod in [m for m in sys.modules if m.startswith(("artel_plugin_hello", "toml_plug", "broken_plug"))]:
        sys.modules.pop(mod, None)


def _toml_pkg(root: Path, name: str, module: str, body: str, perms=("events",)) -> Path:
    root.mkdir(parents=True)
    (root / "artel-plugin.toml").write_text(f'name = "{name}"\nversion = "1.0"\n[plugins]\nmain = "{module}"\n')
    (root / f"{module}.py").write_text(
        f'PLUGIN = {{"name": "{name}", "version": "1.0", "permissions": {list(perms)!r}}}\n{body}\n')
    return root


def _capture_system(kernel, seen):
    def spy(ctx, next_):
        seen.append(ctx["system"])
        return next_()

    kernel.load_plugin("spy", types.SimpleNamespace(setup=lambda k: k.on("llm/request", spy), PLUGIN={}))


def _kinds(project):
    return [e["kind"] for e in project.log.read()]


def test_install_from_path_runs_no_code_and_shows_manifest(cfg):
    before = sorted(p.name for p in HELLO.rglob("*"))
    entry = P.install(str(HELLO))
    assert entry["name"] == "artel-plugin-hello" and entry["version"] == "0.1.0" and entry["kind"] == "path"
    assert entry["enabled"] is False and entry["entry_points"] == {"hello": "artel_plugin_hello"}
    m = entry["manifests"]["hello"]
    assert m["name"] == "hello" and m["permissions"] == ["system_prompt", "events"]
    assert "hook:before_agent_start" in m["provides"]
    assert "artel_plugin_hello" not in sys.modules            # installing never imports the package
    assert sorted(p.name for p in HELLO.rglob("*")) == before  # the source directory is not touched by the build
    reg = json.loads((P.registry_path()).read_text())
    assert reg["packages"]["artel-plugin-hello"]["source"] == str(HELLO)
    with pytest.raises(P.PackageError, match="уже установлен"):
        P.install(str(HELLO))


def test_hello_lifecycle_enable_run_disable_remove(cfg):
    P.install(str(HELLO))
    assert P.enable("artel-plugin-hello", config=cfg)["error"] is None   # test-loaded in a throwaway kernel
    assert "artel_plugin_hello" not in sys.modules

    k = new_kernel(cfg)
    assert "artel_plugin_hello" in k.records
    seen: list = []
    _capture_system(k, seen)
    project, status = run_goal("Hello plugin run", cfg, kernel=k)
    assert status == "done"
    kinds = _kinds(project)
    assert "hello/before_agent_start" in kinds and "hello/task_done" in kinds
    assert seen and all(LINE in s for s in seen)
    done = [e for e in project.log.read() if e["kind"] == "hello/task_done"]
    assert all(e["task_id"] != project.meta["root_task"] for e in done)
    k.shutdown()

    P.disable("artel-plugin-hello")
    k = new_kernel(cfg)
    assert "artel_plugin_hello" not in k.records
    seen.clear()
    _capture_system(k, seen)
    project, _ = run_goal("Without hello", cfg, kernel=k)
    assert not any(x.startswith("hello/") for x in _kinds(project)) and not any(LINE in s for s in seen)
    k.shutdown()

    path = Path(P.get("artel-plugin-hello")["path"])
    P.remove("artel-plugin-hello")
    assert not path.exists() and P.load_registry()["packages"] == {}


def test_live_kernel_enable_disable_and_update(cfg):
    P.install(str(HELLO))
    k = build_kernel(cfg)
    P.enable("artel-plugin-hello", kernel=k)
    assert "artel_plugin_hello" in k.records and k.hooks["before_agent_start"]
    entry = P.update("artel-plugin-hello", kernel=k)
    assert entry["enabled"] and "artel_plugin_hello" in k.records
    P.disable("artel-plugin-hello", kernel=k)
    assert "artel_plugin_hello" not in k.records and not k.hooks["before_agent_start"]
    assert "artel_plugin_hello" not in sys.modules


def test_install_from_git(cfg, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(HELLO, repo)
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    entry = P.install(f"git+file://{repo}")
    assert entry["kind"] == "git" and entry["revision"] and entry["entry_points"] == {"hello": "artel_plugin_hello"}


def test_classify_sources():
    assert P.classify("https://github.com/o/r")[0] == "git"
    assert P.classify("git+https://example.org/o/r.git@v1") == ("git", "https://example.org/o/r.git", "v1")
    assert P.classify("artel-plugin-x==1.2")[0] == "pip"


def test_install_from_pip_spec_uses_target_dir(cfg):
    calls = []

    def fake_pip(cmd, **kw):
        calls.append(cmd)
        target = Path(cmd[cmd.index("--target") + 1])
        info = target / "artel_plugin_x-2.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: artel-plugin-x\nVersion: 2.0\n\n")
        (info / "entry_points.txt").write_text("[artel.plugins]\nx = artel_plugin_x\n")
        (target / "artel_plugin_x.py").write_text('PLUGIN = {"name": "x", "permissions": ["network"]}\n'
                                                  'raise SystemExit("must never be imported by install")\n')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    entry = P.install("artel-plugin-x==2.0", runner=fake_pip)
    assert calls and calls[0][-1] == "artel-plugin-x==2.0" and "--target" in calls[0]
    assert entry["version"] == "2.0" and entry["permissions"] == ["network"]


def test_update_with_new_permissions_switches_off(cfg, tmp_path):
    src = _toml_pkg(tmp_path / "pkg", "toml-plug", "toml_plug", "def setup(k):\n    pass")
    P.install(str(src))
    assert P.enable("toml-plug", config=cfg)["enabled"]
    (src / "toml_plug.py").write_text('PLUGIN = {"name": "toml-plug", "permissions": ["events", "shell"]}\n'
                                      "def setup(k):\n    pass\n")
    entry = P.update("toml-plug")
    assert not entry["enabled"] and "shell" in entry["error"]


def test_builtins_can_be_switched_but_not_the_core(cfg):
    P.set_builtin("artel.plugins.evolve_tools", False)
    assert "artel.plugins.evolve_tools" not in build_kernel(cfg).records
    with pytest.raises(P.PackageError):
        P.set_builtin("artel.plugins.builtin_tools", False)
    P.set_builtin("artel.plugins.evolve_tools", True)
    rows = {r["id"]: r for r in P.overview(cfg, build_kernel(cfg))}
    assert rows["artel.plugins.evolve_tools"]["loaded"] and rows["artel.plugins.builtin_tools"]["core"]


def test_lifecycle_events_and_payloads(cfg):
    seen = []

    def setup(k):
        k.on("project_start", lambda **e: seen.append(("project_start", e["resumed"])))
        k.on("before_agent_start", lambda **e: seen.append(("before_agent_start", e["role"])))
        k.on("agent_end", lambda **e: seen.append(("agent_end", e["outcome"].status)))
        k.on("task_done", lambda **e: seen.append(("task_done", e["task"]["status"])))
        k.on("project_end", lambda **e: seen.append(("project_end", e["status"])))
        k.on("session_shutdown", lambda **e: seen.append(("session_shutdown", e["reason"])))

    k = new_kernel(cfg)
    k.load_plugin("observer", types.SimpleNamespace(setup=setup, PLUGIN={}))
    _, status = run_goal("Events", cfg, kernel=k)
    k.shutdown(reason="test")
    k.shutdown(reason="again")                                   # idempotent
    names = [n for n, _ in seen]
    assert status == "done" and names[0] == "project_start" and seen[0] == ("project_start", False)
    assert names[-2:] == ["project_end", "session_shutdown"] and seen[-1] == ("session_shutdown", "test")
    assert ("before_agent_start", "planner") in seen and ("task_done", "done") in seen
    assert names.index("before_agent_start") < names.index("agent_end") < names.index("task_done")
    assert not k.records                                         # shutdown unloaded every plugin


def test_cli_install_enable_list_remove(cfg, capsys):
    from artel.cli import main

    assert main(["plugins", "install", str(HELLO)]) == 0
    out = capsys.readouterr().out
    assert "system_prompt" in out and "provides" in out
    assert main(["plugins", "enable", "artel-plugin-hello", "--yes"]) == 0
    assert main(["plugins", "list"]) == 0
    assert "artel-plugin-hello" in capsys.readouterr().out
    assert main(["plugins", "remove", "artel-plugin-hello"]) == 0


def test_web_plugins_api(cfg):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from artel.web.server import Runner, make_handler

    runner = Runner(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runner))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())

    try:
        r = call("/api/plugins/install", {"source": str(HELLO)})
        assert r["name"] == "artel-plugin-hello" and not r["enabled"]
        call("/api/plugins/artel-plugin-hello/enable", {})
        rows = {p["id"]: p for p in call("/api/plugins")["plugins"]}
        assert rows["artel-plugin-hello"]["loaded"] and "artel_plugin_hello" in runner.kernel.records
        call("/api/plugins/artel-plugin-hello/disable", {})
        assert "artel_plugin_hello" not in runner.kernel.records
        call("/api/plugins/artel-plugin-hello/remove", {})
        assert "artel-plugin-hello" not in {p["id"] for p in call("/api/plugins")["plugins"]}
        bad = call("/api/plugins/install", {"source": "/no/such/dir"})
        assert bad.get("error")
    finally:
        httpd.shutdown()
        runner.kernel.shutdown()


def test_kernel_without_listeners_stays_fast():
    k = Kernel({})
    k.emit("before_agent_start", agent={})   # no listeners: a dict miss, nothing else
    assert not k.hooks.get("before_agent_start")
