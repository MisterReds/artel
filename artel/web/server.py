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

from ..manager import human_comment, new_kernel, resume, run_goal
from ..project import Project, list_projects

STATIC = Path(__file__).parent / "index.html"


class Runner:
    """Projects driven by this server process."""

    def __init__(self, config: dict):
        self.config = config
        self.kernel = new_kernel(config)
        self.active: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

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
        stop = threading.Event()
        project = Project.open(slug, self.config)
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
    return {"slug": project.slug, "meta": project.meta, "board": snap, "workers": workers,
            "notes": project.prompt_notes(), "live": project.slug in runner.active, "seq": project.log.seq}


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
    httpd = ThreadingHTTPServer((host, port), make_handler(runner))
    httpd.daemon_threads = True
    print(f"artel dashboard on http://{host}:{port}")
    httpd.serve_forever()


__all__ = ["serve", "resume", "run_goal"]
