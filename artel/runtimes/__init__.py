"""Agent runtimes. ``native`` is Artel's own loop over the Messages API; ``claude_code`` drives the Claude Code CLI
and ``codex`` the Codex CLI headless, exposing Artel's tools to them over MCP.

``runtime: auto`` (the default) picks what is connected, in this order: Claude Code (Claude subscription), Codex
(ChatGPT subscription), DeepSeek Harness (installed and DEEPSEEK_API_KEY set), an API key for the native loop. With nothing connected, runs end with a clear setup error
instead of a crash.
"""

from __future__ import annotations

import os
import time

NO_EXECUTOR = ("Нет подключённого исполнителя (not installed): откройте «Провайдеры» и подключите Codex, "
               "Claude Code или DeepSeek Harness, или задайте ключ API в переменной окружения (ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY для DeepSeek Harness)")
_auto_cache: dict = {}


def detect_runtime(config: dict, ttl: float = 120.0) -> str:
    """The runtime ``auto`` resolves to on this machine right now ('none' when nothing is connected)."""
    if config.get("provider") == "scripted":
        return "native"
    key = (config.get("provider"), (config.get("openai_compat") or {}).get("api_key_env"))
    hit = _auto_cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    from ..providers import claude_code_status, codex_status

    if claude_code_status(config).get("logged_in"):
        name = "claude_code"
    elif codex_status(config).get("logged_in"):
        name = "codex"
    elif dsh_ready(config):
        name = "dsh"
    elif native_ready(config):
        name = "native"
    else:
        name = "none"
    _auto_cache[key] = (name, time.time())
    return name


def dsh_ready(config: dict) -> bool:
    from .dsh import dsh_settings, key_available, sdk_available

    cfg = dsh_settings(config)
    return sdk_available(cfg) and bool(os.environ.get("DEEPSEEK_API_KEY")) and key_available(cfg)


def native_ready(config: dict) -> bool:
    if config.get("provider") == "openai_compat":
        oc = config.get("openai_compat") or {}
        return bool(os.environ.get(oc.get("api_key_env", "OPENAI_API_KEY"))) or "localhost" in oc.get("base_url", "") \
            or "127.0.0.1" in oc.get("base_url", "")
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def runtime_for(config: dict, role: str) -> str:
    name = (config.get("runtime_by_role") or {}).get(role) or config.get("runtime", "auto")
    return detect_runtime(config) if name == "auto" else name


def run_agent(kernel, project, *, role: str, **kw):
    """Same contract as :func:`artel.agent.run_loop`, dispatched to the runtime plugin configured for the role."""
    name = runtime_for(kernel.config, role)
    if name == "none":
        from ..agent import Outcome

        out = Outcome(status="error", error=NO_EXECUTOR)
        out.cost_usd = 0.0
        project.log.append("llm/error", actor=role, task_id=((kw.get("ctx") and kw["ctx"].task) or {}).get("id"),
                           error=NO_EXECUTOR)
        return out
    fn = kernel.get(f"runtime:{name}")
    if fn is None:
        raise RuntimeError(f"no runtime plugin registered as {name!r}; loaded: "
                           f"{sorted(k[8:] for k in kernel.services if k.startswith('runtime:'))}")
    return fn(kernel, project, role=role, **kw)
