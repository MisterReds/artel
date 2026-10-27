"""Run a finished result for its owner: find out how it starts, start it on a free loopback port, hand back a link.

``run_spec`` is pure (meta first, then heuristics over the project's shared dir). ``ResultRunner`` owns the live
processes of one web server / CLI invocation: its registry is an instance attribute, never module state. Starting
is an explicit click of the owner on code the agents wrote: it runs plain unless the project's sandbox config is on.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

from . import sandbox
from .agent_env import agent_env
from .httpd import LocalHTTPServer
from .reports import redact
from .tools import WIN, group_popen_kwargs, terminate_tree

PY = "python3" if not WIN else sys.executable
SERVER_FILES = ("server.py", "app.py", "main.py")
README_CMD = re.compile(r"^\s*(?:\$\s*)?((?:python3?|node|npm|uvicorn|flask)\b.*)$")
URL_RE = re.compile(r"http://(127\.0\.0\.1|localhost|0\.0\.0\.0):(\d+)")
URL_WAIT_S, READY_WAIT_S, TAIL_LINES = 15.0, 20.0, 40
MAX_CMD = 300


def _search_dirs(shared: Path) -> list[Path]:
    subs = sorted(p for p in shared.iterdir() if p.is_dir() and not p.name.startswith(".")) if shared.is_dir() else []
    return [shared, *subs[:5]]


def _rel(shared: Path, d: Path) -> str:
    return "." if d == shared else d.relative_to(shared).as_posix()


def _readme_command(d: Path) -> Optional[list]:
    for name in ("README.md", "readme.md", "README.txt", "README"):
        f = d / name
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")[:60000]
        for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
            for line in block.splitlines():
                m = README_CMD.match(line)
                if m and "<" not in m.group(1):
                    try:
                        return shlex.split(m.group(1))
                    except ValueError:
                        continue
    return None


def _kind(cmd: list) -> str:
    return "web" if any(w in " ".join(cmd) for w in ("http", "serve", "uvicorn", "flask", "runserver", "server", "app",
                                                      "start", "{port}")) else "cli"


def run_spec(project) -> Optional[dict]:
    """How to start the result: {"command": [...], "cwd": rel_dir, "kind": web|static|cli, "source": meta|heuristic}."""
    run = project.meta.get("run")
    if run:
        cmd = run.get("command") if isinstance(run, dict) else run
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in (cmd or [])]
        if argv:
            spec = {"command": argv, "cwd": ".", "kind": "web", "source": "meta"}
            if isinstance(run, dict) and run.get("url_path"):
                spec["url_path"] = str(run["url_path"])
            return spec
    shared = Path(project.shared)
    if not shared.is_dir():
        return None
    dirs = _search_dirs(shared)
    for d in dirs:
        pkg = d / "package.json"
        if pkg.is_file():
            try:
                scripts = (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {})
            except (ValueError, OSError):
                scripts = {}
            if "start" in scripts:
                return {"command": ["npm", "start"], "cwd": _rel(shared, d), "kind": "web", "source": "heuristic"}
    for d in dirs:
        for name in SERVER_FILES:
            if (d / name).is_file():
                return {"command": [PY, name], "cwd": _rel(shared, d), "kind": "web", "source": "heuristic"}
        if (d / "manage.py").is_file():
            return {"command": [PY, "manage.py", "runserver", "127.0.0.1:{port}"], "cwd": _rel(shared, d),
                    "kind": "web", "source": "heuristic"}
    for d in dirs:
        cmd = _readme_command(d)
        if cmd:
            return {"command": cmd, "cwd": _rel(shared, d), "kind": _kind(cmd), "source": "heuristic"}
    for d in dirs:
        if (d / "index.html").is_file():
            return {"command": [], "cwd": _rel(shared, d), "kind": "static", "source": "heuristic"}
    return None


def validate_run(run) -> Optional[dict]:
    """The auditor's `run` field -> what is stored in project.meta["run"], or raise ValueError."""
    if run in (None, "", {}):
        return None
    if not isinstance(run, dict):
        raise ValueError("run must be an object {command, url_path}")
    cmd = run.get("command")
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_CMD:
        raise ValueError(f"run.command must be a non-empty string of at most {MAX_CMD} characters")
    out = {"command": cmd.strip()}
    path = run.get("url_path")
    if path:
        if not isinstance(path, str) or len(path) > MAX_CMD:
            raise ValueError("run.url_path must be a short string")
        out["url_path"] = path
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connects(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def kill_pid(pid: int, grace_s: float = 2.0) -> None:
    """Stop a process group started by an earlier ResultRunner (another process); used from the pid file."""
    if WIN:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30)
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        end = time.monotonic() + grace_s
        while time.monotonic() < end:
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(0.05)


class _Live:
    def __init__(self, proc=None, server=None, port=0, url="", spec=None, log: Optional[Path] = None):
        self.proc, self.server, self.port, self.url, self.spec, self.log = proc, server, port, url, spec, log
        self.answering = False


class ResultRunner:
    """One running result per project; a new start replaces the old one."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.live: dict[str, _Live] = {}
        self.lock = threading.Lock()

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _pidfile(project) -> Path:
        return Path(project.root) / "run.pid"

    @staticmethod
    def _logfile(project) -> Path:
        return Path(project.root) / "run.log"

    def _argv(self, project, argv: list, cwd: Path) -> list:
        if sandbox.settings(self.config)["mode"] == "off" or WIN:
            return argv
        plan = sandbox.plan(self.config, shlex.join(argv), cwd=cwd, writable=[str(project.shared)])
        return plan.argv if plan.backend != "none" else argv

    def _tail(self, log: Optional[Path]) -> str:
        if not log or not log.is_file():
            return ""
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]
        except OSError:
            return ""
        return redact("\n".join(lines))

    # --- API ------------------------------------------------------------
    def start(self, project) -> dict:
        spec = run_spec(project)
        if not spec:
            raise ValueError("нечего запускать: в результате не нашлось ни index.html, ни сервера, ни команды запуска")
        self.stop(project)
        shared = Path(project.shared).resolve()
        cwd = (shared / spec["cwd"]).resolve()
        if cwd != shared and shared not in cwd.parents:
            raise ValueError("cwd outside the result")
        port = free_port()
        log = self._logfile(project)
        if spec["kind"] == "static":
            server = LocalHTTPServer(("127.0.0.1", port), partial(_QuietHandler, directory=str(cwd)))
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            live = _Live(server=server, port=port, url=f"http://127.0.0.1:{port}", spec=spec)
            live.answering = True
            with self.lock:
                self.live[project.slug] = live
            return {"url": live.url, "pid": os.getpid(), "port": port}
        env = agent_env("native", {"PORT": str(port), "HOST": "127.0.0.1"}, self.config)
        argv = [a.replace("{port}", str(port)) for a in spec["command"]]
        if WIN and argv and argv[0] in ("npm", "npx"):
            argv[0] += ".cmd"
        argv = self._argv(project, argv, cwd)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as out:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, **group_popen_kwargs())
        live = _Live(proc=proc, port=port, spec=spec, log=log)
        with self.lock:
            self.live[project.slug] = live
        try:
            self._pidfile(project).write_text(json.dumps({"pid": proc.pid, "port": port}), encoding="utf-8")
        except OSError:
            pass
        found = self._wait_url(live)
        if found:
            live.url, live.port = found[0], found[1]
        else:
            live.url = f"http://127.0.0.1:{port}"
        live.url += spec.get("url_path", "") if spec.get("url_path", "").startswith("/") else ""
        end = time.monotonic() + READY_WAIT_S
        while time.monotonic() < end and proc.poll() is None:
            if _connects(live.port):
                live.answering = True
                break
            time.sleep(0.1)
        return {"url": live.url, "pid": proc.pid, "port": live.port}

    def _wait_url(self, live: _Live):
        end = time.monotonic() + URL_WAIT_S
        while time.monotonic() < end and live.proc.poll() is None:
            try:
                m = URL_RE.search(live.log.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                m = None
            if m:
                return f"http://{'127.0.0.1' if m.group(1) == '0.0.0.0' else m.group(1)}:{m.group(2)}", int(m.group(2))
            if _connects(live.port):
                return None
            time.sleep(0.1)
        return None

    def stop(self, project) -> bool:
        """True when something was running and is stopped."""
        with self.lock:
            live = self.live.pop(project.slug, None)
        stopped = False
        if live:
            if live.server:
                live.server.shutdown()
                live.server.server_close()
                stopped = True
            elif live.proc:
                stopped = live.proc.poll() is None
                terminate_tree(live.proc)
        else:  # started by another process (the CLI): the pid file names its process group
            try:
                pid = json.loads(self._pidfile(project).read_text(encoding="utf-8"))["pid"]
                kill_pid(int(pid))
                stopped = True
            except (OSError, ValueError, KeyError):
                pass
        try:
            self._pidfile(project).unlink()
        except OSError:
            pass
        return stopped

    def status(self, project) -> dict:
        with self.lock:
            live = self.live.get(project.slug)
        spec = run_spec(project)
        base = {"runnable": bool(spec), "kind": (spec or {}).get("kind"), "source": (spec or {}).get("source"),
                "command": " ".join((spec or {}).get("command") or []) if spec else ""}
        if not live:
            return {**base, "running": False, "state": "stopped", "url": "", "log": self._tail(self._logfile(project))}
        alive = live.server is not None or live.proc.poll() is None
        if alive and not live.answering and _connects(live.port):
            live.answering = True
        state = "exited" if not alive else "running" if live.answering else "not_answering"
        return {**base, "running": alive, "state": state, "url": live.url, "pid": live.proc.pid if live.proc else None,
                "log": self._tail(live.log)}

    def stop_all(self) -> None:
        with self.lock:
            slugs = list(self.live)
        for slug in slugs:
            with self.lock:
                live = self.live.pop(slug, None)
            if live and live.server:
                live.server.shutdown()
                live.server.server_close()
            elif live and live.proc:
                terminate_tree(live.proc)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


__all__ = ["run_spec", "validate_run", "ResultRunner", "free_port"]
