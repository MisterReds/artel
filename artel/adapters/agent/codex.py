"""OpenAI Codex CLI codec (ChatGPT subscription via device-code login, or an API key): ``codex exec --json``.

Codex works with its own tools (shell, apply_patch, web search) inside its sandbox and receives Artel's coordination
tools over MCP through the loop's bridge. Login: ``artel providers login codex`` or the dashboard's "Подключения".
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from ...contracts import AgentEvent
from ._common import classify  # noqa: F401 - part of the codec interface
from .codex_support import (DEFAULTS, SANDBOX_BROKEN, _catalog_cache, _sandbox_mode, build_args,  # noqa: F401
                            codex_env, model_catalog, normalize_model, sandbox_mode, sandbox_note)

NAME, LABEL = "codex", "codex"
INSTALL_HINT = "Codex CLI не установлен: «Подключения» → «Установить» или `artel providers install codex`"
NO_SESSION = "codex produced no thread"
OK_EXIT, TRACK_STOP, LOG_ROLE, TRACK_MISSING_USAGE, STRIP_STDERR = (0, None, -15), True, False, True, True
GATE_TOOL, SHELL_TOOL = None, "shell"
TERMINAL_GRACE_S = 10.0  # after a terminal tool: the longest we wait for the turn's closing usage event


def settings(config: dict) -> dict:
    return {**DEFAULTS, **(config.get("codex") or {})}


codex_settings = settings


# --- what the loop asks of a codec -------------------------------------------------------------------------------
def prepare(run) -> dict:
    from ...tooling import READONLY_ROLES

    run.state["grants"] = grants = run.ctx.grants()
    run.state["judge"] = run.role in READONLY_ROLES or not grants["write"]
    run.state["run_dir"] = run_dir = Path(run.ctx.workspace)
    mkdirs = []
    if run.state["judge"]:  # a private scratch cwd: the deliverables are outside every writable root of the sandbox
        run.state["run_dir"] = run_dir = run.project.root / ".cc" / f"{run.role}-scratch"
        mkdirs.append(run_dir)
    run.state["last"] = last = run.project.root / ".cc" / f"codex-last-{uuid.uuid4().hex[:8]}.txt"
    return {"mkdirs": [last.parent, *mkdirs], "cleanup": [last]}


def note(run) -> str:
    from ...tooling import READONLY_ROLES

    specs, ctx = run.bridge.specs, run.ctx
    return (f"\n\n## Runtime\nYou run inside OpenAI Codex. Use your own shell and apply_patch for files. Artel "
            f"coordination tools come from the MCP server `artel`: {', '.join(s.name for s in specs)}. The ONLY way to "
            f"finish is calling {' or '.join(s.name for s in specs if s.terminal)} on that server. Your working "
            f"directory is your workspace; the team's shared deliverables directory is {ctx.shared_dir}.{ctx.shared_note}"
            + (" Your role is read-only: read and run checks on the deliverables by their absolute path, never change "
               "them; your working directory is a private scratch space for temporary files."
               if run.role in READONLY_ROLES else "")
            + (f" Read-only reference sources: {', '.join(map(str, run.project.refs))}." if run.project.refs else ""))


def first_prompt(system: str, note_text: str, first_user: str) -> str:
    return f"<role_instructions>\n{system}{note_text}\n</role_instructions>\n\n{first_user}"


def model_seen(cc: dict) -> str:
    return f"codex:{cc.get('model') or 'default'}"


def start_info(run) -> dict:
    return {"model": model_seen(run.cc), "tools": [s.name for s in run.bridge.specs] + ["shell", "apply_patch"],
            "effort": run.ctx.state.get("effort") or run.cc.get("reasoning_effort") or "default"}


def launch(run) -> dict:
    from ...runtimes import mcp_client

    cc, ctx, config, journal = run.cc, run.ctx, run.kernel.config, []
    mode = sandbox_mode(cc, config=config)
    if mode == "none" and not ctx.state.get("codex_sandbox_noted"):
        ctx.state["codex_sandbox_noted"] = True
        journal.append(("codex/sandbox", dict(mode=mode, reason=sandbox_note(cc), note=(
            "Codex's sandbox cannot start commands on this machine; running without it — "
            "Artel's file grants and command policy still apply"))))
    servers, ext_env = mcp_client.codex_secrets(mcp_client.cli_config(config, run.role))
    args = build_args(cc, workspace=run.state["run_dir"], shared=ctx.shared_dir, role=run.role,
                      bridge_url=run.bridge.url, bridge_token=run.bridge.token_arg, effort=ctx.state.get("effort"),
                      thread_id=run.session, last_message=run.state["last"], grants=run.state["grants"], mode=mode,
                      mcp_servers=servers)
    return {"args": args, "cwd": run.state["run_dir"], "env": {**codex_env(cc, config), **ext_env},
            "stdin": run.prompt, "journal": journal}


def sandbox_recover(cc: dict, broken: str) -> dict:
    """A command failed because the sandbox itself could not start: drop the sandbox on this machine and resume."""
    key = (cc.get("binary"), cc.get("home"))
    if not cc.get("sandbox_fallback", True) or _sandbox_mode.get(key) == "none":
        return {"error": "setup: Codex's sandbox cannot start commands on this machine (" + broken.strip()[-200:]
                + "). Allow it (unprivileged user namespaces) or set codex.sandbox_fallback: true"}
    _sandbox_mode[key] = "none"
    _sandbox_mode[(key, "why")] = broken.strip()[-300:]
    return {"journal": ("codex/sandbox", dict(mode="none", reason=broken[-300:], note=(
        "Codex's sandbox failed to start a command; continuing without it — Artel's file grants and command "
        "policy still apply"))),
        "prompt": "Your last command did not run: the sandbox on this machine failed to start it. That is fixed now — "
                  "shell commands and file edits work. Repeat the step and continue the task."}


def loop_msg(name: str, inp) -> tuple:
    from ...agent import _short

    return _short(inp.get("command", "")), "loop: repeated shell command"


# --- `codex exec --json` -> AgentEvent ---------------------------------------------------------------------------
def _item(item: dict) -> list:
    kind = item.get("type") or item.get("item_type") or ""
    iid = item.get("id") or uuid.uuid4().hex[:10]
    if kind == "agent_message":
        return [AgentEvent("text", {"text": item.get("text", ""), "response": {"n": 1, "fields": {}}})]
    if kind == "command_execution":
        cmd, output, code = item.get("command", ""), item.get("aggregated_output", "") or "", item.get("exit_code")
        res = {"id": iid, "output": output, "is_error": code not in (0, None)}
        if res["is_error"] and SANDBOX_BROKEN.search(output):
            res["sandbox_broken"] = output[-300:]
        return [AgentEvent("tool_call", {"id": iid, "name": "shell", "input": {"command": cmd}, "turn": True,
                                         "observe": "late"}), AgentEvent("tool_result", res)]
    if kind == "file_change":
        changes = item.get("changes") or []
        return [AgentEvent("tool_call", {"id": iid, "name": "apply_patch", "input": {"changes": changes}, "turn": True,
                                         "edited": [c["path"] for c in changes if c.get("path")]}),
                AgentEvent("tool_result", {"id": iid, "output": item.get("status", ""), "digest": False,
                                           "is_error": item.get("status") == "failed"})]
    if kind == "mcp_tool_call":  # the bridge already logged the call and its result
        args = item.get("arguments", {})
        return [AgentEvent("tool_call", {"id": iid, "name": item.get("tool", "?"), "turn": True, "bridged": True,
                                         "input": args if isinstance(args, dict) else {}})]
    if kind == "web_search":
        return [AgentEvent("tool_call", {"id": iid, "name": "web_search", "input": {"query": item.get("query", "")}})]
    if kind == "error":
        return [AgentEvent("error", {"message": item.get("message", "")})]
    return [AgentEvent("text", {"text": ""})]  # progress only (reasoning, todo lists…)


def parse(line: str) -> list:
    try:
        ev = json.loads(line)
    except ValueError:
        return []
    if not isinstance(ev, dict):
        return []
    typ, item = ev.get("type", ""), ev.get("item") or {}
    if typ == "thread.started":
        return [AgentEvent("session", {"id": ev.get("thread_id") or ""})]
    if typ == "turn.completed":
        u = ev.get("usage") or {}
        return [AgentEvent("usage", {"input_tokens": int(u.get("input_tokens") or 0), "cost_usd": 0.0,
                                     "output_tokens": int(u.get("output_tokens") or 0),
                                     "cached": int(u.get("cached_input_tokens") or 0)})]
    if typ in ("turn.failed", "error"):
        err = ev.get("error") or {}
        return [AgentEvent("error", {"fatal": True, "message": str((
            err.get("message") if isinstance(err, dict) else str(err)) or ev.get("message", ""))})]
    if typ == "item.started":
        if item.get("type") == "command_execution":
            return [AgentEvent("command", {"command": item.get("command", ""), "phase": "start"})]
        return [AgentEvent("tool_call", {"id": str(item.get("id") or ""), "name": str(item.get("type") or "item"),
                                         "input": {}, "phase": "start"})]
    return _item(item) if typ == "item.completed" else []
