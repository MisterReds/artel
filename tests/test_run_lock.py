"""Cross-process run ownership: a project's manager runs in one process at a time, and every process sees who
runs it.

The owner holds an exclusive OS lock on ``<project>/.run.lock`` for as long as its manager runs; the OS drops the lock
when that process dies. The dashboard of another process reports such a project as running and refuses to start a
second manager on it; the CLI refuses as well."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from artel.httpd import LocalHTTPServer
from artel.project import DEFAULT_CONFIG, Project, ProjectBusy, deep_merge
from artel.web.server import Runner, make_handler

REPO = Path(__file__).resolve().parents[1]
CFG = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})

HOLDER = """
import sys, time
from pathlib import Path
from artel.project import DEFAULT_CONFIG, Project
project = Project(Path(sys.argv[1]), DEFAULT_CONFIG)
project.claim_run(sys.argv[2])
print("held", flush=True)
time.sleep(600)
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return Project.create("goal", CFG, home=tmp_path / "home")


def spawn_holder(project, kind="acceptance"):
    """Another OS process that runs the project (holds its run lock) until killed."""
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(project.root), kind], stdout=subprocess.PIPE,
                             env=env, text=True)
    assert child.stdout.readline().strip() == "held"
    return child


def kill(child):
    child.kill()
    child.wait(timeout=30)
    child.stdout.close()


@pytest.fixture
def holder(project):
    child = spawn_holder(project)
    yield child
    if child.poll() is None:
        kill(child)


def test_another_process_running_the_project_is_seen_with_its_owner_and_blocks_a_second_run(project, holder):
    owner = project.running_elsewhere()
    assert owner and owner["pid"] == holder.pid and owner["kind"] == "acceptance"
    with pytest.raises(ProjectBusy) as e:
        project.claim_run("cli")
    assert e.value.owner["pid"] == holder.pid and f"pid {holder.pid}" in str(e.value)
    kill(holder)
    assert project.running_elsewhere() is None
    assert project.claim_run("cli") is True
    project.release_run()


def test_the_owning_process_does_not_see_its_own_run_as_elsewhere(project):
    assert project.running_elsewhere() is None
    assert project.claim_run("web") is True
    try:
        assert project.claim_run("web") is False  # the same object already holds it
        other = Project(project.root, CFG)  # a second object in this process: still this process's run
        assert other.running_elsewhere() is None
        assert other.run_owner()["pid"] == os.getpid()
    finally:
        project.release_run()
    assert project.run_owner() is None


def test_manager_refuses_to_run_a_project_another_process_runs(project, holder):
    from artel.manager import Manager, new_kernel

    kernel = new_kernel(CFG)
    try:
        with pytest.raises(ProjectBusy):
            Manager(kernel, project).run()
    finally:
        kernel.shutdown(reason="test")
    assert project.meta["status"] != "running"
    assert not [e for e in project.log.read() if e["kind"].startswith("manager/")]


def test_cli_resume_fails_clearly_while_another_process_runs_the_project(project, holder, capsys):
    from artel import cli

    code = cli.main(["resume", project.slug])
    assert code not in (0, None)
    assert f"pid {holder.pid}" in capsys.readouterr().err
    assert not [e for e in project.log.read() if e["kind"] == "owner/resume"]


# --- the dashboard ------------------------------------------------------------------------------------------------
@pytest.fixture
def served(project, monkeypatch):
    runner = Runner(CFG)
    driven = []
    monkeypatch.setattr(runner, "_drive", lambda proj, stop: driven.append(proj.slug))
    httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(runner))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api", driven
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET"):
    req = urllib.request.Request(url, data=b"{}" if method == "POST" else None, method=method,  # noqa: S310
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - a local test server
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_dashboard_shows_a_run_of_another_process_as_live_and_refuses_to_resume_it(project, holder, served):
    api, driven = served
    code, view = call(f"{api}/projects/{project.slug}")
    assert code == 200 and view["live"] is True
    assert view["live_elsewhere"]["pid"] == holder.pid and view["live_elsewhere"]["kind"] == "acceptance"
    _, ps = call(f"{api}/portfolio?mode=user")
    entry = next(p for p in ps if p["slug"] == project.slug)
    assert entry["live"] is True and entry["live_elsewhere"]["pid"] == holder.pid
    _, ps = call(f"{api}/projects")
    entry = next(p for p in ps if p["slug"] == project.slug)
    assert entry["live"] is True and entry["live_elsewhere"]["pid"] == holder.pid

    code, out = call(f"{api}/projects/{project.slug}/resume", "POST")
    assert code == 409
    assert out["error"] == f"Проект уже выполняется в другом процессе (pid {holder.pid})"
    time.sleep(0.2)
    assert driven == []
    assert not [e for e in project.log.read() if e["kind"] == "owner/resume"]

    kill(holder)
    code, view = call(f"{api}/projects/{project.slug}")
    assert view["live"] is False and view["live_elsewhere"] is None
    code, out = call(f"{api}/projects/{project.slug}/resume", "POST")
    assert code == 200
    time.sleep(0.3)
    assert driven == [project.slug]


def test_a_run_of_the_dashboard_process_outside_its_runner_is_live_and_not_resumed_twice(project, served):
    """`artel run --web`: the CLI's manager and the dashboard share a process; the runner did not start the run."""
    api, driven = served
    cli_run = Project(project.root, CFG)
    cli_run.claim_run("run")
    try:
        _, view = call(f"{api}/projects/{project.slug}")
        assert view["live"] is True and view["live_elsewhere"] is None
        code, out = call(f"{api}/projects/{project.slug}/resume", "POST")
        assert code == 409 and out["error"].startswith("Проект уже выполняется") and driven == []
    finally:
        cli_run.release_run()
