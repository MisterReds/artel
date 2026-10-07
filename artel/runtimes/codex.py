"""OpenAI Codex CLI as an Artel runtime (ChatGPT subscription via device-code login, or an API key).

Each role run spawns ``codex exec --json`` in the role's workspace. Codex works with its own tools (shell,
apply_patch, web search) inside its sandbox and receives Artel's coordination tools over MCP through the same
localhost bridge as the Claude Code runtime, so tool execution, validation and logging are identical.

Every agent message, command and file change is mirrored into the project event log. If Codex ends without a
terminal tool, the thread is resumed (``codex exec resume <thread>``) with the continuation prompt, up to
``max_resumes`` times. Login: ``artel providers login codex`` or the dashboard's "Провайдеры" page.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

from .. import policy
from ..agent import LoopDetector, Outcome, _short
from ..prompts import CONTINUATION
from ..tooling import ToolContext
from .claude_code import READONLY_ROLES, ToolBridge, child_env

TERMINAL_GRACE_S = 0.5  # after a terminal tool: how long to wait for the turn's closing usage event

DEFAULTS = {"binary": "codex", "model": None, "sandbox": "workspace-write", "max_resumes": 3, "timeout_s": 2400,
            "home": None, "reasoning_effort": None, "extra_args": []}
EFFORT = {"low": "low", "medium": "medium", "high": "high"}


_catalog_cache: dict = {}


def model_catalog(cc: Optional[dict] = None, ttl: float = 600.0) -> list[dict]:
    """Models this Codex account can use (`codex debug models`), cached; [] when the CLI is missing or fails."""
    import time as _t

    cc = cc or DEFAULTS
    key = (cc.get("binary"), cc.get("home"), os.environ.get("CODEX_HOME"))
    hit = _catalog_cache.get(key)
    if hit and _t.time() - hit[1] < ttl:
        return hit[0]
    from ..tools import resolve

    binary = resolve(cc.get("binary", "codex"))
    if not binary:
        return []
    try:
        p = subprocess.run([binary, "debug", "models"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, env=codex_env(cc))
        data = json.loads(p.stdout) if p.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError, OSError):
        data = {}
    models = [{"id": m.get("slug"), "name": m.get("display_name") or m.get("slug"),
               "description": m.get("description", ""), "default_effort": m.get("default_reasoning_level"),
               "efforts": [x.get("effort") for x in m.get("supported_reasoning_levels") or []]}
              for m in (data.get("models") or []) if m.get("slug") and m.get("visibility", "list") == "list"]
    if models:
        _catalog_cache[key] = (models, _t.time())
    return models


def normalize_model(model: str, cc: Optional[dict] = None) -> str:
    """Accept a display name ("GPT-6-Luna") or any capitalisation for the model id Codex expects ("gpt-6-luna")."""
    m = (model or "").strip()
    for item in model_catalog(cc):
        if m.lower() in (str(item["id"]).lower(), str(item["name"]).lower()):
            return item["id"]
    return m


def codex_settings(config: dict) -> dict:
    return {**DEFAULTS, **(config.get("codex") or {})}


def codex_env(cfg: dict) -> dict:
    from ..tools import env_with_tools

    env = env_with_tools(child_env())
    if cfg.get("home"):
        env["CODEX_HOME"] = str(Path(cfg["home"]).expanduser())
    return env


def _toml(value) -> str:
    return json.dumps(value)  # JSON strings and arrays of strings are valid TOML


def build_args(cc: dict, *, workspace: Path, shared: Path, role: str, bridge_url: str, bridge_token: str,
               effort: Optional[str], thread_id: Optional[str], last_message: Path) -> list:
    pkg_root = str(Path(__file__).resolve().parents[2])
    sandbox = "read-only" if role in READONLY_ROLES else cc["sandbox"]
    from ..tools import resolve

    args = [resolve(cc["binary"]) or cc["binary"], "exec", "--json", "--skip-git-repo-check", "--color", "never", "-C", str(workspace),
            "-o", str(last_message),
            "-c", f"mcp_servers.artel.command={_toml(sys.executable)}",
            "-c", f"mcp_servers.artel.args={_toml(['-m', 'artel.runtimes.mcp_proxy', bridge_url, bridge_token])}",
            "-c", f"mcp_servers.artel.env={{PYTHONPATH={_toml(pkg_root)}}}",
            "-c", "mcp_servers.artel.tool_timeout_sec=900",
            # Autonomous: Codex never stops to ask (nobody would answer). Its own commands stay inside the sandbox
            # below and Artel's command policy; Artel's coordination tools must run without a per-call approval,
            # otherwise the planner cannot even submit its plan.
            "-c", 'approval_policy="never"',
            "-c", 'mcp_servers.artel.default_tools_approval_mode="approve"']
    if sandbox == "danger-full-access":
        args += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        args += ["-s", sandbox]
        if sandbox == "workspace-write":
            args += ["--add-dir", str(shared)]
    if cc.get("model"):
        args += ["-m", normalize_model(cc["model"], cc)]
    eff = EFFORT.get(effort or cc.get("reasoning_effort") or "")
    if eff:
        args += ["-c", f"model_reasoning_effort={_toml(eff)}"]
    args += list(cc.get("extra_args") or [])
    args += ["resume", thread_id, "-"] if thread_id else ["-"]
    return args


def run_codex(kernel, project, *, role: str, system: str, first_user: str, ctx: ToolContext, max_turns: int,
              purpose: str, persona=None, stop_event: Optional[threading.Event] = None,
              tool_names: Optional[list] = None) -> Outcome:
    from ..tools import resolve, terminate_tree

    cc = codex_settings(kernel.config)
    actor = persona.name if persona else role
    if not resolve(cc["binary"]):
        out = Outcome(status="error", error="Codex CLI не установлен: «Провайдеры» → «Установить» "
                                            "или `artel providers install codex`")
        out.cost_usd = 0.0
        project.log.append("llm/error", actor=actor, task_id=(ctx.task or {}).get("id"), error=out.error,
                           runtime="codex")
        return out
    task_id = (ctx.task or {}).get("id")
    out = Outcome(status="stalled")
    out.cost_usd = 0.0  # subscription runs are not billed per token; tokens are still counted
    detector = LoopDetector()
    bridge = ToolBridge(kernel, project, ctx, role, tool_names, actor, runtime="codex")
    terminal_names = [s.name for s in bridge.specs if s.terminal]
    note = (f"\n\n## Runtime\nYou run inside OpenAI Codex. Use your own shell and apply_patch for files. Artel "
            f"coordination tools come from the MCP server `artel`: {', '.join(s.name for s in bridge.specs)}. The "
            f"ONLY way to finish is calling {' or '.join(terminal_names)} on that server. Your working directory "
            f"is your workspace; the team's shared deliverables directory is {ctx.shared_dir}.{ctx.shared_note}"
            + (f" Read-only reference sources: {', '.join(map(str, project.refs))}." if project.refs else ""))
    model_label = f"codex:{cc.get('model') or 'default'}"
    thread_id: Optional[str] = None
    prompt = f"<role_instructions>\n{system}{note}\n</role_instructions>\n\n{first_user}"
    project.log.append("agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id, model=model_label,
                       effort=ctx.state.get("effort") or cc.get("reasoning_effort") or "default", system=system + note,
                       user=first_user, tools=[s.name for s in bridge.specs] + ["shell", "apply_patch"],
                       runtime="codex")
    budget = kernel.get("budget")
    last_msg = project.root / ".cc" / f"codex-last-{uuid.uuid4().hex[:8]}.txt"
    last_msg.parent.mkdir(exist_ok=True)
    try:
        for attempt in range(1 + int(cc["max_resumes"])):
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            reason = budget(project) if budget else None
            if reason:
                out.status, out.error = "budget", reason
                break
            args = build_args(cc, workspace=Path(ctx.workspace), shared=ctx.shared_dir, role=role,
                              bridge_url=bridge.url, bridge_token=bridge.token, effort=ctx.state.get("effort"),
                              thread_id=thread_id, last_message=last_msg)
            proc = subprocess.Popen(args, cwd=ctx.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=codex_env(cc), bufsize=1,
                                    encoding="utf-8", errors="replace")
            proc.stdin.write(prompt)
            proc.stdin.close()
            killer = threading.Timer(float(cc["timeout_s"]), terminate_tree, (proc, True))
            killer.start()
            failure = ""
            blocked = ""
            stopped_by_us = False
            grace: Optional[threading.Timer] = None
            try:
                for line in proc.stdout:
                    if stop_event is not None and stop_event.is_set():
                        stopped_by_us = stopped_by_us or terminate_tree(proc)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    typ = ev.get("type", "")
                    if typ == "thread.started":
                        thread_id = ev.get("thread_id") or thread_id
                    elif typ == "turn.completed":
                        u = ev.get("usage") or {}
                        tin = int(u.get("input_tokens") or 0)
                        tout = int(u.get("output_tokens") or 0)
                        out.usage["input_tokens"] += tin
                        out.usage["output_tokens"] += tout
                        project.add_usage(tin, tout, cost_usd=0.0)
                    elif typ in ("turn.failed", "error"):
                        err = ev.get("error") or {}
                        failure = (err.get("message") if isinstance(err, dict) else str(err)) or ev.get("message", "")
                    elif typ == "item.started" and (ev.get("item") or {}).get("type") == "command_execution":
                        # Codex has no pre-exec hook: stop the process as soon as a disallowed command starts
                        cmd = (ev.get("item") or {}).get("command", "")
                        verdict = policy.evaluate(cmd, project.root)
                        if verdict.decision != "allow":
                            project.log.append("policy/blocked", actor=actor, task_id=task_id, command=cmd,
                                               decision=verdict.decision, rule=verdict.rule.id,
                                               justification=verdict.justification, runtime="codex")
                            if verdict.decision == "prompt":
                                ctx.terminal = policy.prompt_terminal(verdict, cmd)
                            else:
                                blocked = (f"The command `{cmd}` was blocked by policy ({verdict.rule.id}): "
                                           f"{verdict.justification or 'forbidden'}. It was not run; choose another way.")
                            stopped_by_us = stopped_by_us or terminate_tree(proc)
                    elif typ == "item.completed":
                        if _item(project, ctx, out, detector, ev.get("item") or {}, actor, task_id, model_label):
                            stopped_by_us = stopped_by_us or terminate_tree(proc)
                        if ctx.run_id:
                            project.board.heartbeat(ctx.run_id, turns=out.turns, tool_calls=out.tool_calls + bridge.calls)
                        if out.turns >= max_turns:
                            out.status, out.error = "turn_limit", f"{max_turns} turns"
                            stopped_by_us = stopped_by_us or terminate_tree(proc)
                    if ctx.terminal and not stopped_by_us:
                        # the run is finished as soon as Artel accepted a terminal tool; do not let Codex continue.
                        # Its closing turn.completed (the turn's token usage) normally follows within milliseconds:
                        # wait for it briefly, but stop at once if Codex starts anything new.
                        if typ in ("turn.completed", "item.started") or grace is not None and not grace.is_alive():
                            stopped_by_us = terminate_tree(proc)
                        elif grace is None:
                            grace = threading.Timer(TERMINAL_GRACE_S, terminate_tree, (proc,))
                            grace.start()
            finally:
                killer.cancel()
                if grace is not None:
                    grace.cancel()
                    stopped_by_us = True
                proc.wait()
            stderr = proc.stderr.read() if proc.stderr else ""
            if ctx.terminal:
                out.status, out.terminal = "terminal", ctx.terminal
                break
            if out.status in ("turn_limit", "stopped") or out.error.startswith("loop"):
                break
            if failure or (proc.returncode not in (0, None, -15) and not stopped_by_us):
                detail = failure or stderr.strip()[-800:] or f"exit {proc.returncode}"
                out.status, out.error = "error", f"codex failed: {detail}"
                project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="codex")
                break
            if not thread_id:
                out.status, out.error = "error", "codex produced no thread"
                break
            project.log.append("agent/continuation", actor=actor, task_id=task_id, idle_turns=attempt + 1,
                               runtime="codex")
            prompt = (blocked + "\n\n" if blocked else "") + CONTINUATION
        else:
            out.status, out.error = "stalled", f"no terminal tool after {cc['max_resumes']} resumes"
    finally:
        bridge.close()
        last_msg.unlink(missing_ok=True)
    out.tool_calls += bridge.calls
    project.log.append("agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
                       terminal=out.name, turns=out.turns, tool_calls=out.tool_calls, usage=out.usage,
                       cost_usd=0.0, error=out.error, runtime="codex", model=model_label)
    return out


def _item(project, ctx, out, detector, item: dict, actor: str, task_id, model: str) -> bool:
    """Mirror one completed Codex item into the event log. Returns True when the run must stop (loop)."""
    kind = item.get("type") or item.get("item_type") or ""
    iid = item.get("id") or uuid.uuid4().hex[:10]
    if kind == "agent_message":
        text = item.get("text", "")
        out.turns += 1
        project.log.append("llm/response", actor=actor, task_id=task_id, turn=out.turns, model=model,
                           blocks=[{"type": "text", "text": text}], runtime="codex")
        if text.strip():
            out.digest.append(f"ASSISTANT: {_short(text, 600)}")
    elif kind == "command_execution":
        cmd = item.get("command", "")
        output = item.get("aggregated_output", "") or ""
        out.turns += 1
        out.tool_calls += 1
        project.log.append("tool/call", actor=actor, task_id=task_id, tool="shell", input={"command": cmd},
                           call_id=iid, runtime="codex")
        project.log.append("tool/result", actor=actor, task_id=task_id, tool="shell", call_id=iid,
                           is_error=item.get("exit_code") not in (0, None), output=output[:4000], runtime="codex")
        out.digest.append(f"TOOL shell({_short(cmd)}) -> {_short(output, 300)}")
        ctx.outputs.append(f"$ {cmd}\n{output[:3000]}")
        verdict = detector.observe_tool("shell", {"command": cmd})
        if verdict:
            project.log.append("agent/loop", actor=actor, task_id=task_id, loop_kind="tool", stage=verdict,
                               details={"tool": "shell", "input": _short(cmd)})
            if verdict == "stop":
                out.status, out.error = "stalled", "loop: repeated shell command"
                return True
    elif kind == "file_change":
        changes = item.get("changes") or []
        out.turns += 1
        out.tool_calls += 1
        project.log.append("tool/call", actor=actor, task_id=task_id, tool="apply_patch",
                           input={"changes": changes}, call_id=iid, runtime="codex")
        project.log.append("tool/result", actor=actor, task_id=task_id, tool="apply_patch", call_id=iid,
                           is_error=item.get("status") == "failed", output=item.get("status", ""), runtime="codex")
        for c in changes:
            if c.get("path"):
                ctx.edited_files.append(c["path"])
    elif kind == "mcp_tool_call":
        out.turns += 1  # the bridge already logged the call and its result
        out.digest.append(f"TOOL {item.get('tool', '?')}({_short(item.get('arguments', {}))})")
    elif kind == "web_search":
        out.tool_calls += 1
        project.log.append("tool/call", actor=actor, task_id=task_id, tool="web_search",
                           input={"query": item.get("query", "")}, call_id=iid, runtime="codex")
    elif kind == "error":
        project.log.append("llm/error", actor=actor, task_id=task_id, error=item.get("message", ""), runtime="codex")
    return False
