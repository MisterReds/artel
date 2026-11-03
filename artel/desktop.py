"""Desktop client, step 1 (no new dependencies): `artel app` opens the dashboard in its own browser window
(Chromium `--app` mode, separate profile), starting `artel web` in the background when nothing answers, and
`--install-shortcut` puts a launcher into the OS menu. Everything OS-specific takes the system name and the
environment as arguments, so it is tested with fake paths."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ICON = Path(__file__).parent / "web/static/icons/icon-512.png"
ICON_SMALL = Path(__file__).parent / "web/static/icons/icon-192.png"  # an ICO entry is at most 256 px

_LINUX_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                "microsoft-edge-stable", "yandex-browser", "yandex-browser-beta", "brave-browser", "brave")
_LINUX_PATHS = ("/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/snap/bin/chromium",
                "/opt/microsoft/msedge/msedge", "/opt/yandex/browser/yandex-browser",
                "/opt/brave.com/brave/brave")
_MAC_APPS = ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Yandex")
_WIN_EXES = (("Google", "Chrome", "Application", "chrome.exe"), ("Microsoft", "Edge", "Application", "msedge.exe"),
             ("Chromium", "Application", "chrome.exe"), ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
             ("Yandex", "YandexBrowser", "Application", "browser.exe"))


class DesktopError(RuntimeError):
    pass


def find_browser(system: str | None = None, *, env=None, which=shutil.which, exists=os.path.exists) -> str | None:
    """The first installed Chromium-family browser (Chrome, Edge, Chromium, Brave, Yandex), or None."""
    system, env = system or platform.system(), os.environ if env is None else env
    if system == "Windows":
        roots = [env.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        for parts in _WIN_EXES:
            for root in filter(None, roots):
                p = os.path.join(root, *parts)
                if exists(p):
                    return p
        return None
    if system == "Darwin":
        bases = ["/Applications", f"{env.get('HOME') or Path.home()}/Applications"]
        for name in _MAC_APPS:
            for base in bases:
                p = f"{base}/{name}.app/Contents/MacOS/{name}"
                if exists(p):
                    return p
        return None
    for name in _LINUX_NAMES:
        if found := which(name):
            return found
    return next((p for p in _LINUX_PATHS if exists(p)), None)


def app_command(browser: str, url: str, profile: Path) -> list[str]:
    """An own profile keeps the app window apart from the owner's browsing; the URL carries no secret."""
    return [browser, f"--app={url}", f"--user-data-dir={profile}"]


def launch(argv: list[str]) -> None:
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **_detached())


def _detached() -> dict:
    if os.name == "nt":
        return {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return {"start_new_session": True}


def server_alive(port: int) -> bool:
    """True when an Artel dashboard answers on this port (any other server on it does not count)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/manifest.webmanifest", timeout=2) as r:
            return json.load(r).get("name") == "Artel"
    except Exception:  # noqa: BLE001 - refused, timeout, not JSON: no dashboard there
        return False


def ensure_server(port: int, *, home: Path, config: str | None = None, popen=subprocess.Popen, probe=server_alive,
                  sleep=time.sleep, clock=time.monotonic, wait_s: float = 30) -> str:
    """'running' when a dashboard already answers, else start `artel web` detached (log: home/logs/web.log)
    and wait until it answers -> 'started'."""
    if probe(port):
        return "running"
    log_path = Path(home) / "logs" / "web.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-c", "import sys; from artel.cli import main; sys.exit(main())",
            *(["--config", config] if config else []), "web", "--port", str(port)]
    with open(log_path, "ab") as log:
        popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **_detached())
    end = clock() + wait_s
    while clock() < end:
        if probe(port):
            return "started"
        sleep(0.5)
    raise DesktopError(f"панель не ответила за {int(wait_s)} с; смотрите журнал: {log_path}")


# ---------- shortcuts ----------

_DESKTOP_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def _exec_arg(s: str) -> str:
    """One Exec= argument per the Desktop Entry spec: quoted when any reserved character is present; inside the
    quotes " ` $ \\ get a backslash (and a literal backslash is doubled again by the string layer); % is a field
    code, so a literal one is %%."""
    s = s.replace("%", "%%")
    if not _DESKTOP_RESERVED & set(s):
        return s
    out = "".join("\\\\" if c == "\\" else "\\" + c if c in '"`$' else c for c in s)
    return f'"{out}"'


def _cmd_arg(s: str) -> str:
    """One argument for a .cmd line: list2cmdline quoting (+ quotes around cmd metacharacters), % doubled."""
    q = subprocess.list2cmdline([s])
    if not q.startswith('"') and set(s) & set("&|<>^()"):
        q = f'"{q}"'
    return q.replace("%", "%%")


def _paths(system: str, home: Path, env) -> dict:
    home = Path(home)
    if system == "Darwin":
        return {"app": home / "Applications/Artel.app"}
    if system == "Windows":
        programs = Path(env.get("APPDATA") or home / "AppData/Roaming") / "Microsoft/Windows/Start Menu/Programs"
        local = Path(env.get("LOCALAPPDATA") or home / "AppData/Local")
        return {"lnk": programs / "Artel.lnk", "cmd": programs / "Artel.cmd", "ico": local / "Artel/Artel.ico"}
    return {"entry": home / ".local/share/applications/artel.desktop", "icon": home / ".local/share/icons/artel.png"}


def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def ico_from_png(png: bytes) -> bytes:
    """An .ico holding one PNG image (Windows Vista+ reads PNG-in-ICO); sizes of 256 are stored as 0."""
    w, h = _png_size(png)
    return (struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(png), 22) + png)


def icns_from_png(png: bytes) -> bytes:
    """An .icns holding one PNG chunk: 'ic09' is the 512x512 PNG, 'ic10' the 1024x1024 one."""
    kind = {512: b"ic09", 1024: b"ic10"}.get(_png_size(png)[0])
    if kind is None:
        raise ValueError("icns needs a 512 or 1024 px PNG")
    return b"icns" + struct.pack(">I", 16 + len(png)) + kind + struct.pack(">I", 8 + len(png)) + png


# no path text inside: the paths travel in the environment, so nothing needs quoting or escaping
_LNK_SCRIPT = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ARTEL_LNK);"
               "$s.TargetPath=$env:ARTEL_TARGET;$s.Arguments='app';$s.Description='Artel';"
               "$s.IconLocation=$env:ARTEL_ICON;$s.Save()")


def _run_with_env(argv, env=None):
    return subprocess.call(argv, env=env)


def install_shortcut(system: str, home: Path, *, env=None, launcher: str | None = None, run=_run_with_env) -> Path:
    """Create the launcher for this OS and return its path."""
    env = os.environ if env is None else env
    launcher = launcher or shutil.which("artel") or "artel"
    p = _paths(system, home, env)
    if system == "Darwin":
        app = p["app"]
        exe = app / "Contents/MacOS/Artel"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(f"#!/bin/sh\nexec {shlex.quote(launcher)} app\n")
        exe.chmod(0o755)
        (app / "Contents/Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
            "<key>CFBundleExecutable</key><string>Artel</string>\n"
            "<key>CFBundleIdentifier</key><string>app.artel.desktop</string>\n"
            "<key>CFBundleName</key><string>Artel</string>\n"
            "<key>CFBundleIconFile</key><string>Artel</string>\n"
            "<key>CFBundlePackageType</key><string>APPL</string>\n</dict></plist>\n")
        (app / "Contents/Resources").mkdir(exist_ok=True)
        (app / "Contents/Resources/Artel.icns").write_bytes(icns_from_png(ICON.read_bytes()))
        return app
    if system == "Windows":
        p["lnk"].parent.mkdir(parents=True, exist_ok=True)
        p["ico"].parent.mkdir(parents=True, exist_ok=True)
        p["ico"].write_bytes(ico_from_png(ICON_SMALL.read_bytes()))
        p["lnk"].unlink(missing_ok=True)  # a stale one must not pass the existence check below
        penv = {**env, "ARTEL_LNK": str(p["lnk"]), "ARTEL_TARGET": launcher, "ARTEL_ICON": str(p["ico"])}
        try:
            # success is the file, not the exit code
            if run(["powershell", "-NoProfile", "-Command", _LNK_SCRIPT], env=penv) == 0 and p["lnk"].exists():
                p["cmd"].unlink(missing_ok=True)
                return p["lnk"]
        except OSError:
            pass
        p["cmd"].write_bytes(f'@echo off\r\nstart "" /min {_cmd_arg(launcher)} app\r\n'.encode())  # no newline translation
        return p["cmd"]
    p["entry"].parent.mkdir(parents=True, exist_ok=True)
    p["icon"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, p["icon"])
    p["entry"].write_text("[Desktop Entry]\nType=Application\nName=Artel\nComment=Команда агентов доводит цель до результата\n"
                          f"Exec={_exec_arg(launcher)} app\nIcon={p['icon']}\nTerminal=false\n"
                          "Categories=Development;Utility;\nStartupWMClass=Artel\n")
    return p["entry"]


def remove_shortcut(system: str, home: Path, *, env=None) -> None:
    env = os.environ if env is None else env
    for key, p in _paths(system, home, env).items():
        if key == "app":
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def open_default(url: str) -> bool:
    return bool(webbrowser.open(url))
