"""Dashboard server (stdlib only). Read APIs + SSE event stream + a few control actions.

Binds to 127.0.0.1 by default: the control endpoints start agents that execute
shell commands. Remote mode (`artel web --remote`, docs/REMOTE.md) binds beyond localhost and then every page,
/api/* call and event stream needs a login session (see auth.py); put it behind HTTPS.
"""

from __future__ import annotations

import ipaddress
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
    events = analytics._events(project)
    timelines = analytics.task_timelines(events)
    for t in snap["tasks"]:
        t["timeline"] = timelines.get(t["id"], [])
    stats = analytics.project_stats(project, live=project.slug in runner.active)
    live = project.slug in runner.active
    errors = [e for e in events if e["kind"] in ("llm/error", "manager/escalated", "manager/provider_switch",
                                                "manager/provider_limit", "provider/limited")][-5:]
    from ..tooling import effective_grants

    g = effective_grants(project, "worker")
    grants = {"read": g["read"], "write": g["write"], "refs": [str(r) for r in project.refs]}
    return {"slug": project.slug, "meta": project.meta, "board": snap, "workers": workers, "stats": stats,
            "grants": grants,
            "active": analytics.active_agents(events, live), "stage": analytics.stage_of(project, snap["tasks"]),
            "recent_problems": errors,
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
                return xff.split(",")[0].strip() or None
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

        def _gate(self, key: str, public: set) -> bool:
            """True when the request may proceed; otherwise the refusal has been sent."""
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
                if len(parts) == 4 and parts[:3] == ["api", "providers", "install"]:
                    return self._json(runner.installer.start(parts[3]))
                if len(parts) == 4 and parts[:3] == ["api", "providers", "uninstall"]:
                    from ..tools import TOOLS, uninstall

                    return self._json(uninstall(parts[3]) if parts[3] in TOOLS else {"ok": False})
                if parts == ["api", "providers", "codex", "login"]:
                    return self._json(runner.codex_login.start())
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
                    r = rank("как переключить провайдера при лимите", docs, runner.config)
                    return self._json({"how": r[0][2] if r else "lexical", "top": docs[r[0][0]] if r else "",
                                       "seconds": round(_t.time() - t0, 2)})
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
    from ..telegram import TelegramBot, token

    if token():
        runner.telegram = TelegramBot(runner).start()
        code = (runner.telegram.store.data.get("pair") or {}).get("code")
        if code:
            print(f"Telegram: отправьте боту /pair {code} (код действует 15 минут)", flush=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(runner, auth))
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
    finally:
        runner.kernel.shutdown(reason="server_stop")


__all__ = ["serve", "resume", "run_goal"]
