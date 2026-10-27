"""Claude Code codec (``claude -p`` stream-json): settings, environment, arguments, line parsing. The loop is loop.py.
Claude Code uses its own tools; Artel's coordination tools reach it over MCP through the proxy and the loop's bridge."""

from __future__ import annotations

import json
import shlex
import sys
import uuid
from pathlib import Path
from typing import Optional

from ...contracts import AgentEvent
from ._common import classify  # noqa: F401 - part of the codec interface

NAME, LABEL = "claude_code", "claude code"
INSTALL_HINT = ("Claude Code CLI не установлен: «Подключения» → «Установить» "
                "или `artel providers install claude`")
NO_SESSION = "claude code produced no session"
OK_EXIT, TRACK_STOP, LOG_ROLE, TRACK_MISSING_USAGE, STRIP_STDERR = (0, None), False, True, False, False
GATE_TOOL = SHELL_TOOL = "Bash"  # roles that judge or plan must not edit what they judge: WRITE_TOOLS
BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "TodoWrite", "WebFetch",
                 "WebSearch"]
# Variables that tie a process to the parent Claude Code session; children must not inherit them.
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID",
               "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SYNC_SESSION_REFS",
               "CLAUDE_CODE_TEE_SDK_STDOUT", "CLAUDE_CODE_DIAGNOSTICS_FILE", "CLAUDE_AFTER_LAST_COMPACT",
               "CLAUDE_CODE_SESSION_ATTENDED")
MCP_PREFIX = "mcp__artel__"
WRITE_TOOLS = ("Write", "Edit", "MultiEdit")
# The MCP proxy is started by its file path, never `-m artel.runtimes.mcp_proxy`: the CLI starts it in the agent's
# workspace, and when that workspace is a checkout of Artel (evolution) `-m` would import that checkout's proxy.
PROXY_SCRIPT = str(Path(__file__).resolve().parents[2] / "runtimes" / "mcp_proxy.py")
POLICY_SCRIPT = str(Path(__file__).resolve().parents[2] / "policy.py")
DEFAULTS = {"binary": "claude", "model": "sonnet", "permission_mode": "bypassPermissions", "max_resumes": 3,
            "timeout_s": 2400, "effort": None, "extra_args": []}


def settings(config: dict) -> dict:
    return {**DEFAULTS, **(config.get("claude_code") or {})}


def child_env(config: Optional[dict] = None, runtime: str = "claude_code") -> dict:
    from ...agent_env import agent_env
    from ...tools import env_with_tools

    env = env_with_tools({k: v for k, v in agent_env(runtime, config=config).items() if k not in SESSION_ENV})
    env["ARTEL_CHILD"] = "1"
    if sys.platform == "win32":
        env.setdefault("PYTHONUTF8", "1")  # Python children (the MCP proxy, policy hook) speak UTF-8 like the CLIs
    return env


def mcp_servers_json(external: dict, base: dict) -> dict:
    """The ``mcpServers`` object for --mcp-config: Artel's bridge plus the configured external servers."""
    out = dict(base)
    for name, s in external.items():
        if name in out:
            continue
        out[name] = ({"type": "http", "url": s["url"], "headers": s["headers"]} if "url" in s else
                     {"command": s["command"], "args": s["args"], "env": s["env"]})
    return out


def build_dir_args(grants: Optional[dict]) -> list:
    """``--add-dir`` for every read and write grant (the workspace is the cwd); no grants: nothing extra."""
    if grants is None:
        return []
    return [x for d in dict.fromkeys([*grants["write"], *grants["read"]]) for x in ("--add-dir", d)]


def policy_hook_settings(project_root) -> str:
    """``--settings`` JSON: a PreToolUse hook running artel/policy.py on every Bash command. By file path in isolated
    mode (-I), never ``-m``: in the agent's workspace a planted artel/policy.py or PYTHONPATH would replace the check."""
    cmd = f"{shlex.quote(sys.executable)} -I {shlex.quote(POLICY_SCRIPT)} {shlex.quote(str(project_root))}"
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}})


# --- what the loop asks of a codec -------------------------------------------------------------------------------
def prepare(run) -> dict:
    from ...runtimes import mcp_client

    cfg_dir = run.project.root / ".cc"
    cfg = run.state["cfg"] = cfg_dir / f"mcp-{uuid.uuid4().hex[:8]}.json"
    external = run.state["external"] = mcp_client.cli_config(run.kernel.config, run.role)
    servers = mcp_servers_json(external, {"artel": {"command": sys.executable,
                                                    "args": [PROXY_SCRIPT, run.bridge.url, run.bridge.token_arg]}})
    return {"mkdirs": [cfg_dir], "files": {cfg: json.dumps({"mcpServers": servers})}, "cleanup": [cfg]}


def note(run) -> str:
    specs, ctx = run.bridge.specs, run.ctx
    terminal = [s.name for s in specs if s.terminal]
    return (f"\n\n## Runtime\nYou run inside Claude Code. Use your built-in tools for files and shell. Artel "
            f"coordination tools are MCP tools named {MCP_PREFIX}<name>: {', '.join(s.name for s in specs)}. The ONLY "
            f"way to finish is calling {' or '.join(MCP_PREFIX + n for n in terminal)}. Your working directory is "
            f"your workspace; the team's shared deliverables directory is {ctx.shared_dir}.{ctx.shared_note}"
            + (f" Read-only reference sources: {', '.join(map(str, run.project.refs))}." if run.project.refs else ""))


def first_prompt(system: str, note_text: str, first_user: str) -> str:
    return first_user  # the system prompt travels in --append-system-prompt


def model_seen(cc: dict) -> str:
    return cc["model"]


def start_info(run) -> dict:
    return {"model": f"claude-code:{run.cc['model']}", "effort": run.cc.get("effort") or "default",
            "tools": [MCP_PREFIX + s.name for s in run.bridge.specs] + BUILTIN_TOOLS}


def launch(run) -> dict:
    from ...tools import resolve
    from ...tooling import READONLY_ROLES

    cc, ctx, role = run.cc, run.ctx, run.role
    external = run.state["external"]
    grants = ctx.grants()
    no_write = role in READONLY_ROLES or not grants["write"]
    args = [resolve(cc["binary"]) or cc["binary"], "-p", run.prompt, "--model", cc["model"], "--output-format",
            "stream-json", "--verbose", "--mcp-config", str(run.state["cfg"]), "--strict-mcp-config",
            *([] if run.session else ["--append-system-prompt", run.system_note]),  # a resumed session keeps it
            "--permission-mode", cc["permission_mode"], *build_dir_args(grants),
            "--allowedTools", *[t for t in BUILTIN_TOOLS if role != "intake"  # intake: only submit_contract
                                and (not no_write or t not in WRITE_TOOLS)],
            "mcp__artel", *[f"mcp__{n}" for n in external]]
    if no_write:
        args += ["--disallowedTools", *WRITE_TOOLS]
    args += ["--settings", policy_hook_settings(run.project.root)]
    eff = ctx.state.get("effort") or cc.get("effort")
    if eff:
        args += ["--effort", eff]
    if run.session:
        args += ["--resume", run.session]
    args += list(cc.get("extra_args") or [])
    return {"args": args, "cwd": ctx.workspace, "env": child_env(run.kernel.config)}


def loop_msg(name: str, inp) -> tuple:
    from ...agent import _short

    return _short(inp), f"loop: repeated tool call {name}"


# --- stream-json -> AgentEvent -----------------------------------------------------------------------------------
def _block(b: dict) -> Optional[AgentEvent]:
    t = b.get("type")
    if t == "text":
        return AgentEvent("text", {"text": b.get("text", "")})
    if t != "tool_use":
        return None
    name, inp = str(b.get("name") or ""), b.get("input", {})
    inp = inp if isinstance(inp, dict) else {}
    d = {"id": str(b.get("id") or ""), "name": name, "input": inp}
    if name.startswith(MCP_PREFIX):
        d.update(bridged=True, label=name[len(MCP_PREFIX):])
    else:
        d["observe"] = "first"
        if name in WRITE_TOOLS and inp.get("file_path"):
            d["edited"] = [inp["file_path"]]
    return AgentEvent("tool_call", d)


def parse(line: str) -> list:
    try:
        ev = json.loads(line)
    except ValueError:
        return []
    typ = ev.get("type") if isinstance(ev, dict) else None
    if typ == "system" and ev.get("subtype") == "init":
        return [AgentEvent("session", {"id": ev.get("session_id") or "", "model": ev.get("model") or ""})]
    if typ == "assistant":
        msg = ev.get("message") or {}
        events = [e for e in (_block(x) for x in msg.get("content") or []) if e]
        if events:  # one model response: the first event carries what the journal's llm/response needs
            first = dict(events[0].data, response={"n": len(events), "fields": {
                "model": msg.get("model") or "", "stop_reason": msg.get("stop_reason") or "",
                "usage": msg.get("usage") or {}}})
            events[0] = AgentEvent(events[0].kind, first)
        return events
    if typ == "user":
        out = []
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                out.append(AgentEvent("tool_result", {"id": str(b.get("tool_use_id") or ""), "output": text,
                                                      "is_error": bool(b.get("is_error"))}))
        return out
    if typ == "result":
        u = ev.get("usage") or {}
        tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
            (u.get("cache_read_input_tokens") or 0)
        return [AgentEvent("session", {"id": ev.get("session_id") or ""}),
                AgentEvent("usage", {"input_tokens": tin, "output_tokens": u.get("output_tokens") or 0,
                                     "cost_usd": float(ev.get("total_cost_usd") or 0.0)}),
                AgentEvent("end", {"is_error": bool(ev.get("is_error")), "detail": str(ev.get("result") or "")})]
    return []
