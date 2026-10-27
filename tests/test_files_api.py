"""File viewer API: safe paths, symlinks, preview kinds and size cut-off, the sandboxed HTML route, the user-mode
payloads of the project API."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from artel.httpd import LocalHTTPServer
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.web.server import Runner, make_handler

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return Project.create("goal", deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}), home=tmp_path / "home")


@pytest.fixture
def base(project):
    runner = Runner(deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}))
    httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(runner))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/projects/{project.slug}"
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def jget(url):
    code, h, body = get(url)
    return code, json.loads(body)


def test_tree_lists_files_with_sizes_kinds_and_collapses_noise(project, base):
    s = project.shared
    (s / "docs").mkdir()
    (s / "docs" / "a.md").write_text("# hi")
    (s / "app.py").write_text("print(1)\n")
    (s / "node_modules" / "x").mkdir(parents=True)
    (s / "node_modules" / "x" / "i.js").write_text("1")
    (s / ".git").mkdir()
    (s / ".git" / "HEAD").write_text("ref")
    code, r = jget(base + "/tree")
    assert code == 200
    by = {i["path"]: i for i in r["items"]}
    assert by["docs/a.md"]["kind"] == "markdown" and by["docs/a.md"]["size"] == 4
    assert by["app.py"]["kind"] == "code" and by["app.py"]["dir"] is False
    assert by["node_modules"]["collapsed"] is True and by[".git"]["collapsed"] is True
    assert not any(p.startswith("node_modules/") or p.startswith(".git/") for p in by)


def test_tree_marks_files_changed_by_the_last_task(project, base):
    s = project.shared
    old = s / "old.txt"
    old.write_text("x")
    os.utime(old, (1_000_000, 1_000_000))
    tid = project.board.add_task("T", "b")
    project.board.update(tid, status="running")
    time.sleep(0.05)
    (s / "new.txt").write_text("y")
    by = {i["path"]: i for i in jget(base + "/tree")[1]["items"]}
    assert by["new.txt"]["changed"] is True and by["old.txt"]["changed"] is False


@pytest.mark.parametrize("path", ["../project.json", "..%2Fproject.json", "/etc/passwd", "a/../../project.json",
                                  "%2Fetc%2Fpasswd"])
def test_file_routes_refuse_traversal(project, base, path):
    for route in ("file", "raw"):
        code, _, body = get(f"{base}/{route}?path={path}")
        assert code in (400, 404), (route, path, code)
        assert b"events" not in body and b"root:" not in body
    code, _, _ = get(f"{base}/preview/{path}")
    assert code in (400, 404)


@posix_only
def test_symlink_outside_shared_is_refused_everywhere(project, base, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    (project.shared / "link.txt").symlink_to(secret)
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "f.txt").write_text("TOPSECRET")
    (project.shared / "dirlink").symlink_to(outside)
    (project.shared / "ok.txt").write_text("fine")
    assert get(base + "/file?path=link.txt")[0] in (400, 404)
    assert get(base + "/raw?path=link.txt")[0] in (400, 404)
    assert get(base + "/preview/link.txt")[0] in (400, 404)
    assert get(base + "/file?path=dirlink/f.txt")[0] in (400, 404)
    paths = [i["path"] for i in jget(base + "/tree")[1]["items"]]
    assert "ok.txt" in paths and "link.txt" not in paths and not any(p.startswith("dirlink") for p in paths)


@posix_only
def test_symlink_inside_shared_is_fine(project, base):
    (project.shared / "real.txt").write_text("hello")
    (project.shared / "alias.txt").symlink_to(project.shared / "real.txt")
    assert jget(base + "/file?path=alias.txt")[1]["text"] == "hello"


def test_preview_kinds(project, base):
    s = project.shared
    (s / "a.md").write_text("# T")
    (s / "p.html").write_text("<p>x</p>")
    (s / "c.py").write_text("x = 1\n")
    (s / "d.json").write_text('{"a": [1, 2]}')
    (s / "i.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    (s / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    (s / "v.mp4").write_bytes(b"\0\0\0\x18ftypmp42")
    (s / "bin.dat").write_bytes(b"\0\1\2\3" * 10)
    want = {"a.md": "markdown", "p.html": "html", "c.py": "code", "d.json": "json", "i.png": "image",
            "doc.pdf": "pdf", "v.mp4": "video", "bin.dat": "binary"}
    for name, kind in want.items():
        code, r = jget(f"{base}/file?path={name}")
        assert code == 200 and r["kind"] == kind, (name, r)
    assert jget(base + "/file?path=c.py")[1]["lang"] == "py"
    assert jget(base + "/file?path=a.md")[1]["text"] == "# T"
    assert "text" not in jget(base + "/file?path=bin.dat")[1]


def test_preview_cuts_off_above_two_megabytes(project, base):
    (project.shared / "big.txt").write_text("a" * (2 * 1024 * 1024 + 1))
    (project.shared / "edge.txt").write_text("a" * (2 * 1024 * 1024))
    big = jget(base + "/file?path=big.txt")[1]
    assert big["kind"] == "toolarge" and "text" not in big and big["size"] == 2 * 1024 * 1024 + 1
    assert jget(base + "/file?path=edge.txt")[1]["kind"] == "text"


def test_csv_preview_is_parsed_and_capped_to_1000_rows(project, base):
    rows = ["name,qty"] + [f'item{i},"{i},5"' for i in range(1500)]
    (project.shared / "t.csv").write_text("\n".join(rows))
    r = jget(base + "/file?path=t.csv")[1]
    assert r["kind"] == "csv" and r["header"] == ["name", "qty"]
    assert len(r["rows"]) == 1000 and r["total_rows"] == 1500 and r["rows"][1] == ["item1", "1,5"]


def test_raw_serves_bytes_with_safe_headers_and_download(project, base):
    (project.shared / "p.html").write_text("<script>alert(1)</script>")
    (project.shared / "i.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    code, h, body = get(base + "/raw?path=p.html")
    assert code == 200 and h["Content-Type"].startswith("text/plain")  # never rendered on the dashboard origin
    assert h["X-Content-Type-Options"] == "nosniff" and "sandbox" in h["Content-Security-Policy"]
    code, h, body = get(base + "/raw?path=i.png&download=1")
    assert h["Content-Type"] == "image/png" and 'attachment; filename="i.png"' in h["Content-Disposition"]
    assert body == b"\x89PNG\r\n\x1a\n"


def test_html_route_is_sandboxed_and_resolves_relative_files(project, base):
    (project.shared / "site").mkdir()
    (project.shared / "site" / "index.html").write_text('<link rel="stylesheet" href="s.css"><b>hi</b>')
    (project.shared / "site" / "s.css").write_text("b{color:red}")
    code, h, body = get(base + "/preview/site/index.html")
    assert code == 200 and b"<b>hi</b>" in body and h["Content-Type"].startswith("text/html")
    assert h["Content-Security-Policy"] == "sandbox allow-scripts"
    assert h["X-Content-Type-Options"] == "nosniff"
    code, h, body = get(base + "/preview/site/s.css")
    assert code == 200 and h["Content-Type"].startswith("text/css") and h["Content-Security-Policy"] == "sandbox allow-scripts"


def test_unknown_project_and_missing_file(project, base):
    assert get(base + "/file?path=nope.txt")[0] == 404
    assert get(base + "/file?path=")[0] in (400, 404)  # a directory is not a file
    assert get(base.rsplit("/", 1)[0] + "/ghost/tree")[0] == 404


# --- user mode payloads ---------------------------------------------------------------------------------------
def blocked_project(project):
    b = project.board
    wid = b.add_task("Research and outline", "body", assignee="Ada")
    b.update(wid, status="running")
    b.comment(wid, "Ada", "BLOCKED: Нужен пароль от банка\nEvidence:\nls: denied")
    b.update(wid, status="blocked", needs_human=1)
    b.comment(wid, "reviewer", "ACCEPTED: All criteria verified against the files.")
    return wid


def test_project_api_user_mode_is_russian_and_carries_question_and_happenings(project, base):
    wid = blocked_project(project)
    raw = jget(base)[1]
    att = raw["stats"]["attention"][0]
    assert att["question"] == "Нужен пароль от банка"  # the question is there in dev mode too
    assert raw["board"]["tasks"][0]["title"] == "Research and outline"
    assert any("Нужен ваш ответ" in h["text"] for h in raw["happenings"])
    user = jget(base + "?mode=user")[1]
    t = [x for x in user["board"]["tasks"] if x["id"] == wid][0]
    assert t["title"] == "Исследование и план"
    assert t["comments"][-1]["body"].startswith("Принято ревьюером: ")
    assert "«Исследование и план»" in " ".join(h["text"] for h in user["happenings"])
    assert user["stats"]["attention"][0]["title"] == "Исследование и план"


def test_portfolio_user_mode_localizes_cards(project, base):
    blocked_project(project)
    url = base.split("/api/")[0] + "/api/portfolio"
    raw = json.loads(get(url)[2])[0]
    user = json.loads(get(url + "?mode=user")[2])[0]
    assert raw["attention"][0]["title"] == "Research and outline"
    assert user["attention"][0]["title"] == "Исследование и план"
    assert user["attention"][0]["question"] == "Нужен пароль от банка"
