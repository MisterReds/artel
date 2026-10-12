"""Claude Code as an Artel runtime.

Each role run spawns ``claude -p`` (stream-json) in the role's workspace. Claude Code
works with its own tools (Bash, Edit, Read, Grep…) and receives Artel's coordination
tools — task_submit, board, skills, memory, verdicts — over MCP. The MCP proxy
forwards calls to a per-run HTTP bridge inside this process, so tool execution,
validation and logging are identical to the native runtime.

Every assistant message, built-in tool call and result is mirrored into the project
event log. If Claude Code ends without a terminal tool, the run is resumed
(``--resume``) with the continuation prompt, up to ``max_resumes`` times.
"""

from __future__ import annotations

import json
import os
import hmac
import secrets
import shlex
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .. import policy
from ..agent import LoopDetector, Outcome, _short, execute_tool
from ..prompts import CONTINUATION
from ..tools import terminate_tree
from ..tooling import READONLY_ROLES, ToolContext

# Claude Code ships better versions of these; do not expose Artel's duplicates.
NATIVE_DUPLICATES = {"bash", "read_file", "write_file", "edit_file", "list_files"}
BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "TodoWrite", "WebFetch",
                 "WebSearch"]
# Variables that tie a process to the parent Claude Code session; children must not inherit them.
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
               "CLAUDE_CODE_SYNC_SESSION_REFS", "CLAUDE_CODE_TEE_SDK_STDOUT", "CLAUDE_CODE_DIAGNOSTICS_FILE",
               "CLAUDE_AFTER_LAST_COMPACT", "CLAUDE_CODE_SESSION_ATTENDED")
MCP_PREFIX = "mcp__artel__"
# Roles that judge or plan must not edit the deliverables they are judging.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit")
# The MCP proxy is started by its file path, never `-m artel.runtimes.mcp_proxy`: the CLI starts it in the agent's
# workspace, and when that workspace is a checkout of Artel (evolution) `-m` would import that checkout's proxy.
PROXY_SCRIPT = str(Path(__file__).resolve().with_name("mcp_proxy.py"))

DEFAULTS = {"binary": "claude", "model": "sonnet", "permission_mode": "bypassPermissions", "max_resumes": 3,
            "timeout_s": 2400, "effort": None, "extra_args": []}


def child_env() -> dict:
    from ..tools import env_with_tools

    env = env_with_tools({k: v for k, v in os.environ.items() if k not in SESSION_ENV})
    env["ARTEL_CHILD"] = "1"
    if sys.platform == "win32":
        env.setdefault("PYTHONUTF8", "1")  # Python children (the MCP proxy, policy hook) speak UTF-8 like the CLIs
    return env


class ToolBridge:
    """Localhost HTTP endpoint that executes Artel tools for one run."""

    def __init__(self, kernel, project, ctx: ToolContext, role: str, tool_names: Optional[list], actor: str,
                 runtime: str = "claude_code"):
        self.kernel, self.project, self.ctx, self.actor, self.runtime = kernel, project, ctx, actor, runtime
        self.specs = [s for s in kernel.tools_for(role, tool_names) if s.name not in NATIVE_DUPLICATES]
        self.token = secrets.token_hex(16)
        # the MCP proxy gets the token from a file only its owner can read, never on its command line (`ps` shows
        # every user the command lines of every process)
        cc_dir = Path(project.root) / ".cc"
        cc_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="bridge-", suffix=".token", dir=cc_dir)  # mode 0600
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.token)
        self.token_file = Path(path)
        self.token_arg = "@" + path
        self.calls = 0
        self.lock = threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if not hmac.compare_digest(self.headers.get("X-Artel-Token") or "", bridge.token):
                    return self._reply({"error": "forbidden"}, 403)
                self._reply({"tools": [{"name": s.name, "description": s.description, "inputSchema": s.input_schema}
                                       for s in bridge.specs]})

            def do_POST(self):  # noqa: N802
                if not hmac.compare_digest(self.headers.get("X-Artel-Token") or "", bridge.token):
                    return self._reply({"error": "forbidden"}, 403)
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self._reply(bridge.call(req["name"], req.get("args") or {}))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, name: str, args: dict) -> dict:
        task_id = (self.ctx.task or {}).get("id")
        call_id = "mcp_" + uuid.uuid4().hex[:10]
        with self.lock:
            if self.ctx.terminal:
                return {"text": "this run is already finished; stop now", "is_error": True}
            self.calls += 1
            self.project.log.append("tool/call", actor=self.actor, task_id=task_id, tool=name, input=args,
                                    call_id=call_id, runtime=self.runtime)
            text, is_error = execute_tool(self.kernel, self.ctx, name, args)
            self.project.log.append("tool/result", actor=self.actor, task_id=task_id, tool=name, call_id=call_id,
                                    is_error=is_error, output=text[:4000], runtime=self.runtime)
        return {"text": text, "is_error": is_error}

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.token_file.unlink(missing_ok=True)


def build_dir_args(grants: Optional[dict]) -> list:
    """``--add-dir`` for every read and write grant (the workspace is the cwd); no grants: nothing extra."""
    if grants is None:
        return []
    return [x for d in dict.fromkeys([*grants["write"], *grants["read"]]) for x in ("--add-dir", d)]


def policy_hook_settings(project_root) -> str:
    """``--settings`` JSON: a PreToolUse hook that runs ``python -m artel.policy`` on every Bash command."""
    cmd = f"{shlex.quote(sys.executable)} -m artel.policy {shlex.quote(str(project_root))}"
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}})


def _policy_gate(project, ctx, actor: str, task_id, tool_input: dict, proc) -> bool:
    """Log forbidden/prompt Bash commands; a prompt verdict ends the run as needs_human. True when not allowed."""
    command = str(tool_input.get("command") or "")
    verdict = policy.evaluate(command, project.root)
    if verdict.decision == "allow":
        return False
    project.log.append("policy/blocked", actor=actor, task_id=task_id, command=command, decision=verdict.decision,
                       rule=verdict.rule.id, justification=verdict.justification)
    if verdict.decision == "prompt" and not ctx.terminal:
        ctx.terminal = policy.prompt_terminal(verdict, command)
        terminate_tree(proc)
    return True


def _normalize(block: dict) -> Optional[dict]:
    t = block.get("type")
    if t == "text":
        return {"type": "text", "text": block.get("text", "")}
    if t == "tool_use":
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input", {})}
    return None


def run_claude_code(kernel, project, *, role: str, system: str, first_user: str, ctx: ToolContext, max_turns: int,
                    purpose: str, persona=None, stop_event: Optional[threading.Event] = None,
                    tool_names: Optional[list] = None) -> Outcome:
    from ..tools import resolve

    cc = {**DEFAULTS, **(kernel.config.get("claude_code") or {})}
    actor = persona.name if persona else role
    task_id = (ctx.task or {}).get("id")
    if not resolve(cc["binary"]):
        out = Outcome(status="error", error="Claude Code CLI не установлен: «Провайдеры» → «Установить» "
                                            "или `artel providers install claude`")
        out.cost_usd = 0.0
        project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="claude_code")
        return out
    out = Outcome(status="stalled")
    out.cost_usd = 0.0
    detector = LoopDetector()
    bridge = ToolBridge(kernel, project, ctx, role, tool_names, actor)
    cfg_dir = project.root / ".cc"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / f"mcp-{uuid.uuid4().hex[:8]}.json"
    cfg_path.write_text(json.dumps({"mcpServers": {"artel": {
        "command": sys.executable, "args": [PROXY_SCRIPT, bridge.url, bridge.token_arg]}}}), encoding="utf-8")
    terminal_names = [s.name for s in bridge.specs if s.terminal]
    note = (f"\n\n## Runtime\nYou run inside Claude Code. Use your built-in tools for files and shell. Artel "
            f"coordination tools are MCP tools named {MCP_PREFIX}<name>: "
            f"{', '.join(s.name for s in bridge.specs)}. The ONLY way to finish is calling "
            f"{' or '.join(MCP_PREFIX + n for n in terminal_names)}. Your working directory is your workspace; the "
            f"team's shared deliverables directory is {ctx.shared_dir}.{ctx.shared_note}"
            + (f" Read-only reference sources: {', '.join(map(str, project.refs))}." if project.refs else ""))
    model_seen = cc["model"]
    session_id: Optional[str] = None
    prompt = first_user
    project.log.append("agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id,
                       model=f"claude-code:{cc['model']}", effort=cc.get("effort") or "default", system=system + note,
                       user=first_user, tools=[MCP_PREFIX + s.name for s in bridge.specs] + BUILTIN_TOOLS,
                       runtime="claude_code")
    budget = kernel.get("budget")
    try:
        for attempt in range(1 + int(cc["max_resumes"])):
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            reason = budget(project) if budget else None
            if reason:
                out.status, out.error = "budget", reason
                break
            from ..tools import resolve

            grants = ctx.grants()
            no_write = role in READONLY_ROLES or not grants["write"]
            args = [resolve(cc["binary"]) or cc["binary"], "-p", prompt, "--model", cc["model"], "--output-format", "stream-json", "--verbose",
                    "--mcp-config", str(cfg_path), "--strict-mcp-config",
                    *([] if session_id else ["--append-system-prompt", system + note]),  # a resumed session keeps it
                    "--permission-mode", cc["permission_mode"], *build_dir_args(grants),
                    "--allowedTools", *[t for t in BUILTIN_TOOLS if not no_write or t not in WRITE_TOOLS],
                    "mcp__artel"]
            if no_write:
                args += ["--disallowedTools", *WRITE_TOOLS]
            args += ["--settings", policy_hook_settings(project.root)]
            eff = ctx.state.get("effort") or cc.get("effort")
            if eff:
                args += ["--effort", eff]
            if session_id:
                args += ["--resume", session_id]
            args += list(cc.get("extra_args") or [])
            proc = subprocess.Popen(args, cwd=ctx.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=child_env(), bufsize=1, encoding="utf-8", errors="replace")
            killer = threading.Timer(float(cc["timeout_s"]), terminate_tree, (proc, True))
            killer.start()
            pending: dict[str, tuple] = {}
            result_ev: Optional[dict] = None
            try:
                for line in proc.stdout:
                    if stop_event is not None and stop_event.is_set():
                        terminate_tree(proc)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    typ = ev.get("type")
                    if typ == "system" and ev.get("subtype") == "init":
                        session_id = ev.get("session_id") or session_id
                        model_seen = ev.get("model") or model_seen
                    elif typ == "assistant":
                        msg = ev.get("message") or {}
                        blocks = [b for b in (_normalize(x) for x in msg.get("content") or []) if b]
                        if not blocks:
                            continue
                        out.turns += 1
                        project.log.append("llm/response", actor=actor, role=role, task_id=task_id, turn=out.turns,
                                           model=msg.get("model") or model_seen, stop_reason=msg.get("stop_reason")
                                           or "", usage=msg.get("usage") or {}, blocks=blocks, runtime="claude_code")
                        for b in blocks:
                            if b["type"] == "text" and b["text"].strip():
                                out.digest.append(f"ASSISTANT: {_short(b['text'], 600)}")
                            elif b["type"] == "tool_use":
                                if b["name"].startswith(MCP_PREFIX):
                                    out.digest.append(f"TOOL {b['name'][len(MCP_PREFIX):]}({_short(b['input'])})")
                                    continue
                                if (verdict := detector.observe_tool(b["name"], b["input"])):
                                    project.log.append("agent/loop", actor=actor, task_id=task_id, loop_kind="tool",
                                                       stage=verdict, details={"tool": b["name"],
                                                                               "input": _short(b["input"])})
                                    if verdict == "stop":
                                        out.status, out.error = "stalled", f"loop: repeated tool call {b['name']}"
                                        terminate_tree(proc)
                                if b["name"] == "Bash" and _policy_gate(project, ctx, actor, task_id, b["input"], proc):
                                    out.digest.append(f"POLICY blocked: {_short(b['input'])}")
                                out.tool_calls += 1
                                pending[b["id"]] = (b["name"], b["input"])
                                project.log.append("tool/call", actor=actor, task_id=task_id, tool=b["name"],
                                                   input=b["input"], call_id=b["id"], runtime="claude_code")
                                if b["name"] in ("Edit", "Write", "MultiEdit") and b["input"].get("file_path"):
                                    ctx.edited_files.append(b["input"]["file_path"])
                        if ctx.run_id:
                            project.board.heartbeat(ctx.run_id, turns=out.turns, tool_calls=out.tool_calls + bridge.calls)
                        if out.turns >= max_turns:
                            out.status, out.error = "turn_limit", f"{max_turns} turns"
                            terminate_tree(proc)
                    elif typ == "user":
                        for b in (ev.get("message") or {}).get("content") or []:
                            if not isinstance(b, dict) or b.get("type") != "tool_result" or b.get("tool_use_id") not in pending:
                                continue
                            name, inp = pending.pop(b["tool_use_id"])
                            c = b.get("content")
                            text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or []
                                                                         if isinstance(x, dict))
                            project.log.append("tool/result", actor=actor, task_id=task_id, tool=name,
                                               call_id=b["tool_use_id"], is_error=bool(b.get("is_error")),
                                               output=text[:4000], runtime="claude_code")
                            out.digest.append(f"TOOL {name}({_short(inp)}) -> {_short(text, 300)}")
                            if name == "Bash":
                                ctx.outputs.append(f"$ {inp.get('command', '')}\n{text[:3000]}")
                    elif typ == "result":
                        result_ev = ev
                        session_id = ev.get("session_id") or session_id
            finally:
                killer.cancel()
                proc.wait()
            stderr = proc.stderr.read() if proc.stderr else ""
            if result_ev:
                u = result_ev.get("usage") or {}
                tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                    (u.get("cache_read_input_tokens") or 0)
                tout = u.get("output_tokens") or 0
                cost = float(result_ev.get("total_cost_usd") or 0.0)
                out.usage["input_tokens"] += tin
                out.usage["output_tokens"] += tout
                out.cost_usd += cost
                project.add_usage(tin, tout, cost_usd=cost)
            out.tool_calls = max(out.tool_calls, 0)
            if ctx.terminal:
                out.status, out.terminal = "terminal", ctx.terminal
                break
            if out.status in ("turn_limit", "stopped") or out.error.startswith("loop"):
                break
            if proc.returncode not in (0, None) or (result_ev and result_ev.get("is_error")):
                detail = (result_ev or {}).get("result") or stderr[-800:] or f"exit {proc.returncode}"
                out.status, out.error = "error", f"claude code failed: {detail}"
                project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="claude_code")
                break
            if not session_id:
                out.status, out.error = "error", "claude code produced no session"
                break
            project.log.append("agent/continuation", actor=actor, task_id=task_id, idle_turns=attempt + 1,
                               runtime="claude_code")
            prompt = CONTINUATION
        else:
            out.status, out.error = "stalled", f"no terminal tool after {cc['max_resumes']} resumes"
    finally:
        bridge.close()
        cfg_path.unlink(missing_ok=True)
    out.tool_calls += bridge.calls
    project.log.append("agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
                       terminal=out.name, turns=out.turns, tool_calls=out.tool_calls, usage=out.usage,
                       cost_usd=round(out.cost_usd, 4), error=out.error, runtime="claude_code", model=model_seen)
    return out
