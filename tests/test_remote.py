"""Remote dashboard: login, sessions, rate limit, hashed token; PWA shell files. No network beyond 127.0.0.1."""

import json
import os
import re
import threading
import urllib.error
import urllib.request
from artel.httpd import LocalHTTPServer
from pathlib import Path

import pytest

from artel.project import DEFAULT_CONFIG, deep_merge
from artel.web import auth as authmod
from artel.web.auth import COOKIE, Auth
from artel.web.server import Runner, make_handler

STATIC = Path(__file__).parent.parent / "artel" / "web" / "static"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Start a dashboard; `remote=True` gives it an Auth (remote mode) with a controllable clock."""
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    started = []

    def start(remote: bool):
        runner = Runner(deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}))
        clock = Clock()
        auth = Auth(home=tmp_path, session_days=30, clock=clock) if remote else None
        token = auth.rotate() if auth else None
        httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(runner, auth))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", auth, token, clock

    yield start
    for h in started:
        h.shutdown()
        h.server_close()


def req(url, method="GET", body=None, cookie=None, headers=None):
    h = dict(headers or {})
    if body is not None or method == "POST":
        h.setdefault("Content-Type", "application/json")
    if cookie:
        h["Cookie"] = f"{COOKIE}={cookie}"
    r = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, h, method=method)
    try:
        resp = OPENER.open(r, timeout=5)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    if "event-stream" in resp.headers.get("Content-Type", ""):
        return resp.status, dict(resp.headers), resp  # the stream stays open; the caller closes it
    return resp.status, dict(resp.headers), resp.read()


def login(base, token, headers=None):
    code, h, body = req(base + "/api/auth/login", "POST", {"token": token}, headers=headers)
    sid = None
    m = re.search(rf"{COOKIE}=([^;]+)", h.get("Set-Cookie", ""))
    if m:
        sid = m.group(1)
    return code, h, sid


def test_remote_mode_requires_login_for_api_sse_and_pages(server):
    base, _, _, _ = server(True)
    for path in ["/api/portfolio", "/api/providers", "/api/projects/x/events?after=0", "/api/update"]:
        code, _, body = req(base + path)
        assert code == 401 and json.loads(body)["error"] == "login required", path
    code, _, _ = req(base + "/api/projects", "POST", {"goal": "do something"})
    assert code == 401
    code, h, _ = req(base + "/")
    assert code == 302 and h["Location"] == "/login"
    code, h, body = req(base + "/login")
    assert code == 200 and "text/html" in h["Content-Type"] and "Токен доступа" in body.decode()
    st = json.loads(req(base + "/api/auth/status")[2])
    assert st["remote"] and not st["authed"]


def test_login_session_cookie_gives_access_and_streams(server):
    base, auth, token, _ = server(True)
    code, h, sid = login(base, token)
    assert code == 200 and sid
    cookie = h["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert f"Max-Age={30 * 86400}" in cookie and "Secure" not in cookie  # plain http on localhost
    assert req(base + "/api/portfolio", cookie=sid)[0] == 200
    assert req(base + "/", cookie=sid)[0] == 200
    code, _, _ = req(base + "/api/projects/nope/events?after=0", cookie=sid)
    # a missing project is a 404 after the gate, not a 401
    assert code == 404
    # behind an HTTPS proxy the cookie is marked Secure
    _, h2, _ = login(base, token, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in h2["Set-Cookie"]
    # logout revokes this session
    req(base + "/api/auth/logout", "POST", {}, cookie=sid)
    assert req(base + "/api/portfolio", cookie=sid)[0] == 401


def test_sse_stream_needs_and_honours_a_session(server, tmp_path):
    from artel.project import Project

    base, auth, token, _ = server(True)
    project = Project.create("stream test", deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}), name="stream")
    project.log.append("note", text="hello")
    url = f"{base}/api/projects/{project.slug}/events?after=0"
    assert req(url)[0] == 401
    _, _, sid = login(base, token)
    code, h, stream = req(url, cookie=sid)
    try:
        assert code == 200 and h["Content-Type"] == "text/event-stream"
        assert b"data:" in stream.readline() + stream.readline()
    finally:
        stream.close()


def test_wrong_token_rejected_and_rate_limited(server):
    base, auth, token, clock = server(True)
    for _ in range(authmod.MAX_FAILS):
        code, _, sid = login(base, "not-the-token")
        assert code == 401 and sid is None
    code, h, sid = login(base, token)  # even the right token waits out the lockout
    assert code == 429 and sid is None and int(h["Retry-After"]) > 0
    # another address (a client behind the local reverse proxy) is not locked out
    code, _, sid = login(base, token, headers={"X-Forwarded-For": "203.0.113.7"})
    assert code == 200 and sid
    clock.t += authmod.FAIL_WINDOW_S + 1
    code, _, sid = login(base, token)
    assert code == 200 and sid


def test_session_expires_and_token_rotation_revokes(server):
    base, auth, token, clock = server(True)
    _, _, sid = login(base, token)
    clock.t += 29 * 86400
    assert req(base + "/api/portfolio", cookie=sid)[0] == 200
    clock.t += 2 * 86400
    assert req(base + "/api/portfolio", cookie=sid)[0] == 401
    _, _, sid2 = login(base, token)
    assert req(base + "/api/portfolio", cookie=sid2)[0] == 200
    new = Auth(home=auth.path.parent).rotate()  # e.g. `artel web token --new` from another process
    assert req(base + "/api/portfolio", cookie=sid2)[0] == 401
    assert login(base, token)[0] == 401 and login(base, new)[0] == 200
    assert req(base + "/api/portfolio", cookie="forged-session-id")[0] == 401


def test_token_and_sessions_stored_hashed_only(tmp_path):
    a = Auth(home=tmp_path)
    token = a.ensure_token()
    assert token and len(token) >= 32 and a.ensure_token() is None  # shown once
    sid = a.login(token, "127.0.0.1")
    raw = (tmp_path / "web_auth.json").read_text()
    assert token not in raw and sid not in raw
    rec = json.loads(raw)["token"]
    assert rec["algo"] == "scrypt" and len(bytes.fromhex(rec["salt"])) == 16 and len(rec["hash"]) == 64
    if os.name != "nt":  # Windows has no group/other mode bits to check
        assert (tmp_path / "web_auth.json").stat().st_mode & 0o077 == 0
    # salted: the same token hashes differently in another install
    b = Auth(home=tmp_path / "other")
    b.rotate()
    assert json.loads((tmp_path / "other" / "web_auth.json").read_text())["token"]["hash"] != rec["hash"]
    assert a.check_token(token) and not a.check_token(token + "x") and not a.check_token("")


def test_localhost_mode_unchanged_without_login(server, tmp_path):
    base, _, _, _ = server(False)
    assert req(base + "/api/portfolio")[0] == 200
    code, h, body = req(base + "/")
    assert code == 200 and b"<title>Artel</title>" in body
    assert req(base + "/login")[0] == 302  # nothing to log into
    st = json.loads(req(base + "/api/auth/status")[2])
    assert st == {"remote": False, "authed": True, "secure": False, "insecure": False}
    assert not (tmp_path / "web_auth.json").exists()


def test_local_serve_refuses_a_public_bind_without_remote_mode(monkeypatch, tmp_path):
    from artel.web import server as srv

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="--remote"):
        srv.serve(port=0, config=deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}), host="0.0.0.0")


def test_insecure_banner_flag(server):
    base, _, token, _ = server(True)
    st = lambda h: json.loads(req(base + "/api/auth/status", headers=h)[2])  # noqa: E731
    assert not st({})["insecure"]  # localhost itself
    assert st({"X-Forwarded-For": "198.51.100.4"})["insecure"]  # a phone via a plain-http proxy
    assert not st({"X-Forwarded-For": "198.51.100.4", "X-Forwarded-Proto": "https"})["insecure"]


def test_pwa_files_served_with_correct_types(server):
    base, _, _, _ = server(True)  # public even in remote mode: the login page installs the app too
    code, h, body = req(base + "/manifest.webmanifest")
    assert code == 200 and h["Content-Type"] == "application/manifest+json"
    m = json.loads(body)
    assert m["name"] == m["short_name"] == "Artel" and m["display"] == "standalone"
    assert m["theme_color"].upper() == "#B5522B" and m["background_color"].upper() == "#F7F4EE"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in m["icons"]:
        code, h, data = req(base + icon["src"])
        assert code == 200 and h["Content-Type"] == icon["type"], icon["src"]
        if icon["type"] == "image/png":
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            w, hh = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            assert f"{w}x{hh}" == icon["sizes"]
    code, h, body = req(base + "/sw.js")
    assert code == 200 and h["Content-Type"].startswith("text/javascript")
    assert h["Service-Worker-Allowed"] == "/" and h["Cache-Control"] == "no-cache"
    assert req(base + "/static/icons/apple-touch-icon.png")[0] == 200


def test_service_worker_caches_only_the_shell_never_api():
    sw = (STATIC / "sw.js").read_text()
    assets = re.search(r"const ASSETS = \[(.*?)\];", sw, re.S).group(1)
    listed = re.findall(r"'([^']+)'", assets)
    assert not [a for a in listed if a.startswith("/api") or a == "/login"]
    for a in listed:  # every precached file exists
        if a.startswith("/static/"):
            assert (STATIC / a[len("/static/"):]).is_file(), a
    # the fetch handler bails out on /api/ before any respondWith / cache.put
    never = re.search(r"function neverCache\(url, req\) \{(.*?)\n\}", sw, re.S).group(1)
    assert "url.pathname.startsWith('/api/')" in never and "req.method !== 'GET'" in never
    handler = sw[sw.index("addEventListener('fetch'"):]
    assert handler.index("if (neverCache(url, req)) return;") < handler.index("respondWith")


def test_index_links_manifest_icons_and_registers_sw():
    html = (STATIC.parent / "index.html").read_text()
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon"' in html and '<meta name="theme-color"' in html
    assert "serviceWorker.register('/sw.js')" in html
    assert "status===401" in html  # a lost session goes back to the login page


def test_other_sites_cannot_drive_the_local_dashboard(server):
    """Any page the user visits can POST to 127.0.0.1 (form, no-cors fetch) or re-point its own domain there (DNS
    rebinding); the dashboard must refuse both, in local mode where there is no login."""
    base, _, _, _ = server(False)
    plain = {"Content-Type": "text/plain"}
    assert req(base + "/api/projects", "POST", {"goal": "x"}, headers=plain)[0] == 403  # simple request, no preflight
    evil = {"Origin": "https://evil.example"}
    code, _, body = req(base + "/api/projects", "POST", {"goal": "x"}, headers=evil)
    assert code == 403 and "cross-origin" in json.loads(body)["error"]
    assert req(base + "/api/portfolio", headers={"Host": "rebind.evil.example:8765"})[0] == 421
    port = base.rsplit(":", 1)[1]
    assert req(base + "/api/portfolio", headers={"Host": f"localhost:{port}"})[0] == 200
    same = {"Origin": base}
    assert req(base + "/api/providers/timezone", "POST", {"timezone": "UTC"}, headers=same)[0] == 200
