"""`artel app`: finding a Chromium-family browser, starting/reusing the dashboard, shortcuts per OS."""
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from artel import cli, desktop


# ---------- browser discovery ----------

def fake_fs(*present):
    present = {str(p) for p in present}
    return dict(which=lambda n: f"/usr/bin/{n}" if f"/usr/bin/{n}" in present else None,
                exists=lambda p: str(p) in present)


def test_linux_finds_chromium_family_on_path_in_preference_order():
    fs = fake_fs("/usr/bin/chromium", "/usr/bin/brave-browser")
    assert desktop.find_browser("Linux", env={}, **fs) == "/usr/bin/chromium"
    fs = fake_fs("/usr/bin/yandex-browser")
    assert desktop.find_browser("Linux", env={}, **fs) == "/usr/bin/yandex-browser"


def test_linux_falls_back_to_known_install_paths():
    fs = fake_fs("/opt/google/chrome/chrome")
    assert desktop.find_browser("Linux", env={}, **fs) == "/opt/google/chrome/chrome"


def test_macos_looks_inside_app_bundles(tmp_path):
    edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    assert desktop.find_browser("Darwin", env={"HOME": str(tmp_path)}, **fake_fs(edge)) == edge
    mine = tmp_path / "Applications/Chromium.app/Contents/MacOS/Chromium"
    assert desktop.find_browser("Darwin", env={"HOME": str(tmp_path)}, **fake_fs(mine)) == str(mine)


def test_windows_searches_program_files_and_local_app_data():
    env = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    yandex = os.path.join(env["LOCALAPPDATA"], "Yandex", "YandexBrowser", "Application", "browser.exe")
    assert desktop.find_browser("Windows", env=env, **fake_fs(yandex)) == yandex
    edge = os.path.join(env["ProgramFiles(x86)"], "Microsoft", "Edge", "Application", "msedge.exe")
    assert desktop.find_browser("Windows", env=env, **fake_fs(edge, yandex)) == edge  # Edge before Yandex


def test_no_browser_gives_none():
    for system in ("Linux", "Darwin", "Windows"):
        assert desktop.find_browser(system, env={"HOME": "/h"}, **fake_fs()) is None


def test_app_command_uses_separate_profile_and_no_token(tmp_path):
    argv = desktop.app_command("/usr/bin/chromium", "http://127.0.0.1:8765/", tmp_path / "app-profile")
    assert argv[0] == "/usr/bin/chromium"
    assert "--app=http://127.0.0.1:8765/" in argv
    assert f"--user-data-dir={tmp_path / 'app-profile'}" in argv
    assert not any("token" in a.lower() for a in argv)


# ---------- the dashboard server ----------

class _Artel(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"name": "Artel"}).encode() if self.path == "/manifest.webmanifest" else b"{}"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_dashboard():
    started = []

    def start(handler=_Artel):
        srv = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv.server_address[1]

    yield start
    for s in started:
        s.shutdown()
        s.server_close()


def test_probe_recognises_artel_and_ignores_other_servers(fake_dashboard):
    class Other(_Artel):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>hello</html>")

    assert desktop.server_alive(fake_dashboard()) is True
    assert desktop.server_alive(fake_dashboard(Other)) is False
    assert desktop.server_alive(1) is False  # nothing listens


def test_running_server_is_reused_not_started(tmp_path, fake_dashboard):
    port = fake_dashboard()
    spawned = []
    state = desktop.ensure_server(port, home=tmp_path, popen=lambda *a, **k: spawned.append(a))
    assert state == "running" and spawned == []


def test_server_is_started_detached_logs_to_web_log_and_waited_for(tmp_path, fake_dashboard):
    port = fake_dashboard()
    answers = iter([False, False, True])  # the fake process "boots" after two probes
    calls = {}

    def popen(argv, **kw):
        calls.update(argv=argv, kw=kw)

    state = desktop.ensure_server(port, home=tmp_path, popen=popen, probe=lambda p: next(answers) if calls else False,
                                  sleep=lambda s: None, wait_s=5)
    assert state == "started"
    assert calls["argv"][0] == sys.executable and "web" in calls["argv"] and str(port) in calls["argv"]
    assert not any("token" in a.lower() for a in calls["argv"])
    kw = calls["kw"]
    assert kw.get("start_new_session") is True or kw.get("creationflags")  # detached from the terminal
    assert (tmp_path / "logs" / "web.log").exists()


def test_server_that_never_answers_is_reported_with_the_log_path(tmp_path):
    t = iter(range(0, 1000, 10))
    with pytest.raises(desktop.DesktopError) as e:
        desktop.ensure_server(1, home=tmp_path, popen=lambda *a, **k: None, probe=lambda p: False,
                              sleep=lambda s: None, wait_s=30, clock=lambda: next(t))
    assert "web.log" in str(e.value)


# ---------- the command ----------

@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    log = {"ensure": [], "popen": [], "web": []}
    monkeypatch.setattr(desktop, "ensure_server", lambda port, **k: log["ensure"].append(port) or "started")
    monkeypatch.setattr(desktop, "launch", lambda argv: log["popen"].append(argv))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: log["web"].append(u) or True)
    return log


def test_app_command_starts_server_then_opens_app_window(app_env, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "find_browser", lambda *a, **k: "/usr/bin/chromium")
    assert cli.main(["app", "--port", "9123"]) == 0
    assert app_env["ensure"] == [9123]
    argv = app_env["popen"][0]
    assert "--app=http://127.0.0.1:9123/" in argv and f"--user-data-dir={tmp_path / 'app-profile'}" in argv
    assert app_env["web"] == []


def test_app_without_chromium_opens_default_browser_and_says_why(app_env, monkeypatch, capsys):
    monkeypatch.setattr(desktop, "find_browser", lambda *a, **k: None)
    assert cli.main(["app"]) == 0
    assert app_env["web"] == ["http://127.0.0.1:8765/"] and app_env["popen"] == []
    assert "Chrome" in capsys.readouterr().out


def test_app_print_only_prints_url(app_env, capsys):
    assert cli.main(["app", "--print"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:8765/"
    assert app_env["ensure"] == [] and app_env["popen"] == [] and app_env["web"] == []


def test_app_no_start_does_not_start_the_server(app_env, monkeypatch):
    monkeypatch.setattr(desktop, "find_browser", lambda *a, **k: "/b")
    assert cli.main(["app", "--no-start"]) == 0
    assert app_env["ensure"] == [] and len(app_env["popen"]) == 1


def test_remote_mode_without_token_is_not_started_in_background(tmp_path, monkeypatch, capsys):
    """The first remote start prints the access token once; into a log file it must never go."""
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    (tmp_path / "artel.yaml").write_text("web:\n  remote: true\n")
    monkeypatch.setattr(desktop, "server_alive", lambda p: False)
    monkeypatch.setattr(desktop, "launch", lambda argv: pytest.fail("must not open"))
    assert cli.main(["app"]) == 2
    assert "artel web" in capsys.readouterr().err


# ---------- shortcuts ----------

ICON = Path(cli.__file__).parent / "web/static/icons/icon-512.png"


def test_linux_shortcut_is_a_desktop_entry_with_icon(tmp_path):
    made = desktop.install_shortcut("Linux", tmp_path, env={}, launcher="/home/u/.local/bin/artel")
    entry = tmp_path / ".local/share/applications/artel.desktop"
    assert made == entry
    text = entry.read_text()
    assert "[Desktop Entry]" in text and "Name=Artel" in text and "Exec=/home/u/.local/bin/artel app" in text
    icon = tmp_path / ".local/share/icons/artel.png"
    assert icon.read_bytes() == ICON.read_bytes() and f"Icon={icon}" in text
    desktop.remove_shortcut("Linux", tmp_path, env={})
    assert not entry.exists() and not icon.exists()
    desktop.remove_shortcut("Linux", tmp_path, env={})  # idempotent


def test_macos_shortcut_is_an_app_bundle_with_shell_launcher(tmp_path):
    app = desktop.install_shortcut("Darwin", tmp_path, env={}, launcher="/usr/local/bin/artel")
    assert app == tmp_path / "Applications/Artel.app"
    exe = app / "Contents/MacOS/Artel"
    assert exe.read_text().startswith("#!/bin/sh") and "/usr/local/bin/artel" in exe.read_text()
    assert exe.stat().st_mode & stat.S_IXUSR
    plist = (app / "Contents/Info.plist").read_text()
    assert "<key>CFBundleExecutable</key>" in plist and "<string>Artel</string>" in plist
    desktop.remove_shortcut("Darwin", tmp_path, env={})
    assert not app.exists()


def test_windows_shortcut_prefers_lnk_and_falls_back_to_cmd(tmp_path):
    env = {"APPDATA": str(tmp_path / "AppData/Roaming")}
    ran = []
    ok = lambda argv: ran.append(argv) or 0  # noqa: E731
    made = desktop.install_shortcut("Windows", tmp_path, env=env, launcher="artel.exe", run=ok)
    programs = tmp_path / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
    assert made == programs / "Artel.lnk"
    assert ran and ran[0][0].lower().startswith("powershell") and "WScript.Shell" in " ".join(ran[0])

    broken = lambda argv: (_ for _ in ()).throw(FileNotFoundError("powershell"))  # noqa: E731
    made = desktop.install_shortcut("Windows", tmp_path, env=env, launcher="artel.exe", run=broken)
    assert made == programs / "Artel.cmd"
    assert "artel.exe" in made.read_text() and "app" in made.read_text()
    desktop.remove_shortcut("Windows", tmp_path, env=env)
    assert not (programs / "Artel.cmd").exists()


def test_install_and_remove_shortcut_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / ".artel"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
    assert cli.main(["app", "--install-shortcut"]) == 0
    assert (tmp_path / ".local/share/applications/artel.desktop").exists()
    assert cli.main(["app", "--remove-shortcut"]) == 0
    assert not (tmp_path / ".local/share/applications/artel.desktop").exists()


def test_manifest_has_install_fields():
    m = json.loads((Path(cli.__file__).parent / "web/static/manifest.webmanifest").read_text())
    assert m["id"] and m["scope"] == "/" and m["display"] == "standalone" and m["description"]
    names = {s["name"] for s in m["shortcuts"]}
    assert names == {"Новый проект", "Нужно вам"}
    assert all(s["url"].startswith("/") for s in m["shortcuts"])
    sw = (Path(cli.__file__).parent / "web/static/sw.js").read_text()
    assert "'/'," in sw and "/static/desk.js" in sw
