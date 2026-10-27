"""Dashboard server (stdlib only). Read APIs + SSE event stream + a few control actions.

Binds to 127.0.0.1 by default: the control endpoints start agents that execute
shell commands. Remote mode (`artel web --remote`, docs/REMOTE.md) binds beyond localhost and then every page,
/api/* call and event stream needs a login session (see auth.py); put it behind HTTPS.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

from ..httpd import LocalHTTPServer
from . import filesview
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import analytics, guardian
from ..humanize import happenings, localize_stats, localize_view, ru
from ..manager import human_comment, new_kernel, resume, run_goal
from ..project import Project, list_projects

STATIC = Path(__file__).parent / "index.html"
LOGIN = Path(__file__).parent / "login.html"
STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
# Reachable without a session in remote mode: the login page, its assets and the PWA shell files.
PUBLIC_GET = {"login", "sw.js", "manifest.webmanifest", "favicon.ico", "favicon.svg", "api/auth/status"}
PUBLIC_POST = {"api/auth/login", "api/auth/logout"}
REGISTRY = Path(__file__).parent.parent / "data" / "harnesses.json"


class Runner:
    """Projects driven by this server process."""

    def __init__(self, config: dict):
        self.config = config
        self.kernel = new_kernel(config)
        self.active: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.plugins_lock = threading.Lock()  # one plugin install / enable / remove at a time
        from ..updater import Updater

        self.updater = Updater(config=config)
        from ..providers import ClaudeLogin, CodexLogin

        self.codex_login = CodexLogin(config)
        self.claude_login = ClaudeLogin(config)
        from ..doctor import Jobs

        self.doctor = Jobs()
        from ..tools import Installer

        self.installer = Installer()
        from ..result_run import ResultRunner

        self.results = ResultRunner(config)  # results the owner started with «Запустить»; stopped on shutdown
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

    def resume(self, slug: str, via: str = "web", after_answer: bool = False) -> None:
        if slug in self.active:
            return
        from ..manager import continue_after_human

        stop = threading.Event()
        project = Project.open(slug, self.config)
        continue_after_human(project)
        project.log.append("owner/resume", via=via, **({"after_answer": True} if after_answer else {}))
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

    def stop(self, slug: str, via: str = "web") -> bool:
        with self.lock:
            ev = self.active.get(slug)
            first = bool(ev) and not ev.is_set()  # one owner/stop per stop, however many times it is pressed
            if ev:
                ev.set()
        if first:
            Project.open(slug, self.config).log.append("owner/stop", via=via)
        return bool(ev)


def project_view(project: Project, runner: Runner, user: bool = False) -> dict:
    """The project page's payload. `user`: system strings in Russian (humanize); the dev mode keeps them raw."""
    snap = project.board.snapshot()
    workers = []
    running = {t["assignee"]: t["id"] for t in snap["tasks"] if t["status"] == "running"}
    for p in project.personas():
        files = project.worker_files(p.name)
        done = sum(1 for t in snap["tasks"] if t["assignee"] == p.name and t["status"] == "done")
        workers.append({"name": p.name, "role": p.role, "files": files, "current": running.get(p.name),
                        "done": done, "skills": p.skills})
    events = analytics._events(project)
    timelines = analytics.task_timelines(events)
    risks = guardian.latest_risk(events)
    for t in snap["tasks"]:
        t["timeline"] = timelines.get(t["id"], [])
        if t["id"] in risks:
            t["risk"], t["risk_score"], t["risk_reason"] = (risks[t["id"]][k] for k in ("risk", "score", "reason"))
    stats = analytics.project_stats(project, live=project.slug in runner.active)
    live = project.slug in runner.active
    errors = [e for e in events if e["kind"] in ("llm/error", "manager/escalated", "manager/provider_switch",
                                                "manager/provider_limit", "provider/limited")][-5:]
    from ..tooling import effective_grants

    g = effective_grants(project, "worker")
    grants = {"read": g["read"], "write": g["write"], "refs": [str(r) for r in project.refs]}
    titles = {t["id"]: t for t in snap["tasks"]}
    view = {"slug": project.slug, "meta": project.meta, "board": snap, "workers": workers, "stats": stats,
            "grants": grants, "happenings": happenings(events, {k: {**v, "title": ru(v["title"]) if user else v["title"]}
                                                                for k, v in titles.items()}),
            "active": analytics.active_agents(events, live), "stage": analytics.stage_of(project, snap["tasks"]),
            "recent_problems": errors,
            "notes": project.prompt_notes(), "live": project.slug in runner.active, "seq": project.log.seq}
    return localize_view(view) if user else view


def evolution_view() -> list[dict]:
    from ..evolve.engine import git
    from ..project import artel_home

    out = []
    base = artel_home() / "evolution"
    for d in sorted(base.iterdir()) if base.exists() else []:
        sf = d / "state.json"
        if not sf.exists():
            continue
        state = json.loads(sf.read_text(encoding="utf-8"))
        lp = d / "ledger.jsonl"
        ledger = [json.loads(ln) for ln in lp.read_text(encoding="utf-8").splitlines() if ln.strip()] if lp.exists() else []
        absorb = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((d / "absorb").glob("*.json"))] if (d / "absorb").exists() else []
        branches = []
        if state.get("repo"):
            raw = git(Path(state["repo"]), "for-each-ref", "--format=%(refname:short)|%(objectname:short)|%(subject)",
                      "refs/heads/evolve/", check=False)
            branches = [dict(zip(("name", "sha", "subject"), ln.split("|", 2))) for ln in raw.splitlines() if ln]
            state["champion"] = git(Path(state["repo"]), "rev-parse", "--short", state.get("main", "HEAD"), check=False)
        diag = sorted(d.glob("gen*-diagnosis.md"))
        from ..evolve.supervisor import STALE_S, pid_alive

        hb = json.loads((d / "heartbeat.json").read_text(encoding="utf-8")) if (d / "heartbeat.json").exists() else {}
        alive = bool(hb) and (time.time() - float(hb.get("at", 0)) < STALE_S or pid_alive(hb.get("pid")))
        state["process"] = {"alive": alive, "pid": hb.get("pid"), "beat": hb.get("at")}
        out.append({"name": d.name, "state": state, "ledger": ledger, "absorb": absorb, "branches": branches,
                    "diagnosis": diag[-1].read_text(encoding="utf-8") if diag else ""})
    return out


def _loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.strip()).is_loopback
    except ValueError:
        return addr.strip() == "localhost"


def plugin_action(runner: Runner, parts: list, data: dict) -> tuple[dict, int]:
    """POST /api/plugins/install {source} and /api/plugins/<id>/{enable,disable,remove,update}.

    Changes apply to the server's live kernel at once (projects started afterwards see them)."""
    from .. import packages as P

    try:
        with runner.plugins_lock:
            if parts == ["install"]:
                source = (data.get("source") or "").strip()
                if not source:
                    return {"error": "укажите источник: git URL, пакет pip или путь"}, 400
                return P.install(source), 200
            if len(parts) != 2:
                return {"error": "not found"}, 404
            name, action = parts
            if name.startswith("artel.plugins."):
                if action not in ("enable", "disable"):
                    return {"error": "встроенный плагин можно только включить или выключить"}, 400
                return P.set_builtin(name, action == "enable", kernel=runner.kernel), 200
            if action == "enable":
                return P.enable(name, kernel=runner.kernel), 200
            if action == "disable":
                return P.disable(name, kernel=runner.kernel), 200
            if action == "remove":
                return P.remove(name, kernel=runner.kernel), 200
            if action == "update":
                return P.update(name, kernel=runner.kernel), 200
            return {"error": "not found"}, 404
    except P.PackageError as exc:
        return {"error": str(exc)}, 400


def open_folder(path: str) -> bool:
    """Show a folder in the system file manager, detached and without a shell; False when no opener exists."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
            return True
        opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
        if not opener:
            return False
        subprocess.Popen([opener, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


def make_handler(runner: Runner, auth=None):
    """`auth` (an auth.Auth) switches on remote mode: everything but PUBLIC_* needs a valid session cookie."""
    from .auth import COOKIE, RateLimited, cookie_value

    class Handler(BaseHTTPRequestHandler):
        server_version = "artel/0.1"

        def log_message(self, *args):  # quiet (and never logs request bodies, so never the token)
            pass

        def _json(self, obj, code: int = 200, headers: dict | None = None) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request body too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # --- remote mode: who is asking, over what, and are they logged in ---------------------------
        def _forwarded_for(self) -> str | None:
            """The client behind a reverse proxy on this host; ignored when the peer is not loopback."""
            xff = self.headers.get("X-Forwarded-For")
            if xff and _loopback(self.client_address[0]):
                # the hop the proxy itself appended (rightmost): the leftmost ones are whatever the client sent
                return xff.split(",")[-1].strip() or None
            return None

        def _client_ip(self) -> str:
            return self._forwarded_for() or self.client_address[0]

        def _secure(self) -> bool:
            return (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"

        def _local_client(self) -> bool:
            return _loopback(self._client_ip())

        def _sid(self) -> str | None:
            return cookie_value(self.headers.get("Cookie"))

        def _authed(self) -> bool:
            return auth is None or auth.valid(self._sid())

        def _auth_status(self) -> dict:
            return {"remote": auth is not None, "authed": self._authed(), "secure": self._secure(),
                    "insecure": auth is not None and not self._secure() and not self._local_client(),
                    **({"session_days": round(auth.ttl / 86400, 2)} if auth is not None else {})}

        def _redirect(self, where: str) -> None:
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _host_ok(self) -> bool:
            """Local mode answers only to its own name: a page whose domain was re-pointed at 127.0.0.1 (DNS
            rebinding) arrives with that domain in Host and is refused."""
            if auth is not None:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            name = host if host.endswith("]") else host.rsplit(":", 1)[0]
            return _loopback(name.strip("[]"))

        def _forged(self) -> str | None:
            """Why a state-changing request cannot be the dashboard's own, or None. Any web page the user visits
            can send a form or a no-cors fetch to 127.0.0.1; such requests cannot carry a JSON Content-Type without
            a CORS preflight (which this server never grants), and browsers name their real origin."""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "Content-Type must be application/json"
            origin = self.headers.get("Origin")
            if origin is not None:
                hosts = {(self.headers.get(h) or "").split(",")[0].strip().lower() for h in ("Host", "X-Forwarded-Host")}
                if urlparse(origin).netloc.lower() not in hosts - {""}:
                    return "cross-origin request"
            return None

        def _gate(self, key: str, public: set) -> bool:
            """True when the request may proceed; otherwise the refusal has been sent."""
            if not self._host_ok():
                self._json({"error": "unknown host"}, 421)
                return False
            if auth is None or key in public or key.startswith("static/") or self._authed():
                return True
            if key.startswith("api/"):
                self._json({"error": "login required"}, 401)
            else:
                self._redirect("/login")
            return False

        def _cookie(self, sid: str, max_age: int) -> str:
            secure = "; Secure" if self._secure() else ""
            return f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

        def _html(self, path: Path) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _login(self) -> None:
            try:
                token = str(self._body().get("token") or "").strip()
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            try:
                sid = auth.login(token, self._client_ip())
            except RateLimited as e:
                return self._json({"error": "too many attempts", "retry_after": e.retry_after}, 429,
                                  {"Retry-After": str(e.retry_after)})
            if not sid:
                return self._json({"error": "wrong token"}, 401)
            return self._json({"ok": True}, 200, {"Set-Cookie": self._cookie(sid, int(auth.ttl))})

        def _project(self, slug: str) -> Project:
            return Project.open(slug, runner.config)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            q = parse_qs(url.query)
            if not self._gate("/".join(parts), PUBLIC_GET):
                return
            try:
                if not parts:
                    self._html(STATIC)
                elif parts == ["login"]:
                    if auth is None or self._authed():
                        self._redirect("/")
                    else:
                        self._html(LOGIN)
                elif parts == ["api", "auth", "status"]:
                    self._json(self._auth_status())
                elif parts == ["sw.js"]:
                    # served from the root so its scope is the whole app; never cached by the HTTP cache
                    self._static("sw.js", {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
                elif parts == ["manifest.webmanifest"]:
                    self._static("manifest.webmanifest", {"Cache-Control": "no-cache"})
                elif parts == ["api", "projects"]:
                    self._json([{**p, "live": p["slug"] in runner.active} for p in list_projects()])
                elif parts == ["api", "portfolio"]:
                    ps = analytics.portfolio(runner.config, live=set(runner.active))
                    self._json([localize_stats(p) for p in ps] if q.get("mode") == ["user"] else ps)
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
                elif parts == ["api", "telegram"]:
                    from ..telegram import status as tg_status

                    self._json(tg_status(runner.config))
                elif parts == ["api", "ready"]:
                    from ..runtimes import detect_runtime

                    self._json({"runtime": detect_runtime(runner.config)})
                elif parts == ["api", "providers"]:
                    from ..providers import overview

                    self._json({**overview(runner.config), "codex_login": runner.codex_login.state,
                                "claude_login": runner.claude_login.state,
                                "installs": runner.installer.state})
                elif parts == ["api", "providers", "models"]:
                    from ..providers import model_catalog

                    self._json({"models": model_catalog(runner.config, q.get("runtime", [""])[0])})
                elif parts == ["api", "timezone"]:
                    from ..tz import info

                    self._json(info(runner.config))
                elif parts == ["api", "providers", "codex", "login"]:
                    self._json(runner.codex_login.state)
                elif parts == ["api", "reports"]:
                    from .. import reports

                    self._json({"reports": reports.load(100), "settings": reports.settings(runner.config),
                                "github": reports.github_ready()})
                elif len(parts) == 3 and parts[:2] == ["api", "reports"]:
                    from .. import reports

                    rep = reports.get(parts[2])
                    self._json({**rep, "markdown": reports.markdown(rep)} if rep else {"error": "not found"}, 200 if rep else 404)
                elif len(parts) == 3 and parts[:2] == ["api", "doctor"]:
                    self._json(runner.doctor.get(parts[2]))
                elif parts == ["api", "providers", "claude", "login"]:
                    self._json(runner.claude_login.state)
                elif parts == ["api", "update"]:
                    self._json(runner.update_status(fetch=q.get("check", ["0"])[0] == "1"))
                elif parts == ["api", "evolution"]:
                    self._json(evolution_view())
                elif parts == ["api", "plugins"]:
                    from ..kernel import LIFECYCLE_EVENTS
                    from ..packages import PERMISSIONS, overview

                    self._json({"plugins": overview(runner.config, runner.kernel), "permissions": PERMISSIONS,
                                "events": LIFECYCLE_EVENTS})
                elif parts == ["api", "skills"]:
                    self._json(runner.kernel.get("skills").list())
                elif len(parts) == 3 and parts[:2] == ["api", "skills"]:
                    self._json({"name": parts[2], "text": runner.kernel.get("skills").view(parts[2]),
                                "ledger": runner.kernel.get("skills").ledger(parts[2])})
                elif len(parts) == 3 and parts[:2] == ["api", "projects"]:
                    self._json({**project_view(self._project(parts[2]), runner, user=q.get("mode") == ["user"]),
                                "remote": auth is not None})
                elif len(parts) == 4 and parts[3] == "run":
                    self._json(runner.results.status(self._project(parts[2])))
                elif len(parts) == 4 and parts[3] == "archive":
                    self._archive(self._project(parts[2]))
                elif len(parts) == 4 and parts[3] == "events":
                    self._sse(self._project(parts[2]), int(q.get("after", ["0"])[0]))
                elif len(parts) == 4 and parts[3] == "files":
                    self._files(self._project(parts[2]), q.get("path", [""])[0])
                # --- file viewer (filesview.py): shared/ only, symlinks out of it refused ---
                elif len(parts) == 4 and parts[3] in ("tree", "file", "raw"):
                    getattr(self, "_fv_" + parts[3])(self._project(parts[2]), q.get("path", [""])[0],
                                                     q.get("download") == ["1"])
                elif len(parts) >= 5 and parts[3] == "preview":
                    self._fv_preview(self._project(parts[2]), "/".join(parts[4:]))
                else:
                    self._json({"error": "not found"}, 404)
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _static(self, rel: str, headers: dict | None = None) -> None:
            root = STATIC_DIR.resolve()
            f = (root / rel).resolve()
            if root not in f.parents or not f.is_file():
                return self._json({"error": "not found"}, 404)
            types = {".svg": "image/svg+xml", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
                     ".png": "image/png", ".js": "text/javascript; charset=utf-8",
                     ".webmanifest": "application/manifest+json"}
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", types.get(f.suffix, "application/octet-stream"))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in ({"Cache-Control": "public, max-age=86400"} | (headers or {})).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _files(self, project: Project, rel: str) -> None:
            base = project.root.resolve()
            target = (base / rel).resolve() if rel else base / "shared"
            if target != base and base not in target.parents:
                return self._json({"error": "outside project"}, 400)
            if target.is_dir():
                items = [{"path": p.relative_to(base).as_posix(), "dir": p.is_dir(), "size": p.stat().st_size}
                         for p in sorted(target.iterdir()) if not p.name.startswith(".")]
                return self._json({"dir": target.relative_to(base).as_posix(), "items": items})
            if target.is_file():
                text = target.read_bytes()[:400_000].decode("utf-8", errors="replace")
                return self._json({"file": target.relative_to(base).as_posix(), "text": text})
            return self._json({"error": "not found"}, 404)

        # --- file viewer ------------------------------------------------------------------------------------
        def _fv_tree(self, project: Project, _rel: str, _dl: bool) -> None:
            since = filesview.last_start(project.board.list())
            self._json({**filesview.tree(Path(project.shared), since), "root": str(Path(project.shared).resolve())})

        def _fv_file(self, project: Project, rel: str, _dl: bool) -> None:
            out = filesview.preview(Path(project.shared), rel)
            self._json(out if out else {"error": "not found"}, 200 if out else 404)

        def _fv_send(self, project: Project, rel: str, headers: dict, ctype: str | None = None,
                     download: bool = False) -> None:
            """One file's bytes (a single Range honoured, for video). Every response is sandboxed and nosniff."""
            real = filesview.resolve(Path(project.shared), rel)
            if real is None or not real.is_file():
                return self._json({"error": "not found"}, 404)
            size = real.stat().st_size
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)) and size:
                a, b = m.groups()
                start, end = (size - int(b), size - 1) if not a else (int(a), int(b) if b else size - 1)
                start, end = max(0, start), min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", ctype or filesview.content_type(real))
            self.send_header("Content-Length", str(end - start + 1 if size else 0))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k, v in headers.items():
                self.send_header(k, v)
            if download:
                self.send_header("Content-Disposition", filesview.attachment(real.name))
            self.end_headers()
            with open(real, "rb") as f:
                f.seek(start)
                left = end - start + 1 if size else 0
                while left > 0:
                    chunk = f.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _fv_raw(self, project: Project, rel: str, download: bool) -> None:
            """Bytes for <img>/<video>/<embed> and downloads. Page-like types are shown as plain text here: the
            only place HTML runs is /preview, in a sandbox."""
            real = filesview.resolve(Path(project.shared), rel)
            ctype, headers = None, {"Content-Security-Policy": "sandbox"}
            if real is not None and real.suffix.lower() in (".html", ".htm", ".xml", ".xhtml"):
                ctype = "text/plain; charset=utf-8"
            if real is not None and real.suffix.lower() == ".pdf" and not download:
                headers = {}  # the browser's PDF viewer does not run inside a sandboxed document; a PDF has no script
            self._fv_send(project, rel, headers, ctype, download)

        def _fv_preview(self, project: Project, rel: str) -> None:
            """The file as a web page would get it (HTML, its CSS and scripts), in an opaque origin: the
            `sandbox allow-scripts` policy keeps its scripts away from the dashboard's cookies, storage and DOM."""
            self._fv_send(project, rel, {"Content-Security-Policy": "sandbox allow-scripts"})

        def _archive(self, project: Project) -> None:
            """shared/ as a zip, streamed to the client; symlinks that point outside shared/ are skipped."""
            import tempfile
            import zipfile

            base = Path(project.shared).resolve()
            with tempfile.TemporaryFile() as tmp:  # a zip needs seeking; the response is then streamed from disk
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                    for dirpath, dirnames, files in os.walk(base):
                        dirnames[:] = [d for d in dirnames if (Path(dirpath) / d).resolve().is_relative_to(base)]
                        for name in files:
                            f = Path(dirpath) / name
                            real = f.resolve()
                            if real.is_file() and real.is_relative_to(base):
                                z.write(real, f.relative_to(base).as_posix())
                size = tmp.tell()
                tmp.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", filesview.attachment(project.slug + ".zip"))
                self.send_header("Content-Length", str(size))
                self.end_headers()
                shutil.copyfileobj(tmp, self.wfile, 64 * 1024)

        def _open_folder(self, project: Project) -> None:
            if auth is not None:
                return self._json({"error": "в удалённом режиме папку на сервере не открыть: скачайте архив"}, 403)
            path = str(Path(project.shared).resolve())
            return self._json({"opened": open_folder(path), "path": path})

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
                if not self._authed():  # session expired or revoked (token rotated): end the stream
                    return
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                # The writer may be another process or another EventLog instance: poll the file.
                time.sleep(0.7)

        def do_POST(self):  # noqa: N802
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
            forged = self._forged()
            if forged:
                return self._json({"error": forged}, 403)
            if not self._gate("/".join(parts), PUBLIC_POST):
                return
            if parts == ["api", "auth", "login"]:
                return self._login() if auth is not None else self._json({"ok": True, "remote": False})
            if parts == ["api", "auth", "logout"]:
                if auth is not None:
                    auth.logout(self._sid())
                return self._json({"ok": True}, 200, {"Set-Cookie": self._cookie("", 0)})
            try:
                data = self._body()
                if parts == ["api", "projects"]:
                    goal = (data.get("goal") or "").strip()
                    if not goal:
                        return self._json({"error": "goal required"}, 400)
                    return self._json({"slug": runner.start(goal, data.get("name") or None)})
                if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "run":
                    proj = self._project(parts[2])
                    if data.get("action") == "start":
                        from ..result_run import run_spec, spec_display

                        spec = run_spec(proj)
                        if spec and spec.get("command"):  # code the workers wrote: the owner confirms the exact command
                            shown = spec_display(spec)
                            if proj.meta.get("run_confirmed") != shown:
                                if data.get("confirmed") is not True or data.get("command", shown) != shown:
                                    return self._json({"error": "нужно подтвердить команду запуска", "command": shown,
                                                       "confirm": True}, 428)
                                proj.set_meta(run_confirmed=shown)
                        try:
                            info = runner.results.start(proj)
                        except ValueError as exc:
                            return self._json({"error": str(exc)}, 400)
                        if info.get("busy"):
                            return self._json({"error": "Уже запускается", "busy": True}, 409)
                        return self._json(runner.results.status(proj))
                    if data.get("action") == "stop":
                        runner.results.stop(proj)
                        return self._json(runner.results.status(proj))
                    return self._json({"error": "action: start | stop"}, 400)
                if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "open-folder":
                    return self._open_folder(self._project(parts[2]))
                if len(parts) == 4 and parts[:3] == ["api", "providers", "install"]:
                    return self._json(runner.installer.start(parts[3]))
                if len(parts) == 4 and parts[:3] == ["api", "providers", "uninstall"]:
                    from ..tools import TOOLS, uninstall

                    return self._json(uninstall(parts[3]) if parts[3] in TOOLS else {"ok": False})
                if parts == ["api", "providers", "codex", "login"]:
                    return self._json(runner.codex_login.start())
                if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[3] == "send":
                    from .. import reports

                    return self._json(reports.send_github(parts[2]))
                if parts == ["api", "reports", "settings"]:
                    from .. import reports
                    from ..project import artel_home, deep_merge

                    if data.get("github") not in ("ask", "auto", "off") or not isinstance(data.get("telegram", True), bool):
                        return self._json({"error": "github: ask | auto | off"}, 400)
                    upd = {"github": data["github"], "telegram": bool(data.get("telegram", True))}
                    path = artel_home() / "artel.yaml"
                    import yaml as _yaml

                    cur = (_yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
                    cur = deep_merge(cur, {"reports": upd})
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(_yaml.safe_dump(cur, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    runner.config["reports"] = {**(runner.config.get("reports") or {}), **upd}
                    return self._json(reports.settings(runner.config))
                if parts == ["api", "doctor"]:
                    problem = (data.get("problem") or "").strip()
                    if not problem:
                        return self._json({"error": "problem required"}, 400)
                    proj = self._project(data["slug"]) if data.get("slug") else None
                    return self._json(runner.doctor.start(runner.config, problem, proj, data.get("task_id") or None,
                                                          data.get("exclude_runtime") or None))
                if parts == ["api", "providers", "claude", "login"]:
                    return self._json(runner.claude_login.start())
                if parts == ["api", "providers", "claude", "code"]:
                    return self._json(runner.claude_login.submit(data.get("code", "")))
                if parts == ["api", "providers", "claude", "cancel"]:
                    return self._json(runner.claude_login.cancel())
                if parts == ["api", "providers", "claude", "logout"]:
                    from ..providers import logout_claude

                    return self._json(logout_claude(runner.config))
                if parts == ["api", "providers", "codex", "cancel"]:
                    return self._json(runner.codex_login.cancel())
                if len(parts) == 4 and parts[:3] == ["api", "providers", "unlimit"]:
                    from .. import limits

                    limits.clear(parts[3])
                    return self._json({"ok": True})
                if parts == ["api", "providers", "codex", "logout"]:
                    from ..providers import logout_codex

                    return self._json(logout_codex(runner.config))
                if parts == ["api", "telegram", "pair"]:
                    from ..telegram import Store, token

                    if not token():
                        return self._json({"error": "ARTEL_TELEGRAM_TOKEN is not set"}, 400)
                    store = runner.telegram.store if getattr(runner, "telegram", None) else Store()
                    return self._json({"code": store.new_code()})
                if parts == ["api", "providers", "routing"]:
                    from ..providers import save_routing

                    try:
                        return self._json(save_routing(runner.config, data))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if parts == ["api", "providers", "embeddings"]:
                    from ..providers import save_embeddings

                    from .. import embeddings

                    embeddings.reset()
                    try:
                        return self._json(save_embeddings(runner.config, data))
                    except (ValueError, TypeError) as e:
                        return self._json({"error": str(e)}, 400)
                if parts == ["api", "providers", "embeddings", "test"]:
                    from ..retrieval import rank

                    docs = ["When Claude hits its usage limit, the work moves to Codex.",
                            "Рецепт борща со сметаной и чесноком."]
                    import time as _t

                    t0 = _t.time()
                    from .. import embeddings

                    embeddings.reset()  # a manual check always tries again
                    r = rank("как переключить провайдера при лимите", docs, runner.config)
                    return self._json({"how": r[0][2] if r else "lexical", "top": docs[r[0][0]] if r else "",
                                       "seconds": round(_t.time() - t0, 2), "error": embeddings.last_error(runner.config)})
                if parts == ["api", "providers", "timezone"]:
                    from ..providers import save_timezone

                    try:
                        return self._json(save_timezone(runner.config, data.get("timezone", "")))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if parts[:2] == ["api", "plugins"] and len(parts) in (3, 4):
                    return self._json(*plugin_action(runner, parts[2:], data))
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
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

    return Handler


def serve(port: int = 8765, config: dict | None = None, host: str | None = None,
          remote: bool | None = None) -> None:
    """Local mode binds 127.0.0.1 with no login. Remote mode (flag or config `web.remote`) binds `host`
    (default config `web.host`, 0.0.0.0) and requires a session created with the access token."""
    from ..project import load_config

    config = config or load_config()
    web = config.get("web") or {}
    remote = bool(web.get("remote")) if remote is None else remote
    auth = token = None
    if remote:
        from .auth import Auth

        host = host or web.get("host") or "0.0.0.0"
        auth = Auth(session_days=web.get("session_days", 30))
        token = auth.ensure_token()
    else:
        host = host or "127.0.0.1"
        if not _loopback(host):
            raise SystemExit(f"artel web: {host} is beyond localhost; that needs login - use `artel web --remote`"
                             " (see docs/REMOTE.md)")
    runner = Runner(config)
    runner.start_update_loop()
    if (config.get("evolve") or {}).get("auto_resume", True):
        from ..evolve.supervisor import Supervisor
        from ..project import artel_home

        runner.supervisor = Supervisor(artel_home() / "evolution").start()
    from ..telegram import TelegramBot, token

    if token():
        runner.telegram = TelegramBot(runner).start()
        code = (runner.telegram.store.data.get("pair") or {}).get("code")
        if code:
            print(f"Telegram: отправьте боту /pair {code} (код действует 15 минут)", flush=True)
    class Server(LocalHTTPServer):
        def handle_error(self, request, client_address):  # an unhandled dashboard error becomes a report too
            import traceback

            tb = traceback.format_exc()
            super().handle_error(request, client_address)
            try:
                from .. import reports

                reports.record("exception", tb.strip().splitlines()[-1][:200], tb, config=runner.config)
            except Exception:  # noqa: BLE001
                pass

    httpd = Server((host, port), make_handler(runner, auth))
    httpd.daemon_threads = True
    from ..brand import banner

    print(banner(f"дашборд http://{host}:{port}" + (" (удалённый режим, вход по токену)" if remote else "")),
          flush=True)
    if remote:
        if token:  # printed to the terminal once; never written to a file or a log
            print("Токен доступа (показывается один раз, сохраните его; хранится только хэш):\n"
                  f"  {token}", flush=True)
        else:
            print("Вход по ранее выпущенному токену. Новый токен (старые сессии закроются): "
                  "artel web token --new", flush=True)
        print("Без HTTPS токен и сессия идут по сети открытым текстом - см. docs/REMOTE.md", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # Ctrl+C is how a person stops the dashboard: say so, no traceback
        live = sorted(getattr(runner, "active", {}) or {})
        print("\nArtel остановлен." + (f" Незавершённые проекты ({len(live)}) продолжатся кнопкой «Продолжить» "
                                       "после следующего `artel web`." if live else ""), flush=True)
    finally:
        httpd.server_close()
        runner.results.stop_all()
        runner.kernel.shutdown(reason="server_stop")


__all__ = ["serve", "resume", "run_goal"]
