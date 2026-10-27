"""Run the result: spec heuristics, auditor's `run`, start/stop with a link, web API, folder/archive, CLI."""
import json
import os
import sys
import threading
import time
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from artel.httpd import LocalHTTPServer
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.result_run import ResultRunner, run_spec, validate_run
from artel.web import server as webserver
from artel.web.auth import Auth
from artel.web.server import Runner, make_handler

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups, symlinks and /proc checks are POSIX; "
                                                                "Windows kills the tree with taskkill instead")

SERVER = '''import http.server, os, subprocess, sys
port = int(os.environ["PORT"])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open("child.pid", "w").write(str(child.pid))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"hello")
    def log_message(self, *a): pass
print("Serving on http://0.0.0.0:%d" % port, flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    return Project.create("goal", DEFAULT_CONFIG, home=tmp_path)


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_gone(pid: int, t: float = 5.0) -> bool:
    end = time.time() + t
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def get(url: str) -> tuple:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


# --- spec ---------------------------------------------------------------------
def test_spec_none_when_empty(project):
    assert run_spec(project) is None


def test_spec_package_json_start(project):
    (project.shared / "package.json").write_text(json.dumps({"scripts": {"start": "node x.js"}}))
    s = run_spec(project)
    assert s["command"] == ["npm", "start"] and s["source"] == "heuristic" and s["kind"] == "web" and s["cwd"] == "."


@pytest.mark.parametrize("name", ["server.py", "app.py", "main.py"])
def test_spec_python_server_in_subdir(project, name):
    (project.shared / "site").mkdir()
    (project.shared / "site" / name).write_text("print(1)")
    s = run_spec(project)
    assert s["command"][1:] == [name] and s["cwd"] == "site"


def test_spec_manage_py(project):
    (project.shared / "manage.py").write_text("")
    assert run_spec(project)["command"][2:] == ["runserver", "127.0.0.1:{port}"]


def test_spec_readme_block(project):
    (project.shared / "README.md").write_text("Usage\n\n```bash\n$ pip install x\nuvicorn api:app --port {port}\n```\n")
    s = run_spec(project)
    assert s["command"][0] == "uvicorn" and s["source"] == "heuristic"


def test_spec_static_only_index(project):
    (project.shared / "index.html").write_text("<h1>hi</h1>")
    s = run_spec(project)
    assert s["kind"] == "static" and s["command"] == []


def test_spec_meta_wins(project):
    (project.shared / "index.html").write_text("x")
    project.set_meta(run={"command": "python3 serve.py --port {port}", "url_path": "/app"})
    s = run_spec(project)
    assert s["source"] == "meta" and s["command"] == ["python3", "serve.py", "--port", "{port}"]
    assert s["url_path"] == "/app"


def test_validate_run():
    assert validate_run(None) is None
    assert validate_run({"command": " x "}) == {"command": "x"}
    for bad in ({"command": ""}, {"command": 5}, {"command": "a" * 301}, "x"):
        with pytest.raises(ValueError):
            validate_run(bad)


def test_audit_verdict_records_run(project):
    from artel.plugins.board_tools import audit_verdict

    class Ctx:
        terminal = None

    c = Ctx()
    audit_verdict(c, True, "ok", run={"command": "python3 app.py", "url_path": "/"})
    assert c.terminal[1]["run"] == {"command": "python3 app.py", "url_path": "/"}
    with pytest.raises(Exception):
        audit_verdict(Ctx(), True, "ok", run={"command": ""})
    c2 = Ctx()
    audit_verdict(c2, True, "ok")
    assert "run" not in c2.terminal[1]


def test_manager_stores_run_on_complete(project):
    from artel.manager import Manager
    from types import SimpleNamespace

    out = SimpleNamespace(name="audit_verdict", payload={"complete": True, "summary": "ok", "missing": [],
                                                         "run": {"command": "python3 app.py"}})
    board = SimpleNamespace(update=lambda *a, **k: None, comment=lambda *a, **k: None)
    fake = SimpleNamespace(audit_failures=0, project=project, board=board, root_id="r")
    Manager.on_audit(fake, out)
    assert project.meta["run"] == {"command": "python3 app.py"}


# --- running ------------------------------------------------------------------
@posix_only
def test_start_stop_restart_and_proxy(project, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    (project.shared / "server.py").write_text(SERVER)
    r = ResultRunner(DEFAULT_CONFIG)
    try:
        info = r.start(project)
        assert info["url"] == f"http://127.0.0.1:{info['port']}"  # 0.0.0.0 in the log became 127.0.0.1
        # a proxy from the environment must not break the loopback request of the child's own check
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        assert opener.open(info["url"], timeout=5).read() == b"hello"
        st = r.status(project)
        assert st["running"] and st["state"] == "running" and "Serving on" in st["log"]
        assert not (project.shared / "run.log").exists() and (project.root / "run.log").exists()
        child = int((project.shared / "child.pid").read_text())
        second = r.start(project)  # start again = restart
        assert wait_gone(info["pid"]) and wait_gone(child)
        assert second["pid"] != info["pid"]
        child2 = int((project.shared / "child.pid").read_text())
        assert r.stop(project) is True
        assert wait_gone(second["pid"]) and wait_gone(child2)
        assert r.status(project)["running"] is False
    finally:
        r.stop_all()


def test_static_index_served(project):
    (project.shared / "index.html").write_text("<h1>static ok</h1>")
    r = ResultRunner(DEFAULT_CONFIG)
    try:
        info = r.start(project)
        code, body = get(info["url"] + "/")
        assert code == 200 and b"static ok" in body
        assert r.status(project)["state"] == "running"
        r.stop(project)
        with pytest.raises(OSError):
            get(info["url"] + "/")
    finally:
        r.stop_all()


def test_start_without_anything_to_run(project):
    with pytest.raises(ValueError):
        ResultRunner(DEFAULT_CONFIG).start(project)


@posix_only
def test_not_answering_reports_log_tail(project, monkeypatch):
    import artel.result_run as rr
    monkeypatch.setattr(rr, "READY_WAIT_S", 0.5)
    monkeypatch.setattr(rr, "URL_WAIT_S", 0.5)
    project.set_meta(run={"command": "python3 -c \"import time; print('booting', flush=True); time.sleep(30)\""})
    r = ResultRunner(DEFAULT_CONFIG)
    try:
        r.start(project)
        st = r.status(project)
        assert st["state"] == "not_answering" and "booting" in st["log"]
    finally:
        r.stop_all()


# --- web API ------------------------------------------------------------------
@pytest.fixture
def web(tmp_path, monkeypatch, project):
    started = []

    def start(remote=False):
        runner = Runner(deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}))
        auth = Auth(home=tmp_path, session_days=30) if remote else None
        token = auth.rotate() if auth else None
        httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(runner, auth))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append((httpd, runner))
        return f"http://127.0.0.1:{httpd.server_address[1]}", token

    yield start
    for h, runner in started:
        h.shutdown()
        h.server_close()
        runner.results.stop_all()


def call(url, method="GET", body=None, cookie=None):
    h = {"Content-Type": "application/json"} if method == "POST" else {}
    if cookie:
        h["Cookie"] = cookie
    r = urllib.request.Request(url, json.dumps(body or {}).encode() if method == "POST" else None, h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_api_run_start_status_stop(web, project):
    (project.shared / "index.html").write_text("<p>api</p>")
    base, _ = web()
    u = f"{base}/api/projects/{project.slug}/run"
    code, _, body = call(u, "POST", {"action": "start"})
    st = json.loads(body)
    assert code == 200 and st["running"] and st["url"]
    assert b"api" in get(st["url"])[1]
    assert json.loads(call(u)[2])["running"] is True
    assert json.loads(call(u, "POST", {"action": "stop"})[2])["running"] is False
    assert call(u, "POST", {"action": "bogus"})[0] == 400
    assert json.loads(call(f"{base}/api/projects/{project.slug}")[2])["remote"] is False


def test_api_run_refuses_forged_post(web, project):
    base, _ = web()
    r = urllib.request.Request(f"{base}/api/projects/{project.slug}/run", b'{"action":"start"}',
                               {"Content-Type": "text/plain"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 403


def test_api_run_needs_login_in_remote_mode(web, project):
    base, _ = web(remote=True)
    for method in ("GET", "POST"):
        for tail in ("run", "archive", "open-folder"):
            if method == "GET" and tail == "open-folder" or method == "POST" and tail == "archive":
                continue
            code, _, _ = call(f"{base}/api/projects/{project.slug}/{tail}", method)
            assert code == 401, (method, tail)


def test_open_folder_local_calls_opener(web, project, monkeypatch):
    seen = []
    monkeypatch.setattr(webserver, "open_folder", lambda p: seen.append(p) or True)
    base, _ = web()
    code, _, body = call(f"{base}/api/projects/{project.slug}/open-folder", "POST")
    assert code == 200 and json.loads(body)["opened"] is True
    assert seen == [str(project.shared.resolve())]


def test_open_folder_refused_in_remote_mode(web, project, monkeypatch):
    from artel.web.auth import COOKIE

    seen = []
    monkeypatch.setattr(webserver, "open_folder", lambda p: seen.append(p) or True)
    base, token = web(remote=True)
    code, h, _ = call(f"{base}/api/auth/login", "POST", {"token": token})
    sid = h["Set-Cookie"].split(";")[0]
    assert sid.startswith(COOKIE)
    code, _, body = call(f"{base}/api/projects/{project.slug}/open-folder", "POST", cookie=sid)
    assert code == 403 and not seen
    assert json.loads(call(f"{base}/api/projects/{project.slug}", cookie=sid)[2])["remote"] is True


@posix_only
def test_archive_has_files_and_skips_outside_symlink(web, project, tmp_path):
    (project.shared / "a.txt").write_text("A")
    (project.shared / "sub").mkdir()
    (project.shared / "sub" / "b.txt").write_text("B")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("S")
    (project.shared / "leak.txt").symlink_to(secret)
    (project.shared / "leakdir").symlink_to(tmp_path)
    base, _ = web()
    code, h, body = call(f"{base}/api/projects/{project.slug}/archive")
    assert code == 200 and "attachment" in h["Content-Disposition"]
    names = set(zipfile.ZipFile(BytesIO(body)).namelist())
    assert {"a.txt", "sub/b.txt"} <= names and not any("leak" in n or "secret" in n for n in names)


# --- CLI ----------------------------------------------------------------------
def test_cli_parse_and_open(project, capsys, monkeypatch):
    from artel import cli

    monkeypatch.setattr(webserver, "open_folder", lambda p: False)
    assert cli.main(["result", "open", project.slug]) == 0
    assert str(project.shared.resolve()) in capsys.readouterr().out
    assert cli.main(["result", "stop", project.slug]) == 0
    assert "не запущен" in capsys.readouterr().out
    assert cli.main(["result", "run", project.slug]) == 1  # nothing to run
    with pytest.raises(SystemExit):
        cli.main(["result", "bogus", project.slug])
