"""Dashboard server (stdlib only). Read APIs + SSE event stream + a few control actions.

Binds to 127.0.0.1 by default: the control endpoints start agents that execute
shell commands, so do not expose this server to untrusted networks.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import analytics
from ..manager import human_comment, new_kernel, resume, run_goal
from ..project import Project, list_projects

STATIC = Path(__file__).parent / "index.html"
REGISTRY = Path(__file__).parent.parent / "data" / "harnesses.json"


class Runner:
    """Projects driven by this server process."""

    def __init__(self, config: dict):
        self.config = config
        self.kernel = new_kernel(config)
        self.active: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        from ..updater import Updater

        self.updater = Updater(config=config)
        from ..providers import CodexLogin

        self.codex_login = CodexLogin(config)
        from ..tools import Installer

        self.installer = Installer()
        self.update_state: dict = {"phase": "idle"}  # idle | checking | verifying | restart_pending | error
        self.restart_pending = False

    # --- self-update ----------------------------------------------------
    def update_status(self, fetch: bool = False) -> dict:
        st = self.updater.status(fetch=True) if fetch or self.updater._last is None else self.updater._last
        return {**st, "state": self.update_state, "running_projects": sorted(self.active),
                "history": self.updater.history()[-5:]}

    def update_apply(self, rollback: bool = False) -> dict:
        from ..updater import UpdateError

        self.update_state = {"phase": "verifying" if not rollback else "rolling back", "since": time.time()}
        try:
            out = self.updater.rollback() if rollback else self.updater.apply()
        except UpdateError as e:
            self.update_state = {"phase": "error", "error": str(e)[-1500:]}
            return {"ok": False, "error": str(e)}
        self.restart_pending = True
        self.update_state = {"phase": "restart_pending", "to": out.get("to"),
                             "waiting_for": sorted(self.active)}
        return {"ok": True, **out}

    def start_update_loop(self) -> None:
        """Check the update channel periodically; restart into the new code once no project is running."""
        from ..updater import restart_process

        interval = float(self.updater.cfg.get("check_interval_min", 30)) * 60

        def loop():
            last = 0.0
            while True:
                if self.restart_pending and not self.active:
                    time.sleep(1.0)  # let the HTTP response that triggered it finish
                    restart_process()
                if interval > 0 and time.time() - last > interval:
                    last = time.time()
                    try:
                        self.updater.status(fetch=True)
                    except Exception:  # noqa: BLE001 - offline is fine; the next check retries
                        pass
                time.sleep(2.0)

        threading.Thread(target=loop, daemon=True, name="artel-updater").start()

    def start(self, goal: str, name: str | None) -> str:
        stop = threading.Event()
        project = Project.create(goal, self.config, name=name)
        self._spawn(project.slug, stop, lambda: self._drive(project, stop))
        return project.slug

    def _drive(self, project: Project, stop: threading.Event) -> None:
        from ..manager import Manager

        try:
            Manager(self.kernel, project, stop).run()
        except Exception as exc:  # noqa: BLE001 - surface in the log, keep serving
            project.log.append("manager/fatal", error=repr(exc))
            project.set_meta(status="error")

    def resume(self, slug: str) -> None:
        if slug in self.active:
            return
        from ..manager import continue_after_human

        stop = threading.Event()
        project = Project.open(slug, self.config)
        continue_after_human(project)
        self._spawn(slug, stop, lambda: self._drive(project, stop))

    def _spawn(self, slug: str, stop: threading.Event, fn) -> None:
        def wrapped():
            try:
                fn()
            finally:
                with self.lock:
                    self.active.pop(slug, None)

        with self.lock:
            self.active[slug] = stop
        threading.Thread(target=wrapped, daemon=True, name=f"artel-{slug}").start()

    def stop(self, slug: str) -> bool:
        ev = self.active.get(slug)
        if ev:
            ev.set()
        return bool(ev)


def project_view(project: Project, runner: Runner) -> dict:
    snap = project.board.snapshot()
    workers = []
    running = {t["assignee"]: t["id"] for t in snap["tasks"] if t["status"] == "running"}
    for p in project.personas():
        files = project.worker_files(p.name)
        done = sum(1 for t in snap["tasks"] if t["assignee"] == p.name and t["status"] == "done")
        workers.append({"name": p.name, "role": p.role, "files": files, "current": running.get(p.name),
                        "done": done, "skills": p.skills})
    timelines = analytics.task_timelines(analytics._events(project))
    for t in snap["tasks"]:
        t["timeline"] = timelines.get(t["id"], [])
    stats = analytics.project_stats(project, live=project.slug in runner.active)
    return {"slug": project.slug, "meta": project.meta, "board": snap, "workers": workers, "stats": stats,
            "notes": project.prompt_notes(), "live": project.slug in runner.active, "seq": project.log.seq}


def evolution_view() -> list[dict]:
    from ..evolve.engine import git
    from ..project import artel_home

    out = []
    base = artel_home() / "evolution"
    for d in sorted(base.iterdir()) if base.exists() else []:
        sf = d / "state.json"
        if not sf.exists():
            continue
        state = json.loads(sf.read_text())
        lp = d / "ledger.jsonl"
        ledger = [json.loads(ln) for ln in lp.read_text().splitlines() if ln.strip()] if lp.exists() else []
        absorb = [json.loads(p.read_text()) for p in sorted((d / "absorb").glob("*.json"))] if (d / "absorb").exists() else []
        branches = []
        if state.get("repo"):
            raw = git(Path(state["repo"]), "for-each-ref", "--format=%(refname:short)|%(objectname:short)|%(subject)",
                      "refs/heads/evolve/", check=False)
            branches = [dict(zip(("name", "sha", "subject"), ln.split("|", 2))) for ln in raw.splitlines() if ln]
            state["champion"] = git(Path(state["repo"]), "rev-parse", "--short", state.get("main", "HEAD"), check=False)
        diag = sorted(d.glob("gen*-diagnosis.md"))
        out.append({"name": d.name, "state": state, "ledger": ledger, "absorb": absorb, "branches": branches,
                    "diagnosis": diag[-1].read_text() if diag else ""})
    return out


def make_handler(runner: Runner):
    class Handler(BaseHTTPRequestHandler):
        server_version = "artel/0.1"

        def log_message(self, *args):  # quiet
            pass

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _project(self, slug: str) -> Project:
            return Project.open(slug, runner.config)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            q = parse_qs(url.query)
            try:
                if not parts:
                    body = STATIC.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif parts == ["api", "projects"]:
                    self._json([{**p, "live": p["slug"] in runner.active} for p in list_projects()])
                elif parts == ["api", "portfolio"]:
                    self._json(analytics.portfolio(runner.config, live=set(runner.active)))
                elif parts == ["api", "analytics"]:
                    self._json(analytics.agent_analytics(analytics.portfolio(runner.config, live=set(runner.active))))
                elif parts == ["api", "registry"]:
                    body = REGISTRY.read_bytes() if REGISTRY.exists() else b"[]"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif parts and parts[0] == "static":
                    self._static("/".join(parts[1:]))
                elif parts == ["favicon.ico"] or parts == ["favicon.svg"]:
                    self._static("favicon.svg")
                elif parts == ["api", "ready"]:
                    from ..runtimes import detect_runtime

                    self._json({"runtime": detect_runtime(runner.config)})
                elif parts == ["api", "providers"]:
                    from ..providers import overview

                    self._json({**overview(runner.config), "codex_login": runner.codex_login.state,
                                "installs": runner.installer.state})
                elif parts == ["api", "providers", "codex", "login"]:
                    self._json(runner.codex_login.state)
                elif parts == ["api", "update"]:
                    self._json(runner.update_status(fetch=q.get("check", ["0"])[0] == "1"))
                elif parts == ["api", "evolution"]:
                    self._json(evolution_view())
                elif parts == ["api", "skills"]:
                    self._json(runner.kernel.get("skills").list())
                elif len(parts) == 3 and parts[:2] == ["api", "skills"]:
                    self._json({"name": parts[2], "text": runner.kernel.get("skills").view(parts[2]),
                                "ledger": runner.kernel.get("skills").ledger(parts[2])})
                elif len(parts) == 3 and parts[:2] == ["api", "projects"]:
                    self._json(project_view(self._project(parts[2]), runner))
                elif len(parts) == 4 and parts[3] == "events":
                    self._sse(self._project(parts[2]), int(q.get("after", ["0"])[0]))
                elif len(parts) == 4 and parts[3] == "files":
                    self._files(self._project(parts[2]), q.get("path", [""])[0])
                else:
                    self._json({"error": "not found"}, 404)
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _static(self, rel: str) -> None:
            root = (Path(__file__).parent / "static").resolve()
            f = (root / rel).resolve()
            if root not in f.parents or not f.is_file():
                return self._json({"error": "not found"}, 404)
            types = {".svg": "image/svg+xml", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
                     ".png": "image/png"}
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", types.get(f.suffix, "application/octet-stream"))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _files(self, project: Project, rel: str) -> None:
            base = project.root.resolve()
            target = (base / rel).resolve() if rel else base / "shared"
            if target != base and base not in target.parents:
                return self._json({"error": "outside project"}, 400)
            if target.is_dir():
                items = [{"path": str(p.relative_to(base)), "dir": p.is_dir(), "size": p.stat().st_size}
                         for p in sorted(target.iterdir()) if not p.name.startswith(".")]
                return self._json({"dir": str(target.relative_to(base)), "items": items})
            if target.is_file():
                text = target.read_bytes()[:400_000].decode("utf-8", errors="replace")
                return self._json({"file": str(target.relative_to(base)), "text": text})
            return self._json({"error": "not found"}, 404)

        def _sse(self, project: Project, after: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_ping = time.time()
            while True:
                events = project.log.read(after=after, limit=500)
                for e in events:
                    self.wfile.write(f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
                                     .encode())
                    after = e["seq"]
                if events:
                    self.wfile.flush()
                    continue
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                # The writer may be another process or another EventLog instance: poll the file.
                time.sleep(0.7)

        def do_POST(self):  # noqa: N802
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
            try:
                data = self._body()
                if parts == ["api", "projects"]:
                    goal = (data.get("goal") or "").strip()
                    if not goal:
                        return self._json({"error": "goal required"}, 400)
                    return self._json({"slug": runner.start(goal, data.get("name") or None)})
                if len(parts) == 4 and parts[:3] == ["api", "providers", "install"]:
                    return self._json(runner.installer.start(parts[3]))
                if len(parts) == 4 and parts[:3] == ["api", "providers", "uninstall"]:
                    from ..tools import TOOLS, uninstall

                    return self._json(uninstall(parts[3]) if parts[3] in TOOLS else {"ok": False})
                if parts == ["api", "providers", "codex", "login"]:
                    return self._json(runner.codex_login.start())
                if parts == ["api", "providers", "codex", "cancel"]:
                    return self._json(runner.codex_login.cancel())
                if parts == ["api", "providers", "codex", "logout"]:
                    from ..providers import logout_codex

                    return self._json(logout_codex(runner.config))
                if parts == ["api", "providers", "routing"]:
                    from ..providers import save_routing

                    return self._json(save_routing(runner.config, data))
                if parts == ["api", "update", "apply"]:
                    return self._json(runner.update_apply())
                if parts == ["api", "update", "rollback"]:
                    return self._json(runner.update_apply(rollback=True))
                if len(parts) == 4 and parts[3] == "stop":
                    return self._json({"stopped": runner.stop(parts[2])})
                if len(parts) == 4 and parts[3] == "resume":
                    runner.resume(parts[2])
                    return self._json({"resumed": parts[2]})
                if len(parts) == 6 and parts[3] == "tasks" and parts[5] == "comment":
                    t = human_comment(self._project(parts[2]), parts[4], data.get("body", ""))
                    return self._json(t)
                return self._json({"error": "not found"}, 404)
            except (KeyError, FileNotFoundError) as exc:
                return self._json({"error": str(exc)}, 404)

    return Handler


def serve(port: int = 8765, config: dict | None = None, host: str = "127.0.0.1") -> None:
    from ..project import load_config

    runner = Runner(config or load_config())
    runner.start_update_loop()
    httpd = ThreadingHTTPServer((host, port), make_handler(runner))
    httpd.daemon_threads = True
    from ..brand import banner

    print(banner(f"дашборд http://{host}:{port}"), flush=True)
    httpd.serve_forever()


__all__ = ["serve", "resume", "run_goal"]
