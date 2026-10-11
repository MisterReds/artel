"""Evolution survives its process: a generation whose process died (machine restart, killed container, OOM) is
resumed automatically.

A running ``artel evolve`` keeps ``heartbeat.json`` in its evolution home fresh (pid, how it was started, time). The
supervisor — a thread in ``artel web`` (``evolve.auto_resume``, on by default) or ``artel evolve --supervise`` — finds
generations that are still marked running in state.json while their heartbeat is stale and their process is gone, and
starts ``artel evolve --resume`` with the same options. Crash loops are cut off: a generation whose resumed process
keeps dying within minutes stops being restarted and is marked for a person, with the reason in the dashboard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

STALE_S = 180          # no heartbeat for this long and no live process: the generation is orphaned
BEAT_S = 30
QUICK_DEATH_S = 300    # a resumed process that dies faster than this counts as a crash
MAX_QUICK_DEATHS = 3   # ... this many times in a row: stop restarting, a person decides
RESUME_FLAGS = {"--parallel-candidates", "--parallel-bench", "--budget", "--finalists", "--screen-tasks", "--tasks",
                "--runtimes", "--repo", "--config"}
RESUME_SWITCHES = {"--no-stack", "--no-wait"}


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                                 timeout=10).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def evolve_process_running(exclude_supervisors: bool = True, exclude: Optional[int] = None) -> Optional[bool]:
    """Whether any ``artel evolve`` process exists (Linux /proc); None when it cannot be told."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    me = exclude or os.getpid()
    parent = os.getppid()
    for p in proc.iterdir():
        if not p.name.isdigit() or int(p.name) in (me, os.getpid(), parent):
            continue
        try:
            args = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [a.decode("utf-8", "replace") for a in args]
        if "evolve" in words and any("artel" in w for w in words) and "--supervise" not in words:
            return True
    return False


def resume_argv(argv: list) -> list:
    """The options of the original ``artel evolve ...`` that matter for continuing it, plus ``--resume``."""
    out, i = ["evolve", "--resume"], 0
    args = list(argv)
    if args and args[0] == "evolve":
        args = args[1:]
    while i < len(args):
        a = args[i]
        key = a.split("=", 1)[0]
        if key in RESUME_FLAGS:
            if "=" in a:
                out.append(a)
            elif i + 1 < len(args):
                out += [a, args[i + 1]]
                i += 1
        elif a in RESUME_SWITCHES:
            out.append(a)
        i += 1
    return out


class Heartbeat:
    """Kept by a running ``artel evolve`` (a daemon thread); removed on a clean exit."""

    def __init__(self, home: Path, argv: list, cwd: Optional[str] = None):
        self.path = Path(home) / "heartbeat.json"
        self.data = {"pid": os.getpid(), "argv": list(argv), "cwd": cwd or os.getcwd(), "started": time.time(),
                     "artel_home": os.environ.get("ARTEL_HOME", "")}
        self._stop = threading.Event()

    def beat(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**self.data, "at": time.time()}), encoding="utf-8")
        tmp.replace(self.path)

    def start(self) -> "Heartbeat":
        self.beat()

        def loop():
            while not self._stop.wait(BEAT_S):
                try:
                    self.beat()
                except OSError:
                    pass

        threading.Thread(target=loop, daemon=True, name="evolve-heartbeat").start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            hb = json.loads(self.path.read_text(encoding="utf-8"))
            if hb.get("pid") == os.getpid():
                self.path.unlink()
        except (OSError, ValueError):
            pass


class AlreadyRunning(RuntimeError):
    pass


class RepoLock:
    """One evolution per repository at a time, whatever ARTEL_HOME it runs with: a second ``artel evolve`` on the
    same repository (another home, a restored journal, a supervisor) is refused instead of doing the work twice."""

    def __init__(self, repo: Path):
        from .engine import git

        common = Path(git(Path(repo), "rev-parse", "--git-common-dir"))
        self.path = (common if common.is_absolute() else Path(repo) / common) / "artel-evolve.lock"

    def holder(self) -> Optional[int]:
        try:
            pid = int(json.loads(self.path.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            pid = None
        if pid and pid != os.getpid() and pid_alive(pid):
            return pid
        return None

    def acquire(self) -> "RepoLock":
        pid = self.holder()
        if pid:
            raise AlreadyRunning(f"evolution is already running on this repository (pid {pid})")
        if not self.path.exists() and evolve_process_running(exclude_supervisors=True, exclude=os.getpid()):
            raise AlreadyRunning("another `artel evolve` process is running (started by a version without locks)")
        self.path.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
        return self

    def release(self) -> None:
        try:
            if int(json.loads(self.path.read_text(encoding="utf-8"))["pid"]) == os.getpid():
                self.path.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            pass


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def orphaned(evo_root: Path, now: Optional[float] = None) -> list[dict]:
    """Evolution homes with a generation marked running whose process is gone."""
    now = now or time.time()
    out = []
    for d in sorted(Path(evo_root).iterdir()) if Path(evo_root).exists() else []:
        state = _read(d / "state.json")
        run = state.get("running")
        if not run or run.get("auto_resume_blocked"):
            continue
        hb = _read(d / "heartbeat.json")
        if hb and (now - float(hb.get("at", 0)) < STALE_S or pid_alive(hb.get("pid"))):
            continue
        if not hb and evolve_process_running() is not False:
            continue  # started by a version without heartbeats: restart only when surely no evolve process exists
        try:
            if state.get("repo") and RepoLock(Path(state["repo"])).holder():
                continue  # another process (another ARTEL_HOME) is evolving this repository
        except Exception:  # noqa: BLE001 - a missing repository: let the resume report it
            pass
        out.append({"home": d, "state": state, "heartbeat": hb})
    return out


def restart(item: dict, log=print) -> Optional[int]:
    """Start ``artel evolve --resume`` for an orphaned generation; returns the pid (None when restarts are cut off)."""
    home, state, hb = item["home"], item["state"], item["heartbeat"]
    run = state["running"]
    sup = run.setdefault("supervisor", {"restarts": 0, "quick_deaths": 0})
    last = sup.get("last_restart")
    if last and hb.get("started") and float(hb.get("at", 0)) - float(hb["started"]) < QUICK_DEATH_S:
        sup["quick_deaths"] = sup.get("quick_deaths", 0) + 1
    elif last:
        sup["quick_deaths"] = 0
    if sup.get("quick_deaths", 0) >= MAX_QUICK_DEATHS:
        run["auto_resume_blocked"] = (f"the resumed process died within {QUICK_DEATH_S // 60} min "
                                      f"{sup['quick_deaths']} times in a row; see {home / 'supervisor.log'}")
        (home / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
        log(f"evolution {home.name}: not restarting — {run['auto_resume_blocked']}")
        return None
    argv = resume_argv(hb.get("argv") or [])
    if "--repo" not in argv and state.get("repo"):
        argv += ["--repo", state["repo"]]
    # where and as what the resume runs comes from this installation, never from heartbeat.json: ARTEL_HOME is the
    # one this evolution lives in, the cwd its repository, and artel is imported from the supervisor's own code
    # (python -P: nothing from the cwd shadows it)
    env = dict(os.environ)
    env["ARTEL_HOME"] = str(Path(home).parents[1])
    pkg_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join([pkg_root, *[x for x in [os.environ.get("PYTHONPATH")] if x]])
    cwd = state.get("repo") if state.get("repo") and Path(state["repo"]).is_dir() else None
    logf = open(home / "supervisor.log", "ab")
    logf.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} auto-resume: artel {' '.join(argv)}\n".encode())
    logf.flush()
    kw: dict = {"cwd": cwd, "env": env, "stdout": logf, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kw["start_new_session"] = True  # outlives the dashboard that started it
    proc = _spawn([sys.executable, *(["-P"] if sys.version_info >= (3, 11) else []), "-m", "artel.cli", *argv], **kw)
    sup.update(restarts=sup.get("restarts", 0) + 1, last_restart=time.time(), pid=proc.pid)
    (home / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    with (home / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": os.urandom(5).hex(), "ts": time.time(), "kind": "auto_resume",
                             "generation": run.get("generation"), "pid": proc.pid, "argv": argv}) + "\n")
    # the new process writes its own heartbeat at once; until then, mark it so the next check does not double-start
    (home / "heartbeat.json").write_text(json.dumps({"pid": proc.pid, "argv": ["evolve", *argv[1:]], "cwd": cwd,
                                                      "started": time.time(), "at": time.time(),
                                                      "artel_home": env["ARTEL_HOME"]}), encoding="utf-8")
    log(f"evolution {home.name}: generation {run.get('generation')} resumed automatically (pid {proc.pid})")
    return proc.pid


def _spawn(args: list, **kw):
    return subprocess.Popen(args, **kw)


def check(evo_root: Path, log=print) -> list:
    started = []
    for item in orphaned(evo_root):
        try:
            pid = restart(item, log)
        except Exception as e:  # noqa: BLE001 - a supervisor problem must never take the dashboard down
            log(f"evolution {item['home'].name}: auto-resume failed: {e!r}")
            continue
        if pid:
            started.append(pid)
    return started


class Supervisor:
    def __init__(self, evo_root: Path, interval_s: float = 120.0, log=print):
        self.root, self.interval, self.log = Path(evo_root), interval_s, log
        self._stop = threading.Event()

    def start(self) -> "Supervisor":
        def loop():
            while True:
                check(self.root, self.log)
                if self._stop.wait(self.interval):
                    return

        threading.Thread(target=loop, daemon=True, name="evolve-supervisor").start()
        return self

    def stop(self) -> None:
        self._stop.set()


__all__ = ["Heartbeat", "Supervisor", "check", "orphaned", "restart", "resume_argv", "pid_alive"]
