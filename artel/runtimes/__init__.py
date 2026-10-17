"""Agent runtimes. ``native`` is Artel's own loop over the Messages API; ``claude_code`` drives the Claude Code CLI
and ``codex`` the Codex CLI headless, exposing Artel's tools to them over MCP; ``acp`` drives any agent that speaks
the Agent Client Protocol (Gemini CLI, Claude Code and Codex through their ACP adapters, goose, OpenHands…).

A runtime name may carry a variant after a colon: ``acp:gemini`` runs the ``acp`` runtime with the ``gemini`` preset,
so different roles can use different ACP agents. The variant is passed to the runtime as ``variant=``.

``runtime: auto`` (the default) picks what is connected, in this order: Claude Code (Claude subscription), Codex
(ChatGPT subscription), DeepSeek Harness (installed, and the model chosen in it has a credential), an API key for the native loop. With nothing connected, runs end with a clear setup error
instead of a crash.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

NO_EXECUTOR = ("Нет подключённого исполнителя (not installed): откройте «Подключения» и подключите Codex, "
               "Claude Code или DeepSeek Harness, или задайте ключ API в переменной окружения (ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY)")
_auto_cache: dict = {}


AUTO_ORDER = ("claude_code", "codex", "dsh", "native")


def connected_runtimes(config: dict, ttl: float = 120.0) -> list[str]:
    """Executors that are connected right now, in auto's order (signed-in CLIs, then API keys); cached for ``ttl``."""
    key = (config.get("provider"), (config.get("openai_compat") or {}).get("api_key_env"))
    hit = _auto_cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return list(hit[0])
    from ..providers import claude_code_status, codex_status

    checks = {"claude_code": lambda: claude_code_status(config).get("logged_in"),
              "codex": lambda: codex_status(config).get("logged_in"),
              "dsh": lambda: dsh_ready(config), "native": lambda: native_ready(config)}
    found = []
    for name in AUTO_ORDER:
        try:
            if checks[name]():
                found.append(name)
        except Exception:  # noqa: BLE001 - a broken status check means "not connected", never a crash
            pass
    _auto_cache[key] = (found, time.time())
    return found


def detect_runtime(config: dict, ttl: float = 120.0) -> str:
    """The runtime ``auto`` resolves to right now ('none' when nothing is connected).

    Skips executors the person switched off (``disabled_runtimes``) and those out on a provider limit until their
    reset (artel/limits.py) — so when Claude Code hits its limit, work moves to Codex. If every connected executor is
    limited, the one that resets first is returned (its runs then wait for the reset)."""
    if config.get("provider") == "scripted":
        return "native"
    from .. import limits

    disabled = set(config.get("disabled_runtimes") or [])
    usable = [r for r in connected_runtimes(config, ttl) if r not in disabled]
    if not usable:
        return "none"
    out = limits.load()
    free = [r for r in usable if r not in out]
    return free[0] if free else min(usable, key=lambda r: out[r])


def dsh_ready(config: dict) -> bool:
    from .dsh import dsh_settings, key_available, sdk_available

    cfg = dsh_settings(config)
    return sdk_available(cfg) and key_available(cfg)  # the model chosen in DeepSeek Harness has its credential


def native_ready(config: dict) -> bool:
    if config.get("provider") == "openai_compat":
        oc = config.get("openai_compat") or {}
        return bool(os.environ.get(oc.get("api_key_env", "OPENAI_API_KEY"))) or "localhost" in oc.get("base_url", "") \
            or "127.0.0.1" in oc.get("base_url", "")
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def runtime_for(config: dict, role: str) -> str:
    """The runtime a role runs on. An explicit choice is a preference: while it is out on a provider limit (or
    switched off) and another connected executor is free, work goes there (``failover: false`` keeps it waiting)."""
    name = (config.get("runtime_by_role") or {}).get(role) or config.get("runtime", "auto")
    if name == "auto":
        return detect_runtime(config)
    base = name.split(":")[0]
    if config.get("failover", True) and config.get("provider") != "scripted":
        from .. import limits

        if base in limits.load() or base in (config.get("disabled_runtimes") or []):
            alt = detect_runtime(config)
            if alt != "none" and alt not in limits.load():
                return alt
    return name


def run_agent(kernel, project, *, role: str, **kw):
    """Same contract as :func:`artel.agent.run_loop`, dispatched to the runtime plugin configured for the role.
    Each run's tokens are added to the project's per-tier account (artel/routing.py)."""
    from ..routing import record, tier_for

    tier = tier_for(kernel.config, role, project)
    out = _run_agent(kernel, project, role=role, **kw)
    if kw.get("purpose") == "verify":  # the acceptance check must not distort the account of the run it measures
        return out
    try:
        usage = getattr(out, "usage", None) or {}
        record(project, tier, usage)
        configured = tier_for(kernel.config, role)
        planned = tier_for(kernel.config, role, project, budget=False)  # simple-request rule, not the budget
        if configured == "expensive":  # the manager's calls are few; each one is visible in the log
            project.log.append("tier/usage", role=role, tier=tier, usage=usage, degraded=tier != planned,
                               simple_request=planned != configured)
    except Exception:  # noqa: BLE001 - accounting never breaks a run
        pass
    return out


def _run_agent(kernel, project, *, role: str, **kw):
    name = runtime_for(kernel.config, role)
    if name == "none":
        from ..agent import Outcome

        out = Outcome(status="error", error=NO_EXECUTOR)
        out.cost_usd = 0.0
        project.log.append("llm/error", actor=role, task_id=((kw.get("ctx") and kw["ctx"].task) or {}).get("id"),
                           error=NO_EXECUTOR)
        return out
    fn = kernel.get(f"runtime:{name}")
    if fn is None and ":" in name:
        base, variant = name.split(":", 1)
        fn = kernel.get(f"runtime:{base}")
        if fn is not None:
            with judge_guard(project, role, kw.get("ctx"), name):
                out = fn(kernel, project, role=role, variant=variant, **kw)
            _note_limit(name, out, project)
            return out
    if fn is None:
        raise RuntimeError(f"no runtime plugin registered as {name!r}; loaded: "
                           f"{sorted(k[8:] for k in kernel.services if k.startswith('runtime:'))}")
    hooks = kernel.hooks
    if not (hooks.get("before_agent_start") or hooks.get("agent_end")):  # fast path: no lifecycle observers
        with judge_guard(project, role, kw.get("ctx"), name):
            out = fn(kernel, project, role=role, **kw)
        _note_limit(name, out, project)
        return out
    ctx = kw.get("ctx")
    info = {"project": project, "role": role, "purpose": kw.get("purpose"), "task": getattr(ctx, "task", None),
            "persona": kw.get("persona")}
    if hooks.get("before_agent_start"):
        # plugins may extend the run: append to agent["system"], change first_user, narrow/extend tool_names
        agent = {"system": kw.get("system", ""), "first_user": kw.get("first_user", ""),
                 "tool_names": list(kw["tool_names"]) if kw.get("tool_names") is not None else None}
        kernel.emit("before_agent_start", agent=agent, **info)
        kw["system"], kw["first_user"] = agent["system"], agent["first_user"]
        if agent["tool_names"] is not None or kw.get("tool_names") is not None:
            kw["tool_names"] = agent["tool_names"]
    with judge_guard(project, role, ctx, name):
        out = fn(kernel, project, role=role, **kw)
    _note_limit(name, out, project)
    kernel.emit("agent_end", outcome=out, **info)
    return out


@contextmanager
def judge_guard(project, role: str, ctx, runtime: str):
    """Judges (reviewer, auditor, planner…) must not change what they judge. Their file tools are read-only, but
    every runtime also gives them a shell (to run tests), and a prompt injection in a deliverable can make that shell
    write. So the deliverables are snapshotted — ignored files such as node_modules/ and .env included — and the
    project's command policy saved; whatever a judge's run changed there is restored and recorded."""
    from ..tooling import READONLY_ROLES

    shared = getattr(ctx, "shared_dir", None) or getattr(project, "shared", None)
    judge = role in READONLY_ROLES or (ctx is not None and not ctx.grants()["write"])
    if not judge or shared is None or not Path(shared).is_dir():
        yield
        return
    from ..snapshot import Snapshot

    guard = Snapshot(Path(project.root) / ".cc" / f"guard-{uuid.uuid4().hex[:8]}.git", shared, ignores=False)
    tree = guard.track()
    policy_file = Path(project.root) / "policy.yaml"
    policy_before = policy_file.read_bytes() if policy_file.is_file() else None
    try:
        yield
    finally:
        changed = guard.diff_files(tree) if tree else None
        if changed:
            guard.restore(tree)
        now = policy_file.read_bytes() if policy_file.is_file() else None
        if now != policy_before:
            changed = [*(changed or []), "../policy.yaml"]
            if policy_before is None:
                policy_file.unlink(missing_ok=True)
            else:
                policy_file.write_bytes(policy_before)
        if changed:
            project.log.append("agent/readonly_violation", actor=role, role=role, runtime=runtime,
                               task_id=((getattr(ctx, "task", None) or {}).get("id")), files=changed[:50],
                               note="a judge changed what it judges; restored")
        shutil.rmtree(guard.git_dir, ignore_errors=True)


def _note_limit(name: str, out, project) -> None:
    """A run that ended on a provider limit marks its runtime as out until the reset, so ``auto`` moves on."""
    err = getattr(out, "error", "") or ""
    if getattr(out, "status", "") != "error" or not err:
        return
    from ..failures import classify, retry_at

    if classify(err) == "limit":
        from .. import limits

        until = limits.mark(name.split(":")[0], retry_at(err), err)
        project.log.append("provider/limited", runtime=name, until=until, reason=err[:300])
