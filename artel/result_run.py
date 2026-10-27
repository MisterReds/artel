"""Run a finished result for its owner: find out how it starts, start it on a free loopback port, hand back a link.

``run_spec`` is pure (meta first, then heuristics over the project's shared dir). ``ResultRunner`` owns the live
processes of one web server / CLI invocation: its registry is an instance attribute, never module state. Starting
is an explicit click of the owner on code the agents wrote: it runs plain unless the project's sandbox config is on.
"""
from __future__ import annotations

import csv
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
README_CMD = re.compile(r"^\s*(?:\$\s*)?((?:python3?|node|npm|npx|uvicorn|flask)\b.*)$")
URL_RE = re.compile(r"http://(127\.0\.0\.1|localhost|0\.0\.0\.0):(\d+)")
URL_WAIT_S, READY_WAIT_S, TAIL_LINES = 15.0, 20.0, 40
MAX_CMD = 300
TAIL_BYTES, LOG_KEEP, LOG_MAX = 64 * 1024, 1024 * 1024, 2 * 1024 * 1024
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "cmd", "powershell", "pwsh", "ksh", "csh", "tcsh"}
WEB_WORDS = {"http", "serve", "server", "uvicorn", "flask", "runserver", "app", "start", "dev", "{port}"}
NOT_SERVER_NPM = {"install", "i", "ci", "test", "t", "build", "lint", "audit", "update", "publish", "pack"}


def _search_dirs(shared: Path) -> list[Path]:
    subs = sorted(p for p in shared.iterdir() if p.is_dir() and not p.name.startswith(".")) if shared.is_dir() else []
    return [shared, *subs[:5]]


def _rel(shared: Path, d: Path) -> str:
    return "." if d == shared else d.relative_to(shared).as_posix()


def _starts_server(argv: list) -> bool:
    """A README line is taken only when it looks like starting a server/app, not installing, testing or building."""
    if not argv:
        return False
    head, rest = Path(argv[0]).name.lower(), argv[1:]
    if head in ("npm", "npx"):
        sub = next((a for a in rest if not a.startswith("-")), "")
        if sub == "run":
            script = next((a for a in rest[rest.index("run") + 1:] if not a.startswith("-")), "")
            return bool(script) and script.split(":")[0] in ("start", "dev", "serve", "server")
        return head == "npm" and sub == "start"
    if head.startswith("python"):
        if rest[:1] == ["-m"]:
            return len(rest) > 1 and rest[1].split(".")[0] in ("http", "uvicorn", "flask", "gunicorn", "hypercorn", "streamlit")
        script = next((a for a in rest if not a.startswith("-")), "")
        return bool(script) and not re.match(r"(?i)(build|test|lint|setup|install)", Path(script).stem)
    if head == "node":
        script = next((a for a in rest if not a.startswith("-")), "")
        return bool(script) and not re.match(r"(?i)(build|test|lint)", Path(script).stem)
    if head == "flask":
        return "run" in rest
    return head == "uvicorn"


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
                        argv = shlex.split(m.group(1))
                    except ValueError:
                        continue
                    if _starts_server(argv):
                        return argv
    return None


def _kind(cmd: list) -> str:
    words = set(re.findall(r"\{port\}|\w+", " ".join(cmd).lower()))
    return "web" if words & WEB_WORDS else "cli"


def spec_display(spec: dict) -> str:
    """The exact command the owner confirms (and `run_confirmed` stores), with its directory when not the root."""
    cmd = shlex.join(spec.get("command") or [])
    return cmd if spec.get("cwd", ".") == "." else f"{cmd}  (в {spec['cwd']})"


def run_spec(project) -> Optional[dict]:
    """How to start the result: {"command": [...], "cwd": rel_dir, "kind": web|static|cli, "source": meta|heuristic}.
    Order: meta, package.json start, server.py/app.py/main.py/manage.py, a static index.html, README (last)."""
    run = project.meta.get("run")
    if run:
        try:
            ok = validate_run(run if isinstance(run, dict) else {"command": run if isinstance(run, str) else " ".join(run)})
        except (ValueError, TypeError):
            ok = None
        argv = shlex.split(ok["command"]) if ok else []
        if argv:
            spec = {"command": argv, "cwd": ".", "kind": "web", "source": "meta"}
            if ok.get("url_path"):
                spec["url_path"] = ok["url_path"]
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
            if isinstance(scripts, dict) and "start" in scripts:
                return {"command": ["npm", "start"], "cwd": _rel(shared, d), "kind": "web", "source": "heuristic"}
    for d in dirs:
        for name in SERVER_FILES:
            if (d / name).is_file():
                return {"command": [PY, name], "cwd": _rel(shared, d), "kind": "web", "source": "heuristic"}
        if (d / "manage.py").is_file():
            return {"command": [PY, "manage.py", "runserver", "127.0.0.1:{port}"], "cwd": _rel(shared, d),
                    "kind": "web", "source": "heuristic"}
    for d in dirs:
        if (d / "index.html").is_file():
            return {"command": [], "cwd": _rel(shared, d), "kind": "static", "source": "heuristic"}
    for d in dirs:
        cmd = _readme_command(d)
        if cmd:
            return {"command": cmd, "cwd": _rel(shared, d), "kind": _kind(cmd), "source": "heuristic"}
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
    try:
        argv = shlex.split(cmd)
    except ValueError:
        raise ValueError("run.command is not a valid command line")
    if not argv:
        raise ValueError("run.command is empty")
    first = argv[0]
    if Path(first.replace("\\", "/")).name.lower().removesuffix(".exe") in SHELLS:
        raise ValueError("run.command must not start a shell")
    if first.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", first):
        raise ValueError("run.command must be relative to the result, not an absolute path")
    if any(".." in re.split(r"[\\/]", a) for a in argv):
        raise ValueError("run.command must not contain `..`")
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


def proc_identity(pid: int) -> Optional[dict]:
    """{"create_time", "argv0"} of a live process, or None. Linux: /proc/<pid>/stat field 22 (start time in ticks);
    macOS: `ps -o lstart=`; Windows has no cheap creation time here, so only the image name (`tasklist`)."""
    pid = int(pid)
    try:
        if WIN:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=15).stdout.strip()
            row = next(csv.reader([out.splitlines()[0]]), []) if out else []
            return {"create_time": "", "argv0": row[0]} if len(row) > 1 and row[1] == str(pid) else None
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            fields = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            try:
                argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode("utf-8", "replace")
            except OSError:
                argv0 = ""
            return {"create_time": fields[19], "argv0": argv0}
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=15)
        started = r.stdout.strip()
        if not started:
            return None
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=15)
        return {"create_time": started, "argv0": comm.stdout.strip()}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def pid_record(pid: int, **extra) -> dict:
    ident = proc_identity(pid) or {"create_time": "", "argv0": ""}
    pgid = pid
    if not WIN:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pass
    return {"pid": pid, "pgid": pgid, **ident, **extra}


def pid_alive_same(rec) -> bool:
    """True only when the pid file's process is still the very process that was started (not a reused pid)."""
    try:
        pid = int(rec["pid"])
        ident = proc_identity(pid)
        if not ident:
            return False
        if WIN:
            return bool(rec.get("argv0")) and ident["argv0"].lower() == str(rec["argv0"]).lower()
        if not rec.get("create_time") or ident["create_time"] != str(rec["create_time"]):
            return False
        return os.getpgid(pid) == int(rec.get("pgid", -1))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def kill_pid(rec: dict, grace_s: float = 2.0) -> bool:
    """Stop the process group named by a pid-file record from an earlier ResultRunner (another process), but only
    after its identity is verified; False (and nothing signalled) when the pid is gone or was reused."""
    if not pid_alive_same(rec):
        return False
    pid = int(rec["pid"])
    if WIN:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30)
        return True
    import signal
    pgid = int(rec["pgid"])
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return True
        end = time.monotonic() + grace_s
        while time.monotonic() < end:
            if not pid_alive_same(rec):
                return True
            time.sleep(0.05)
    return True


def _read_tail(log: Optional[Path], nbytes: int = TAIL_BYTES) -> str:
    """The last ``nbytes`` of a log, read by seeking (a log may be megabytes)."""
    try:
        with open(log, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - nbytes))
            data = f.read()
    except (OSError, TypeError):
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > nbytes and "\n" in text:
        text = text.split("\n", 1)[1]  # the first line is cut in the middle
    return text


def _pump(stream, log: Path) -> None:
    """Copy the child's output into a capped log: past LOG_MAX it is cut back to its last LOG_KEEP bytes."""
    try:
        with open(log, "wb+", buffering=0) as f:
            size = 0
            while True:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
                if size > LOG_MAX:
                    f.seek(-LOG_KEEP, 2)
                    keep = f.read()
                    f.seek(0)
                    f.truncate(0)
                    f.write(keep)
                    size = len(keep)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


class _Live:
    def __init__(self, proc=None, server=None, port=0, url="", spec=None, log: Optional[Path] = None,
                 pidfile: Optional[Path] = None):
        self.proc, self.server, self.port, self.url, self.spec, self.log = proc, server, port, url, spec, log
        self.pidfile = pidfile
        self.answering = False


class ResultRunner:
    """One running result per project; a new start replaces the old one."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.live: dict[str, _Live] = {}
        self.lock = threading.Lock()
        self._plocks: dict[str, threading.Lock] = {}  # per project: start/stop never interleave

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _pidfile(project) -> Path:
        return Path(project.root) / "run.pid"

    @staticmethod
    def _logfile(project) -> Path:
        return Path(project.root) / "run.log"

    def _plock(self, project) -> threading.Lock:
        with self.lock:
            return self._plocks.setdefault(project.slug, threading.Lock())

    def _argv(self, project, argv: list, cwd: Path) -> list:
        if sandbox.settings(self.config)["mode"] == "off" or WIN:
            return argv
        plan = sandbox.plan(self.config, shlex.join(argv), cwd=cwd, writable=[str(project.shared)])
        return plan.argv if plan.backend != "none" else argv

    def sandboxed(self, project) -> bool:
        if sandbox.settings(self.config)["mode"] == "off" or WIN:
            return False
        try:
            return sandbox.plan(self.config, "true", cwd=project.shared, writable=[str(project.shared)]).backend != "none"
        except Exception:  # noqa: BLE001
            return False

    def _tail(self, log: Optional[Path]) -> str:
        text = _read_tail(log)
        return redact("\n".join(text.splitlines()[-TAIL_LINES:])) if text else ""

    def _read_pidfile(self, project) -> Optional[dict]:
        try:
            rec = json.loads(self._pidfile(project).read_text(encoding="utf-8"))
            return rec if isinstance(rec, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _drop_pidfile(path: Path, pid: Optional[int] = None) -> None:
        try:
            if pid is not None:
                if int(json.loads(path.read_text(encoding="utf-8")).get("pid", -1)) != pid:
                    return
            path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    def _watch(self, proc, pidfile: Path) -> None:
        proc.wait()
        self._drop_pidfile(pidfile, proc.pid)

    # --- API ------------------------------------------------------------
    def start(self, project) -> dict:
        """Start (or restart) the result; ``{"busy": True}`` when another start of this project is in progress."""
        lk = self._plock(project)
        if not lk.acquire(blocking=False):
            return {"busy": True}
        try:
            return self._start(project)
        finally:
            lk.release()

    def _start(self, project) -> dict:
        spec = run_spec(project)
        if not spec:
            raise ValueError("нечего запускать: в результате не нашлось ни index.html, ни сервера, ни команды запуска")
        self._stop(project)
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
        log.write_bytes(b"")
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **group_popen_kwargs())
        threading.Thread(target=_pump, args=(proc.stdout, log), daemon=True).start()
        pidfile = self._pidfile(project)
        live = _Live(proc=proc, port=port, spec=spec, log=log, pidfile=pidfile)
        with self.lock:
            self.live[project.slug] = live
        record = pid_record(proc.pid, port=port)
        try:
            pidfile.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            pass
        threading.Thread(target=self._watch, args=(proc, pidfile), daemon=True).start()
        found = self._wait_url(live)
        if found:
            live.url, live.port = found[0], found[1]
        else:
            live.url = f"http://127.0.0.1:{port}"
        live.url += spec.get("url_path", "") if spec.get("url_path", "").startswith("/") else ""
        if proc.poll() is None:
            try:
                pidfile.write_text(json.dumps({**record, "port": live.port, "url": live.url}), encoding="utf-8")
            except OSError:
                pass
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
            m = URL_RE.search(_read_tail(live.log))
            if m:
                return f"http://{'127.0.0.1' if m.group(1) == '0.0.0.0' else m.group(1)}:{m.group(2)}", int(m.group(2))
            if _connects(live.port):
                return None
            time.sleep(0.1)
        return None

    def stop(self, project) -> bool:
        """True when something was running and is stopped."""
        with self._plock(project):
            return self._stop(project)

    def _stop(self, project) -> bool:
        with self.lock:
            live = self.live.pop(project.slug, None)
        stopped = False
        if live:
            stopped = self._stop_live(live)
        else:  # started by another process (the CLI): the pid file names its process group, if still the same process
            rec = self._read_pidfile(project)
            if rec is not None:
                stopped = kill_pid(rec)
        self._drop_pidfile(self._pidfile(project))
        return stopped

    @staticmethod
    def _stop_live(live: _Live) -> bool:
        if live.server:
            live.server.shutdown()
            live.server.server_close()
            return True
        if live.proc:
            was = live.proc.poll() is None
            terminate_tree(live.proc)
            if live.pidfile:
                ResultRunner._drop_pidfile(live.pidfile, live.proc.pid)
            return was
        return False

    def status(self, project) -> dict:
        with self.lock:
            live = self.live.get(project.slug)
        spec = run_spec(project)
        base = {"runnable": bool(spec), "kind": (spec or {}).get("kind"), "source": (spec or {}).get("source"),
                "command": spec_display(spec) if spec and spec.get("command") else "", "sandbox": self.sandboxed(project),
                "needs_confirm": bool(spec and spec.get("command")
                                      and project.meta.get("run_confirmed") != spec_display(spec))}
        if not live:
            rec = self._read_pidfile(project)
            if rec is not None and pid_alive_same(rec):  # started by `artel result run` in another process
                port = int(rec.get("port") or 0)
                up = bool(port) and _connects(port)
                return {**base, "running": True, "state": "running" if up else "not_answering",
                        "url": rec.get("url") or (f"http://127.0.0.1:{port}" if port else ""), "pid": rec["pid"],
                        "external": True, "log": self._tail(self._logfile(project))}
            if rec is not None:
                self._drop_pidfile(self._pidfile(project))
            return {**base, "running": False, "state": "stopped", "url": "", "log": self._tail(self._logfile(project))}
        alive = live.server is not None or live.proc.poll() is None
        if alive and not live.answering and _connects(live.port):
            live.answering = True
        state = "exited" if not alive else "running" if live.answering else "not_answering"
        return {**base, "running": alive, "state": state, "url": live.url, "pid": live.proc.pid if live.proc else None,
                "log": self._tail(live.log)}

    def stop_all(self) -> None:
        with self.lock:
            lives = list(self.live.values())
            self.live.clear()
        for live in lives:
            self._stop_live(live)


class _QuietHandler(SimpleHTTPRequestHandler):
    """Static result server: loopback Host only, and nothing whose real path leaves the served directory."""

    def log_message(self, *args):
        pass

    def send_head(self):
        host = (self.headers.get("Host") or "").strip().lower()
        host = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if host not in ("127.0.0.1", "localhost", "::1"):
            self.send_error(403, "loopback only")
            return None
        return super().send_head()

    def translate_path(self, path):
        real = os.path.realpath(super().translate_path(path))
        root = os.path.realpath(self.directory)
        if real != root and not real.startswith(root.rstrip(os.sep) + os.sep):
            return os.path.join(root, ".artel-outside-the-result")  # does not exist -> 404
        return real


__all__ = ["run_spec", "validate_run", "ResultRunner", "free_port", "spec_display"]
