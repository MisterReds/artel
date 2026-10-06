"""What Artel can talk to, and how it is signed in — the data behind the dashboard's "Провайдеры" page.

- Codex (ChatGPT subscription): device-code login driven through the official CLI (`codex login --device-auth`);
  the code and link are shown in the dashboard, the CLI stores and refreshes the tokens itself.
- Claude Code (Claude subscription): status of the installed CLI; login happens in the CLI.
- DeepSeek Harness: whether the SDK (with its bundled runtime) is installed and DEEPSEEK_API_KEY is set; dsh plugins.
- API keys: only whether the configured environment variable is set — values are never read out or stored.
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
from .tools import installed_by_artel, resolve

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ROLES = ["manager", "planner", "worker", "tester", "reviewer", "auditor", "librarian", "refiner", "evolver", "absorber"]
RUNTIMES = ["auto", "native", "claude_code", "codex", "dsh"]
KEY_VARS = {"ANTHROPIC_API_KEY": "Anthropic API", "OPENAI_API_KEY": "OpenAI API", "OPENROUTER_API_KEY": "OpenRouter",
            "DEEPSEEK_API_KEY": "DeepSeek"}


def _run(args: list, env: Optional[dict] = None, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
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
                                         stderr=subprocess.STDOUT, text=True, env=codex_env(cfg), bufsize=1)
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
                self.proc.terminate()
        return self.state


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
            "detail": next((ln for ln in reversed(text.splitlines()) if "WARNING" not in ln), ""), "model": cfg.get("model") or "по умолчанию аккаунта"}


def logout_codex(config: dict) -> dict:
    cfg = codex_settings(config)
    rc, text = _run([resolve(cfg["binary"]) or cfg["binary"], "logout"], env=codex_env(cfg))
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
            "detail": "вход: запустите `claude` в терминале и выполните /login"}


def dsh_status(config: dict) -> dict:
    """DeepSeek Harness: SDK installed, key present (the value is never read out), configured plugins."""
    from .runtimes.dsh import KEY_ENV, dsh_settings, key_available, sdk_available

    cfg = dsh_settings(config)
    installed = sdk_available(cfg)
    version = ""
    if installed:
        try:
            from importlib.metadata import version as _v

            version = _v("deepseek-harness-sdk")
        except Exception:  # noqa: BLE001 - a custom SDK module without distribution metadata
            version = ""
    key = installed and key_available(cfg)
    return {"installed": installed, "by_artel": installed_by_artel("dsh"), "version": version, "logged_in": key,
            "method": f"ключ {KEY_ENV}" if key else "", "key_env": KEY_ENV,
            "key_set": bool(os.environ.get(KEY_ENV)), "model": cfg.get("model") or "",
            "provider": cfg.get("provider"), "profile": cfg.get("profile"),
            "plugins": [str(p) for p in cfg.get("plugins") or []],
            "detail": "" if key else f"задайте переменную окружения {KEY_ENV} и перезапустите Artel"}


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


def routing(config: dict) -> dict:
    from .runtimes import detect_runtime

    return {"runtime": config.get("runtime", "auto"), "auto_resolves_to": detect_runtime(config, ttl=0), "runtime_by_role": config.get("runtime_by_role") or {},
            "roles": ROLES, "runtimes": RUNTIMES,
            "models": {"claude_code": (config.get("claude_code") or {}).get("model", "sonnet"),
                       "codex": (config.get("codex") or {}).get("model") or "",
                       "dsh": (config.get("dsh") or {}).get("model") or ""}}


def save_routing(config: dict, data: dict, path: Optional[Path] = None) -> dict:
    """Persist runtime choices to ARTEL_HOME/artel.yaml and apply them to the live config."""
    from .project import artel_home, deep_merge

    upd: dict = {}
    if data.get("runtime") in RUNTIMES:
        upd["runtime"] = data["runtime"]
    if isinstance(data.get("runtime_by_role"), dict):
        upd["runtime_by_role"] = {r: v for r, v in data["runtime_by_role"].items() if r in ROLES and v in RUNTIMES}
    for rt in ("claude_code", "codex", "dsh"):
        m = (data.get("models") or {}).get(rt)
        if isinstance(m, str):
            upd[rt] = {"model": m.strip() or None}
    path = path or artel_home() / "artel.yaml"
    current = yaml.safe_load(path.read_text()) if path.exists() else {}
    current = current or {}
    if "runtime_by_role" in upd:
        current.pop("runtime_by_role", None)  # replace, not merge: removing a role override must stick
    merged = deep_merge(current, upd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False))
    if "runtime_by_role" in upd:
        config["runtime_by_role"] = upd.pop("runtime_by_role")
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
    current = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    ui = dict(current.get("ui") or {})
    if name:
        ui["timezone"] = name
    else:
        ui.pop("timezone", None)
    current["ui"] = ui
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False))
    config["ui"] = {**(config.get("ui") or {}), **ui}
    if not name:
        config["ui"].pop("timezone", None)
    return info(config)


def overview(config: dict) -> dict:
    return {"codex": codex_status(config), "claude_code": claude_code_status(config), "dsh": dsh_status(config),
            "keys": api_keys(config),
            "system_one": system_one_status(config), "routing": routing(config),
            "timezone": __import__("artel.tz", fromlist=["info"]).info(config)}


__all__ = ["CodexLogin", "overview", "save_routing", "save_timezone", "logout_codex", "json"]
