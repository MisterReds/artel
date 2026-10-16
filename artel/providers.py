"""What Artel can talk to, and how it is signed in — the data behind the dashboard's "Подключения" page.

- Codex (ChatGPT subscription): device-code login driven through the official CLI (`codex login --device-auth`);
  the code and link are shown in the dashboard, the CLI stores and refreshes the tokens itself.
- Claude Code (Claude subscription): status of the installed CLI; login happens in the CLI.
- DeepSeek Harness: whether the SDK is installed, which model it runs (chosen in dsh itself) and whether that model has a credential; its own settings page.
- API keys: only whether the configured environment variable is set — values are never read out or stored.
- ACP agents (Gemini CLI, Claude Code / Codex via ACP adapters, goose, OpenHands…): which presets are installed;
  login happens in each agent's own CLI.
- Local System One model: whether its server answers.
- Routing: which runtime and model each role uses (saved to ARTEL_HOME/artel.yaml).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

from .runtimes.codex import codex_env, codex_settings
from .tools import installed_by_artel, resolve, terminate_tree

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ROLES = ["manager", "planner", "worker", "tester", "reviewer", "auditor", "librarian", "refiner", "evolver", "absorber", "doctor", "intake"]
RUNTIMES = ["auto", "native", "claude_code", "codex", "dsh", "acp"]
KEY_VARS = {"ANTHROPIC_API_KEY": "Anthropic API", "OPENAI_API_KEY": "OpenAI API", "OPENROUTER_API_KEY": "OpenRouter",
            "DEEPSEEK_API_KEY": "DeepSeek"}


def _run(args: list, env: Optional[dict] = None, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
        return p.returncode, ANSI.sub("", (p.stdout + p.stderr)).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


class CodexLogin:
    """One device-code login at a time; the CLI process waits for the browser confirmation in the background."""

    def __init__(self, config: dict):
        self.config = config
        self.state: dict = {"phase": "idle"}
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()

    def start(self, wait_s: float = 20.0) -> dict:
        cfg = codex_settings(self.config)
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return self.state
            binary = resolve(cfg["binary"])
            if not binary:
                self.state = {"phase": "error", "error": "Codex CLI не установлен — сначала «Установить»"}
                return self.state
            self.proc = subprocess.Popen([binary, "login", "--device-auth"], stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, env=codex_env(cfg), bufsize=1, encoding="utf-8", errors="replace")
            self.state = {"phase": "starting", "since": time.time()}
        found = threading.Event()

        def reader():
            text = ""
            for line in self.proc.stdout:
                text += ANSI.sub("", line)
                url = re.search(r"https://\S+/device\S*", text)
                code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b", text)
                if url and code and self.state.get("phase") == "starting":
                    m = re.search(r"expires in (\d+) minutes", text)
                    self.state = {"phase": "waiting", "url": url.group(0), "code": code.group(1),
                                  "expires_at": time.time() + 60 * int(m.group(1) if m else 15)}
                    found.set()
            rc = self.proc.wait()
            if rc == 0:
                self.state = {"phase": "done", "at": time.time()}
                _invalidate()
            elif self.state.get("phase") != "cancelled":
                self.state = {"phase": "error", "error": text.strip()[-600:] or f"exit {rc}"}
            found.set()

        threading.Thread(target=reader, daemon=True, name="codex-login").start()
        found.wait(wait_s)
        return self.state

    def cancel(self) -> dict:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.state = {"phase": "cancelled"}
                terminate_tree(self.proc)
        return self.state


class ClaudeLogin:
    """Claude subscription login through the official CLI (`claude auth login --claudeai`): it prints an authorize
    link; after signing in, the page shows a code that the person pastes into the dashboard, which hands it to the
    waiting CLI on stdin. The CLI stores and refreshes the tokens; Artel never sees them."""

    def __init__(self, config: dict):
        self.config = config
        self.state: dict = {"phase": "idle"}
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()

    def _binary(self) -> Optional[str]:
        return resolve((self.config.get("claude_code") or {}).get("binary", "claude"))

    def start(self, wait_s: float = 20.0) -> dict:
        from .runtimes.claude_code import child_env

        with self.lock:
            if self.proc and self.proc.poll() is None:
                return self.state
            binary = self._binary()
            if not binary:
                self.state = {"phase": "error", "error": "Claude Code CLI не установлен — сначала «Установить»"}
                return self.state
            env = {**child_env(), "BROWSER": "true"}  # no browser on a server: the link is shown in the dashboard
            self.proc = subprocess.Popen([binary, "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                         bufsize=1, encoding="utf-8", errors="replace")
            self.state = {"phase": "starting", "since": time.time()}
        found = threading.Event()

        def reader():
            text = ""
            for line in self.proc.stdout:
                text += ANSI.sub("", line)
                url = re.search(r"https://\S+/oauth/authorize\S*", text)
                if url and self.state.get("phase") == "starting":
                    self.state = {"phase": "waiting", "url": url.group(0), "since": time.time()}
                    found.set()
            rc = self.proc.wait()
            if rc == 0:
                self.state = {"phase": "done", "at": time.time()}
                _invalidate()
            elif self.state.get("phase") != "cancelled":
                lines = [ln for ln in text.strip().splitlines() if "authorize" not in ln]
                self.state = {"phase": "error", "error": "\n".join(lines)[-600:] or f"exit {rc}"}
            found.set()

        threading.Thread(target=reader, daemon=True, name="claude-login").start()
        found.wait(wait_s)
        return self.state

    def submit(self, code: str, wait_s: float = 30.0) -> dict:
        code = (code or "").strip()
        with self.lock:
            if not (self.proc and self.proc.poll() is None and self.state.get("phase") == "waiting"):
                return {**self.state, "error": self.state.get("error") or "вход не начат"}
            if not code or any(c.isspace() for c in code):
                return {**self.state, "error": "вставьте код целиком, одной строкой"}
            self.state = {"phase": "checking"}
            self.proc.stdin.write(code + "\n")
            self.proc.stdin.flush()
        try:
            self.proc.wait(wait_s)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.2)  # let the reader record the outcome
        return self.state

    def cancel(self) -> dict:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.state = {"phase": "cancelled"}
                terminate_tree(self.proc)
        return self.state


def logout_claude(config: dict) -> dict:
    from .runtimes.claude_code import child_env

    binary = resolve((config.get("claude_code") or {}).get("binary", "claude"))
    if not binary:
        return {"ok": False, "detail": "Claude Code CLI не установлен"}
    rc, text = _run([binary, "auth", "logout"], env=child_env())
    _invalidate()
    return {"ok": rc == 0, "detail": text}


def _invalidate() -> None:
    """A login or logout changes who is connected: auto must not keep using its cached answer."""
    from .runtimes import _auto_cache

    _auto_cache.clear()


def codex_status(config: dict) -> dict:
    cfg = codex_settings(config)
    binary = resolve(cfg["binary"])
    if not binary:
        return {"installed": False, "logged_in": False, "detail": "не установлен"}
    rc, text = _run([binary, "login", "status"], env=codex_env(cfg))
    logged = rc == 0 and "not logged in" not in text.lower()
    method = "ChatGPT" if "chatgpt" in text.lower() else ("API key" if "api key" in text.lower() else "")
    _, ver = _run([binary, "--version"], env=codex_env(cfg))
    ver = next((ln for ln in ver.splitlines() if ln.lower().startswith("codex")), ver.splitlines()[-1] if ver else "")
    return {"installed": True, "by_artel": installed_by_artel("codex"), "version": ver, "logged_in": logged,
            "method": method,
            "detail": next((ln for ln in reversed(text.splitlines()) if "WARNING" not in ln), ""), "model": cfg.get("model") or "по умолчанию аккаунта",
            "sandbox": _codex_sandbox(cfg) if logged else ""}


def _codex_sandbox(cfg: dict) -> str:
    from .runtimes.codex import sandbox_mode, sandbox_note

    mode = sandbox_mode(cfg)
    return {"sandbox": "песочница Codex работает",
            "sandbox+network": "песочница Codex работает (с доступом к сети)",
            "none": "песочница Codex на этой машине не запускается — команды идут без неё, действуют права и политика "
                    "команд Artel" + (f" ({sandbox_note(cfg)[-160:]})" if sandbox_note(cfg) else "")}.get(mode, mode)


def logout_codex(config: dict) -> dict:
    cfg = codex_settings(config)
    rc, text = _run([resolve(cfg["binary"]) or cfg["binary"], "logout"], env=codex_env(cfg))
    _invalidate()
    return {"ok": rc == 0, "detail": text}


def claude_code_status(config: dict) -> dict:
    cc = config.get("claude_code") or {}
    from .runtimes.claude_code import child_env

    binary = resolve(cc.get("binary", "claude"))
    if not binary:
        return {"installed": False, "logged_in": False, "detail": "не установлен"}
    _, ver = _run([binary, "--version"], env=child_env())

    rc, text = _run([binary, "auth", "status"], env=child_env())
    try:
        st = json.loads(text[text.index("{"):]) if rc == 0 else {}
    except ValueError:
        st = {}
    method = {"oauth_token": "подписка Claude", "claude.ai": "подписка Claude", "api_key": "API key"}.get(
        st.get("authMethod", ""), st.get("authMethod", ""))
    return {"installed": True, "by_artel": installed_by_artel("claude"), "version": ver.splitlines()[-1] if ver else "",
            "logged_in": bool(st.get("loggedIn")),
            "method": method, "model": cc.get("model", "sonnet"),
            "detail": "вход: кнопка «Войти» ниже или `artel providers login claude`"}


def dsh_status(config: dict) -> dict:
    """DeepSeek Harness: installed, which model it will run (chosen in dsh itself), whether that model's provider has
    a credential (names only, never values), and dsh's own settings page."""
    from .runtimes.dsh import access, dsh_home, dsh_settings, provider_keys, sdk_available

    cfg = dsh_settings(config)
    installed = sdk_available(cfg)
    version = ""
    if installed:
        try:
            from importlib.metadata import version as _v

            version = _v("deepseek-harness-sdk")
        except Exception:  # noqa: BLE001 - a custom SDK module without distribution metadata
            version = ""
    acc = access(cfg) if installed else {"ok": False, "provider": "", "model": None, "source": "", "why": ""}
    home = dsh_home(cfg)
    shown = str(home).replace(str(Path.home()), "~", 1)
    return {"installed": installed, "by_artel": installed_by_artel("dsh"), "version": version,
            "logged_in": bool(installed and acc["ok"]), "provider": acc.get("provider") or "",
            "model": acc.get("model") or "", "source": acc.get("source") or "", "providers": sorted(provider_keys(cfg)),
            "home": shown, "profile": cfg.get("profile"), "plugins": [str(p) for p in cfg.get("plugins") or []],
            "settings": DSH_GUI.status(), "detail": acc.get("why") or ""}


class _DshGui:
    """DeepSeek Harness's own settings page (`dsh web`) on the shared home, started on request from the dashboard:
    models, keys, other providers and sign-ins are configured there, by dsh, not by Artel."""

    def __init__(self):
        self.proc = None
        self.url = ""
        self.error = ""
        self.lock = threading.Lock()

    def status(self) -> dict:
        alive = self.proc is not None and self.proc.poll() is None
        return {"running": alive, "url": self.url if alive else "", "error": "" if alive else self.error}

    def start(self, config: dict) -> dict:
        from .runtimes.dsh import dsh_home, dsh_settings, launcher

        with self.lock:
            if self.proc is not None and self.proc.poll() is None and self.url:
                return self.status()
            cfg = dsh_settings(config)
            home = dsh_home(cfg)
            home.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "DSH_HOME": str(home)}
            # the newest dsh first: one installed with npm, the SDK's bundled runtime, then npx (needs Node.js)
            candidates = []
            if cfg.get("binary") or resolve("dsh"):
                candidates.append([resolve(cfg.get("binary") or "dsh") or "dsh"])
            sdk = launcher({**cfg, "binary": None})
            if sdk and sdk not in candidates:
                candidates.append(sdk)
            if resolve("npx"):
                candidates.append([resolve("npx"), "-y", "@deepseek-ai/dsh@latest"])
            self.url, self.error = "", ""
            tails = []
            for argv in candidates:
                url, tail = self._try(argv, env)
                if url:
                    self.url = url
                    return self.status()
                tails.append(f"{os.path.basename(argv[0])}: {tail}")
            self.error = ("Настройки DeepSeek Harness не открылись" + (":\n" + "\n".join(tails) if tails else
                          ": DeepSeek Harness не установлен") + "\nЗапустите вручную: "
                          f"DSH_HOME={home} npx @deepseek-ai/dsh web")
            return self.status()

    def _try(self, argv: list, env: dict) -> tuple:
        try:
            proc = subprocess.Popen([*argv, "web", "--no-open", "--port", "0"], env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
                                    errors="replace", start_new_session=True)
        except OSError as e:
            return "", str(e)
        lines: list = []
        deadline = time.time() + 180
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip())
            m = re.search(r"https?://\S+", line)
            if m and "dsh web" in line:
                self.proc = proc
                threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
                return m.group(0), ""
        proc.kill()
        err = next((ln for ln in reversed(lines) if "Error" in ln or "error" in ln), lines[-1] if lines else "")
        return "", err.strip()[:300]

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if os.name == "posix":  # npx → node: the whole process group started for it
                import signal

                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except OSError:
                    pass
            else:
                terminate_tree(self.proc)


DSH_GUI = _DshGui()


def runtimes(config: dict) -> list[str]:
    """Selectable runtimes: the base ones plus one ``acp:<preset>`` per ACP agent preset."""
    from .runtimes.acp import presets

    return RUNTIMES + [f"acp:{n}" for n in presets(config)]


def acp_status(config: dict) -> dict:
    """ACP presets with their command lines and whether the command is installed; nothing is started."""
    from .runtimes.acp import AcpError, acp_settings, presets

    a = config.get("acp") or {}
    items = [{"name": n, "command": p["command"], "installed": bool(p["command"] and resolve(p["command"][0]))}
             for n, p in presets(config).items()]
    try:
        eff = acp_settings(config)
        command, err = eff["command"], ""
    except AcpError as e:
        command, err = [], str(e)
    return {"agent": a.get("agent", "gemini"), "command": command, "custom": bool(a.get("command")),
            "installed": bool(command and resolve(command[0])), "presets": items, "error": err}


def api_keys(config: dict) -> list[dict]:
    names = dict(KEY_VARS)
    oc = config.get("openai_compat") or {}
    if oc.get("api_key_env"):
        names.setdefault(oc["api_key_env"], f"OpenAI-совместимый ({oc.get('base_url', '')})")
    dec = config.get("decide") or {}
    if dec.get("api_key_env"):
        names.setdefault(dec["api_key_env"], "System One")
    return [{"env": k, "label": v, "set": bool(os.environ.get(k))} for k, v in names.items()]


def system_one_status(config: dict) -> dict:
    """Whether a System One is connected (plugin service or HTTP endpoint) and which decision points are on."""
    dec = config.get("decide") or {}
    plugins = [p for p in config.get("plugins", []) if "system_one" in p or "system1" in p]
    http = dec.get("backend") == "http" and bool(dec.get("url"))
    name = plugins[0] if plugins else (dec.get("url") if http else "")
    return {"connected": bool(plugins or http), "name": name,
            "points": [k for k, v in (dec.get("points") or {}).items() if v]}


CLAUDE_MODELS = [{"id": "sonnet", "name": "Sonnet (последняя)"}, {"id": "opus", "name": "Opus (последняя)"},
                 {"id": "haiku", "name": "Haiku (последняя)"}]
TOGGLABLE = ["claude_code", "codex", "dsh", "native"]


def model_catalog(config: dict, runtime: str) -> list[dict]:
    """Models a runtime can use: Codex asks its CLI (`codex debug models`), Claude Code takes the aliases its CLI
    resolves to the latest model of each family; [] when unknown (the field stays free text)."""
    if runtime == "codex":
        from .runtimes.codex import model_catalog as codex_models

        return codex_models(codex_settings(config))
    if runtime == "claude_code":
        return claude_models(config)
    return []


_claude_cache: dict = {}


def claude_models(config: dict, ttl: float = 600.0) -> list[dict]:
    """Claude Code has no model-listing command; its `--help` names the aliases it resolves to the latest model of
    each family (e.g. 'fable', 'opus', 'sonnet'). Those come first, then the other known aliases; any full model
    name is accepted as a custom value."""
    from .runtimes.claude_code import child_env

    binary = resolve((config.get("claude_code") or {}).get("binary", "claude"))
    hit = _claude_cache.get(binary)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    names: list[str] = []
    if binary:
        _, text = _run([binary, "--help"], env=child_env())
        m = re.search(r"--model <model>(.*?)(?:\n\s*-{1,2}[a-z]|\Z)", text, re.S)
        names = re.findall(r"'([a-z][a-z0-9.-]*)'", m.group(1)) if m else []
    known = {c["id"]: c["name"] for c in CLAUDE_MODELS}
    out = [{"id": n, "name": known.get(n, n.capitalize() + " (последняя)")} for n in dict.fromkeys(names + list(known))]
    if binary:
        _claude_cache[binary] = (out, time.time())
    return out


def limited(config: dict) -> dict:
    from . import limits

    return {k: v for k, v in limits.load().items()}


def routing(config: dict) -> dict:
    from .runtimes import detect_runtime

    return {"runtime": config.get("runtime", "auto"), "auto_resolves_to": detect_runtime(config, ttl=0),
            "disabled_runtimes": list(config.get("disabled_runtimes") or []), "togglable": TOGGLABLE,
            "limited": limited(config), "runtime_by_role": config.get("runtime_by_role") or {},
            "roles": ROLES, "runtimes": runtimes(config),
            "models": {"claude_code": (config.get("claude_code") or {}).get("model", "sonnet"),
                       "codex": (config.get("codex") or {}).get("model") or "",
                       "dsh": (config.get("dsh") or {}).get("model") or ""},
            "catalogs": {rt: model_catalog(config, rt) for rt in ("claude_code", "codex")},
            "acp_agent": (config.get("acp") or {}).get("agent", "gemini")}


def save_routing(config: dict, data: dict, path: Optional[Path] = None) -> dict:
    """Persist runtime choices to ARTEL_HOME/artel.yaml and apply them to the live config."""
    from .project import artel_home, deep_merge

    from .runtimes.acp import presets

    upd: dict = {}
    allowed = runtimes(config)
    if data.get("runtime") in allowed:
        upd["runtime"] = data["runtime"]
    if isinstance(data.get("runtime_by_role"), dict):
        upd["runtime_by_role"] = {r: v for r, v in data["runtime_by_role"].items() if r in ROLES and v in allowed}
    if data.get("acp_agent") in presets(config):
        upd["acp"] = {"agent": data["acp_agent"]}
    if isinstance(data.get("disabled_runtimes"), list):
        upd["disabled_runtimes"] = [r for r in data["disabled_runtimes"] if r in TOGGLABLE]
    for rt in ("claude_code", "codex", "dsh"):
        m = (data.get("models") or {}).get(rt)
        if isinstance(m, str):
            m = m.strip()
            if m and rt == "codex":
                from .runtimes.codex import normalize_model

                m = normalize_model(m, codex_settings(config))
                known = [x["id"] for x in model_catalog(config, "codex")]
                if known and m not in known:
                    raise ValueError(f"Codex не знает модель {m!r}; доступны: {', '.join(known)}")
            upd[rt] = {"model": m or None}
    path = path or artel_home() / "artel.yaml"
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    current = current or {}
    for k in ("runtime_by_role", "disabled_runtimes"):
        if k in upd:
            current.pop(k, None)  # replace, not merge: removing a role override must stick
    merged = deep_merge(current, upd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for k in ("runtime_by_role", "disabled_runtimes"):
        if k in upd:
            config[k] = upd.pop(k)
    for k, v in upd.items():
        config[k] = deep_merge(config.get(k) or {}, v) if isinstance(v, dict) else v
    return routing(config)


def save_timezone(config: dict, name, path: Optional[Path] = None) -> dict:
    """Persist ui.timezone; an empty name clears it (system zone). Invalid IANA names raise ValueError, nothing is saved."""
    from .project import artel_home
    from .tz import info, valid_zone

    name = name.strip() if isinstance(name, str) else name
    if name and not valid_zone(name):
        raise ValueError(f"unknown time zone: {name!r}")
    if name is not None and not isinstance(name, str):
        raise ValueError("timezone must be a string")
    path = path or artel_home() / "artel.yaml"
    current = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
    ui = dict(current.get("ui") or {})
    if name:
        ui["timezone"] = name
    else:
        ui.pop("timezone", None)
    current["ui"] = ui
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config["ui"] = {**(config.get("ui") or {}), **ui}
    if not name:
        config["ui"].pop("timezone", None)
    return info(config)


def embeddings_status(config: dict) -> dict:
    from .embeddings import status

    return status(config)


def save_embeddings(config: dict, data: dict, path: Optional[Path] = None) -> dict:
    """embeddings.backend (auto | local | http | off), dim (768/512/256/128), url/model for http."""
    from .embeddings import DIMS
    from .project import artel_home, deep_merge

    upd: dict = {}
    if data.get("backend") in ("auto", "local", "http", "off"):
        upd["backend"] = data["backend"]
    if data.get("dim") is not None:
        if int(data["dim"]) not in DIMS:
            raise ValueError(f"размерность одна из {DIMS}")
        upd["dim"] = int(data["dim"])
    for k in ("url", "model", "api_key_env"):
        if isinstance(data.get(k), str):
            upd[k] = data[k].strip()
    path = path or artel_home() / "artel.yaml"
    current = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
    current = deep_merge(current, {"embeddings": upd})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config["embeddings"] = deep_merge(config.get("embeddings") or {}, upd)
    return embeddings_status(config)


def overview(config: dict) -> dict:
    return {"embeddings": embeddings_status(config),"codex": codex_status(config), "claude_code": claude_code_status(config), "dsh": dsh_status(config), "acp": acp_status(config),
            "keys": api_keys(config),
            "system_one": system_one_status(config), "routing": routing(config),
            "timezone": __import__("artel.tz", fromlist=["info"]).info(config)}


__all__ = ["save_embeddings", "embeddings_status", "CodexLogin", "ClaudeLogin", "logout_claude", "overview", "model_catalog", "save_routing", "save_timezone", "logout_codex", "json"]
