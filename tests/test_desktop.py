"""`arteliq app`: finding a Chromium-family browser, starting/reusing the dashboard, shortcuts per OS."""
import json
import os
import stat
import struct
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
    mine = f"{tmp_path}/Applications/Chromium.app/Contents/MacOS/Chromium"  # macOS paths are '/'-joined
    assert desktop.find_browser("Darwin", env={"HOME": str(tmp_path)}, **fake_fs(mine)) == mine


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
        body = json.dumps({"name": "Arteliq"}).encode() if self.path == "/manifest.webmanifest" else b"{}"
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


class Child:
    """A started process: poll() is None while it runs, else its exit code."""
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


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
        return Child()

    state = desktop.ensure_server(port, home=tmp_path, popen=popen, probe=lambda p: next(answers) if calls else False,
                                  port_open=lambda p: False, sleep=lambda s: None, wait_s=5)
    assert state == "started"
    assert calls["argv"][0] == sys.executable and "web" in calls["argv"] and str(port) in calls["argv"]
    assert not any("token" in a.lower() for a in calls["argv"])
    kw = calls["kw"]
    assert kw.get("start_new_session") is True or kw.get("creationflags")  # detached from the terminal
    assert (tmp_path / "logs" / "web.log").exists()


def test_server_that_never_answers_is_reported_with_the_log_path(tmp_path):
    t = iter(range(0, 1000, 10))
    with pytest.raises(desktop.DesktopError) as e:
        desktop.ensure_server(1, home=tmp_path, popen=lambda *a, **k: Child(), probe=lambda p: False,
                              port_open=lambda p: False, sleep=lambda s: None, wait_s=30, clock=lambda: next(t))
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
    assert "arteliq web" in capsys.readouterr().err


# ---------- shortcuts ----------

ICON = Path(cli.__file__).parent / "web/static/icons/icon-512.png"


def test_linux_shortcut_is_a_desktop_entry_with_icon(tmp_path):
    made = desktop.install_shortcut("Linux", tmp_path, env={}, launcher="/home/u/.local/bin/artel")
    entry = tmp_path / ".local/share/applications/artel.desktop"
    assert made == entry
    text = entry.read_text()
    assert "[Desktop Entry]" in text and "Name=Arteliq" in text and "Exec=/home/u/.local/bin/artel app" in text
    icon = tmp_path / ".local/share/icons/artel.png"
    assert icon.read_bytes() == ICON.read_bytes() and f"Icon={icon}" in text
    desktop.remove_shortcut("Linux", tmp_path, env={})
    assert not entry.exists() and not icon.exists()
    desktop.remove_shortcut("Linux", tmp_path, env={})  # idempotent


def test_macos_shortcut_is_an_app_bundle_with_shell_launcher(tmp_path):
    app = desktop.install_shortcut("Darwin", tmp_path, env={}, launcher="/usr/local/bin/artel")
    assert app == tmp_path / "Applications/Arteliq.app"
    exe = app / "Contents/MacOS/Arteliq"
    assert exe.read_text().startswith("#!/bin/sh") and "/usr/local/bin/artel" in exe.read_text()
    assert os.name == "nt" or exe.stat().st_mode & stat.S_IXUSR  # Windows has no exec bit
    plist = (app / "Contents/Info.plist").read_text()
    assert "<key>CFBundleExecutable</key>" in plist and "<string>Arteliq</string>" in plist
    desktop.remove_shortcut("Darwin", tmp_path, env={})
    assert not app.exists()


PROGRAMS = "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
WIN_LAUNCHER = r"C:\Users\Ann & Bob\100%\it's $x\artel.exe"


def test_windows_shortcut_passes_paths_only_through_environment(tmp_path):
    env = {"APPDATA": str(tmp_path / "AppData/Roaming"), "LOCALAPPDATA": str(tmp_path / "AppData/Local")}
    seen = []

    def run(argv, env=None):  # PowerShell "makes" the shortcut: the file appears
        seen.append((argv, env))
        Path(env["ARTEL_LNK"]).write_bytes(b"lnk")
        return 0

    programs = tmp_path / PROGRAMS
    made = desktop.install_shortcut("Windows", tmp_path, env=env, launcher=WIN_LAUNCHER, run=run)
    assert made == programs / "Arteliq.lnk"
    argv, penv = seen[0]
    script = " ".join(argv)
    assert argv[0].lower().startswith("powershell") and "WScript.Shell" in script
    assert "$args" not in script  # -Command does not bind $args
    assert penv["ARTEL_LNK"] == str(programs / "Arteliq.lnk") and penv["ARTEL_TARGET"] == WIN_LAUNCHER
    for text in (str(programs), WIN_LAUNCHER, "AppData", "artel.exe"):
        assert text not in script  # no path text inside the script: nothing to quote or inject
    ico = Path(penv["ARTEL_ICON"])
    assert ico.read_bytes()[:4] == b"\0\0\1\0" and "AppData" in str(ico)
    assert not (programs / "Arteliq.cmd").exists()


def test_windows_shortcut_falls_back_to_cmd_when_no_lnk_appears_or_powershell_is_missing(tmp_path):
    env = {"APPDATA": str(tmp_path / "AppData/Roaming")}
    programs = tmp_path / PROGRAMS
    silent = lambda argv, env=None: 0  # noqa: E731 - exit code 0 but no file was written
    made = desktop.install_shortcut("Windows", tmp_path, env=env, launcher="artel.exe", run=silent)
    assert made == programs / "Arteliq.cmd" and "artel.exe" in made.read_text()

    broken = lambda argv, env=None: (_ for _ in ()).throw(FileNotFoundError("powershell"))  # noqa: E731
    made = desktop.install_shortcut("Windows", tmp_path, env=env, launcher="artel.exe", run=broken)
    assert made == programs / "Arteliq.cmd"
    desktop.remove_shortcut("Windows", tmp_path, env=env)
    assert not made.exists()


def test_install_reports_which_windows_launcher_it_made(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData/Roaming"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
    monkeypatch.setattr(desktop.subprocess, "call", lambda *a, **k: 0)  # "succeeds" without a file
    assert cli.main(["app", "--install-shortcut"]) == 0
    out = capsys.readouterr().out
    assert "Arteliq.cmd" in out and ".lnk" in out


def test_install_and_remove_shortcut_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
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


def test_ico_container_wraps_the_png_for_windows():
    png = (ICON.parent / "icon-192.png").read_bytes()
    ico = desktop.ico_from_png(png)
    assert ico[:6] == b"\0\0\1\0\1\0"  # ICONDIR: reserved, type icon, one image
    w, h, _, _, planes, bpp, size, offset = struct.unpack("<BBBBHHII", ico[6:22])
    assert (w, h, size, offset) == (192, 192, len(png), 22) and ico[offset:] == png


def test_icns_container_wraps_the_png_for_macos():
    png = ICON.read_bytes()
    icns = desktop.icns_from_png(png)
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns)
    assert icns[8:12] == b"ic09" and struct.unpack(">I", icns[12:16])[0] == len(png) + 8 and icns[16:] == png
    with pytest.raises(ValueError):
        desktop.icns_from_png((ICON.parent / "icon-192.png").read_bytes())


# ---------- launcher quoting ----------

AWKWARD = ["/h/plain/artel", "/h/with space/artel", '/h/qu"ote/artel', "/h/dol$lar/artel", "/h/per%cent/artel",
           "/h/back`tick/artel", "/h/back\\slash/artel", "/h/it's/artel", "/h/a&b;c/artel"]


RESERVED = " \t\n\"'\\><~|&;$*?#()`"


def parse_exec(line: str) -> list[str]:
    """The Desktop Entry spec, read the way a launcher reads it: string unescape, then Exec unquoting, then %%."""
    value = line.split("=", 1)[1]
    value = value.replace("\\\\", "\\")  # the string layer: \\ -> \
    args, cur, quoted, i, have = [], "", False, 0, False
    while i < len(value):
        c = value[i]
        if quoted:
            if c == "\\" and i + 1 < len(value) and value[i + 1] in '"`$\\':
                cur, i = cur + value[i + 1], i + 1
            elif c == '"':
                quoted = False
            else:
                cur += c
        elif c == '"':
            quoted, have = True, True
        elif c == " ":
            if have or cur:
                args.append(cur)
            cur, have = "", False
        else:
            assert c not in RESERVED, f"reserved {c!r} outside quotes"
            cur += c
        i += 1
    if have or cur:
        args.append(cur)
    assert all(a.replace("%%", "").count("%") == 0 for a in args), "a lone % is a field code"
    return [a.replace("%%", "%") for a in args]


@pytest.mark.parametrize("launcher", AWKWARD)
def test_desktop_entry_exec_roundtrips_awkward_paths(tmp_path, launcher):
    entry = desktop.install_shortcut("Linux", tmp_path, env={}, launcher=launcher)
    line = next(ln for ln in entry.read_text().splitlines() if ln.startswith("Exec="))
    assert parse_exec(line) == [launcher, "app"]


def test_macos_launcher_script_is_shell_safe(tmp_path):
    import shlex
    for launcher in AWKWARD:
        app = desktop.install_shortcut("Darwin", tmp_path, env={}, launcher=launcher)
        body = (app / "Contents/MacOS/Arteliq").read_text().splitlines()[1]
        assert shlex.split(body) == ["exec", launcher, "app"]


def test_macos_bundle_has_a_real_icns_named_in_the_plist(tmp_path):
    app = desktop.install_shortcut("Darwin", tmp_path, env={}, launcher="artel")
    assert "<key>CFBundleIconFile</key><string>Arteliq</string>" in (app / "Contents/Info.plist").read_text()
    assert (app / "Contents/Resources/Arteliq.icns").read_bytes() == desktop.icns_from_png(ICON.read_bytes())
    assert not (app / "Contents/Resources/Arteliq.png").exists()


def test_windows_cmd_quotes_spaces_and_doubles_percent(tmp_path):
    env = {"APPDATA": str(tmp_path)}
    broken = lambda argv, env=None: 1  # noqa: E731
    for launcher, want in [(r"C:\Program Files\Arteliq\artel.exe", r'"C:\Program Files\Arteliq\artel.exe"'),
                           (r"C:\100%\artel.exe", r"C:\100%%\artel.exe"),
                           (r"C:\A&B\artel.exe", r'"C:\A&B\artel.exe"'),
                           (r"C:\My 50%\artel.exe", r'"C:\My 50%%\artel.exe"')]:
        made = desktop.install_shortcut("Windows", tmp_path, env=env, launcher=launcher, run=broken)
        assert f"start \"\" /min {want} app\r\n" in made.read_bytes().decode()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_app_profile_dir_is_private_new_or_existing(app_env, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "find_browser", lambda *a, **k: "/usr/bin/chromium")
    profile = tmp_path / "app-profile"
    assert cli.main(["app"]) == 0
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    profile.chmod(0o755)
    assert cli.main(["app"]) == 0
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700


# ---------- starting the server safely ----------

def _other_server(fake_dashboard):
    class Other(_Artel):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>not artel</html>")

    return fake_dashboard(Other)


def test_a_port_held_by_another_program_is_reported_and_nothing_is_started(tmp_path, fake_dashboard):
    port = _other_server(fake_dashboard)
    spawned = []
    with pytest.raises(desktop.DesktopError) as e:
        desktop.ensure_server(port, home=tmp_path, popen=lambda *a, **k: spawned.append(a))
    assert str(e.value) == f"порт {port} занят другой программой" and spawned == []


def test_child_that_dies_early_is_reported_with_the_log_tail(tmp_path):
    def popen(argv, stdout=None, **kw):
        stdout.write(b"line1\nTraceback...\nOSError: address already in use\n")
        stdout.flush()
        return Child(code=1)

    t = iter(range(0, 1000))
    with pytest.raises(desktop.DesktopError) as e:
        desktop.ensure_server(1, home=tmp_path, popen=popen, probe=lambda p: False, port_open=lambda p: False,
                              sleep=lambda s: None, clock=lambda: next(t), wait_s=500)
    assert "address already in use" in str(e.value) and "завершилась" in str(e.value)
    assert not (tmp_path / "web.start.lock").exists()  # the lock does not outlive a failed start


def _start_with_log(tmp_path):
    started = []

    def popen(*a, **k):
        started.append(1)
        return Child()

    desktop.ensure_server(1, home=tmp_path, popen=popen, port_open=lambda p: False, probe=lambda p: bool(started),
                          sleep=lambda s: None)
    return tmp_path / "logs" / "web.log"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_web_log_is_private_and_capped_at_start(tmp_path):
    log = tmp_path / "logs" / "web.log"
    log.parent.mkdir()
    log.write_bytes(b"old\n" * 100 + b"x" * (3 * 1024 * 1024) + b"TAIL\n")
    log.chmod(0o644)
    _start_with_log(tmp_path)
    data = log.read_bytes()
    assert len(data) <= 1024 * 1024 + 16 and data.endswith(b"TAIL\n") and not data.startswith(b"old")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_web_log_under_the_cap_is_kept(tmp_path):
    log = tmp_path / "logs" / "web.log"
    log.parent.mkdir()
    log.write_bytes(b"keep me\n")
    _start_with_log(tmp_path)
    assert log.read_bytes() == b"keep me\n"


def test_concurrent_starts_spawn_one_server(tmp_path):
    import time as _t
    spawned, up, results = [], threading.Event(), []

    def popen(*a, **k):
        spawned.append(1)
        _t.sleep(0.2)  # booting takes a while; the second caller arrives meanwhile
        up.set()
        return Child()

    def go():
        results.append(desktop.ensure_server(1, home=tmp_path, popen=popen, probe=lambda p: up.is_set(),
                                             port_open=lambda p: False, sleep=lambda s: _t.sleep(0.01), wait_s=10))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert len(spawned) == 1 and len(results) == 2 and set(results) <= {"started", "running"}
    assert not (tmp_path / "web.start.lock").exists()


def test_stale_start_lock_of_a_dead_process_is_taken_over(tmp_path):
    import subprocess as sp
    dead = sp.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (tmp_path / "web.start.lock").write_text(str(dead.pid))
    spawned = []
    up = []

    def popen(*a, **k):
        spawned.append(1)
        up.append(1)
        return Child()

    state = desktop.ensure_server(1, home=tmp_path, popen=popen, probe=lambda p: bool(up), port_open=lambda p: False,
                                  sleep=lambda s: None)
    assert state == "started" and spawned == [1]


def test_live_start_lock_makes_the_caller_wait_instead_of_spawning(tmp_path):
    (tmp_path / "web.start.lock").write_text(str(os.getpid()))  # a live process is starting the server
    ticks = iter([False, False, True])
    state = desktop.ensure_server(1, home=tmp_path, popen=lambda *a, **k: pytest.fail("second start"),
                                  probe=lambda p: next(ticks), port_open=lambda p: False, sleep=lambda s: None)
    assert state == "running"
    assert (tmp_path / "web.start.lock").exists()  # not ours: left alone


def test_app_does_not_open_a_window_on_a_port_held_by_another_program(app_env, monkeypatch, capsys):
    def busy(port, **k):
        raise desktop.DesktopError(f"порт {port} занят другой программой")

    monkeypatch.setattr(desktop, "ensure_server", busy)
    monkeypatch.setattr(desktop, "find_browser", lambda *a, **k: "/usr/bin/chromium")
    assert cli.main(["app", "--port", "9124"]) == 1
    assert "порт 9124 занят другой программой" in capsys.readouterr().err
    assert app_env["popen"] == [] and app_env["web"] == []
