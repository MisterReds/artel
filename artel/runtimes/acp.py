"""Any ACP agent as an Artel runtime (Agent Client Protocol, https://agentclientprotocol.com).

ACP is JSON-RPC 2.0 over the agent's stdio, the protocol editors such as Zed use to drive coding agents. Artel plays
the client: it spawns the configured agent command in the role's workspace and speaks protocol version 1:

- ``initialize`` — advertises ``fs.readTextFile`` / ``fs.writeTextFile`` (no terminal: agents use their own shell);
  an optional ``authenticate`` with ``acp.auth_method``;
- ``session/new`` — ``cwd`` is the workspace and ``mcpServers`` carries Artel's MCP proxy (stdio), so the agent gets
  Artel's coordination tools (task_submit, board, skills…) from the same localhost bridge as the other CLI runtimes;
  the shared directory and refs are passed as ``additionalDirectories`` when the agent supports them;
- ``session/prompt`` — the role instructions and the task; when the turn ends without a terminal tool, another
  ``session/prompt`` with the continuation prompt goes to the same session, up to ``max_resumes`` times;
- ``session/update`` — message/thought chunks become ``llm/response``, ``tool_call`` / ``tool_call_update`` become
  ``tool/call`` / ``tool/result``, ``plan`` becomes ``agent/plan``; ``usage_update`` and the turn's ``usage`` count;
- ``session/request_permission`` — answered by role policy: read-only roles are refused edits, commands go through
  :mod:`artel.policy`, edits outside the workspace / shared directory are refused;
- ``fs/read_text_file`` (workspace, shared directory, refs) and ``fs/write_text_file`` (workspace and shared
  directory only, never refs, never for read-only roles);
- ``session/cancel`` once Artel accepted a terminal tool, on stop, on a loop, on the turn limit or the timeout.

Config (``acp:`` in artel.yaml): ``agent`` (preset name), ``command`` (explicit argv, overrides the preset), ``env``,
``timeout_s``, ``max_resumes``, ``auth_method``, ``presets`` (your own ``name: [argv]`` or ``{command, env}``).
Per role: ``runtime_by_role: {worker: "acp:gemini", reviewer: "acp:claude"}``; plain ``acp`` uses ``acp.agent``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .. import policy
from ..agent import LoopDetector, Outcome, _short
from ..prompts import CONTINUATION
from ..tooling import ToolContext
from .claude_code import PROXY_SCRIPT, READONLY_ROLES, ToolBridge, child_env

PROTOCOL_VERSION = 1
# Known ACP agents. Each is a command line; users add their own under ``acp.presets``.
PRESETS: dict = {
    "gemini": ["gemini", "--experimental-acp"],
    "claude": ["claude-agent-acp"],          # npm @agentclientprotocol/claude-agent-acp
    "codex": ["codex-acp"],                  # the ACP adapter for the Codex CLI
    "goose": ["goose", "acp"],
    "openhands": ["openhands", "acp"],
    "opencode": ["opencode", "acp"],
    "qwen": ["qwen", "--experimental-acp"],
}
DEFAULTS = {"agent": "gemini", "command": [], "env": {}, "timeout_s": 2400, "max_resumes": 3, "auth_method": None,
            "presets": {}, "handshake_timeout_s": 120, "cancel_grace_s": 5}
EDIT_KINDS = {"edit", "delete", "move"}
RPC_TIMEOUT = -32001


class AcpError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def presets(config: dict) -> dict:
    """Preset name -> {"command": [...], "env": {...}} (built-in presets plus ``acp.presets``)."""
    user = (config.get("acp") or {}).get("presets") or {}
    out = {}
    for name, p in {**PRESETS, **user}.items():
        if isinstance(p, str):
            p = p.split()
        out[name] = {"command": list(p), "env": {}} if isinstance(p, list) else \
            {"command": list(p.get("command") or []), "env": dict(p.get("env") or {})}
    return out


def acp_settings(config: dict, variant: Optional[str] = None) -> dict:
    """Effective settings for one run: ``variant`` (from ``acp:<name>``) picks a preset, else ``acp.command`` or
    the preset named by ``acp.agent``."""
    cfg = {**DEFAULTS, **(config.get("acp") or {})}
    ps = presets(config)
    name = variant or cfg["agent"]
    if variant or not cfg.get("command"):
        preset = ps.get(name)
        if preset is None:
            raise AcpError(f"неизвестный ACP-агент {name!r}: задайте acp.command или acp.presets.{name} "
                           f"(известные: {', '.join(sorted(ps))})")
        cfg["command"], cfg["env"] = preset["command"], {**preset["env"], **(cfg.get("env") or {})}
    else:
        name = variant or "custom"
        cfg["command"] = list(cfg["command"]) if isinstance(cfg["command"], list) else str(cfg["command"]).split()
    cfg["name"] = name
    return cfg


def _within(p: Path, roots) -> bool:
    for r in roots:
        try:
            p.relative_to(Path(r).resolve())
            return True
        except ValueError:
            continue
    return False


def _text_of(content) -> str:
    """Text of an ACP ContentBlock or a list of ToolCallContent items."""
    if isinstance(content, dict):
        content = [content]
    parts = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "content":
            parts.append(_text_of(c.get("content")))
        elif c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c.get("type") == "diff":
            parts.append(f"diff {c.get('path', '')}")
        elif c.get("type") == "resource":
            parts.append((c.get("resource") or {}).get("text", ""))
        elif c.get("type") == "resource_link":
            parts.append(c.get("uri", ""))
    return "\n".join(p for p in parts if p)


class AcpClient:
    """JSON-RPC 2.0 over the agent's stdio. A reader thread queues every incoming message; the run loop handles
    them on its own thread, so all run state stays single-threaded."""

    def __init__(self, argv: list, cwd: Path, env: dict):
        self.proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env, bufsize=1, encoding="utf-8",
                                     errors="replace")
        self.inbox: "queue.Queue[Optional[dict]]" = queue.Queue()
        self.stderr: list[str] = []
        self.next_id = 0
        self.wlock = threading.Lock()
        threading.Thread(target=self._read, daemon=True, name="acp-stdout").start()
        threading.Thread(target=self._drain, daemon=True, name="acp-stderr").start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # agents sometimes log to stdout; only JSON-RPC lines matter
            if isinstance(msg, dict):
                self.inbox.put(msg)
        self.inbox.put(None)  # EOF

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line)
            if len(self.stderr) > 400:
                del self.stderr[:200]

    def stderr_tail(self, n: int = 800) -> str:
        return "".join(self.stderr).strip()[-n:]

    def send(self, obj: dict) -> None:
        obj = {"jsonrpc": "2.0", **obj}
        with self.wlock:
            try:
                self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def request(self, method: str, params: dict) -> int:
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        return self.next_id

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def respond(self, mid, result=None, error: Optional[dict] = None) -> None:
        self.send({"id": mid, "error": error} if error else {"id": mid, "result": result if result is not None else {}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class AcpRun:
    """One role run against one ACP agent process."""

    def __init__(self, kernel, project, *, role: str, ctx: ToolContext, cfg: dict, actor: str, bridge: ToolBridge,
                 max_turns: int, stop_event: Optional[threading.Event]):
        self.kernel, self.project, self.role, self.ctx, self.cfg = kernel, project, role, ctx, cfg
        self.actor, self.bridge, self.max_turns, self.stop_event = actor, bridge, max_turns, stop_event
        self.task_id = (ctx.task or {}).get("id")
        self.out = Outcome(status="stalled")
        self.out.cost_usd = 0.0
        self.detector = LoopDetector()
        self.model = f"acp:{cfg['name']}"
        self.client: Optional[AcpClient] = None
        self.session_id: Optional[str] = None
        self.agent_caps: dict = {}
        self.agent_info: dict = {}
        self.auth_methods: list = []
        self.tools: dict[str, dict] = {}  # toolCallId -> {name, title, kind, input, logged, bridge}
        self.msg: dict = {"id": None, "text": "", "thought": ""}
        self.blocked: list[str] = []
        self.cancelled = False
        self.session_cost = 0.0
        self.last_beat = 0.0
        self.write_roots = [Path(ctx.workspace).resolve(), Path(project.shared).resolve()]
        self.refs = [Path(r).resolve() for r in getattr(project, "refs", [])]
        self.read_roots = self.write_roots + self.refs
        self.bridge_names = {s.name for s in bridge.specs}

    # ---- logging helpers ----
    def log(self, kind: str, **kw) -> None:
        self.project.log.append(kind, actor=self.actor, task_id=self.task_id, runtime="acp", **kw)

    def heartbeat(self, force: bool = False) -> None:
        if self.ctx.run_id and (force or time.time() - self.last_beat > 5):
            self.last_beat = time.time()
            self.project.board.heartbeat(self.ctx.run_id, turns=self.out.turns,
                                         tool_calls=self.out.tool_calls + self.bridge.calls)

    # ---- request/response plumbing ----
    def call(self, method: str, params: dict, timeout: float) -> dict:
        """Send a request and process incoming traffic until its response arrives."""
        rid = self.client.request(method, params)
        deadline = time.time() + timeout
        while True:
            if self.should_cancel():
                self.cancel()  # the agent answers the pending prompt with stopReason "cancelled"
                deadline = min(deadline, time.time() + float(self.cfg.get("cancel_grace_s", 5)))
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AcpError(f"{method}: no answer in {int(timeout)}s", RPC_TIMEOUT)
            try:
                msg = self.client.inbox.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if msg is None:
                code = self.client.proc.poll()
                raise AcpError(f"agent exited (code {code}) during {method}: {self.client.stderr_tail() or 'no output'}")
            if "method" in msg:
                self.handle(msg)
            elif msg.get("id") == rid:
                if msg.get("error"):
                    e = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                    detail = e.get("message", "")
                    if e.get("data"):
                        detail += f" ({_short(e['data'], 400)})"
                    raise AcpError(detail or f"{method} failed", e.get("code"))
                return msg.get("result") or {}

    def should_cancel(self) -> bool:
        if self.cancelled:
            return False
        return bool(self.ctx.terminal or (self.stop_event is not None and self.stop_event.is_set())
                    or self.out.status == "turn_limit" or self.out.error.startswith("loop"))

    def cancel(self) -> None:
        if not self.cancelled and self.session_id:
            self.cancelled = True
            self.client.notify("session/cancel", {"sessionId": self.session_id})

    # ---- incoming messages ----
    def handle(self, msg: dict) -> None:
        method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "session/update":
            self.on_update(params.get("update") or {})
            return
        if mid is None:
            return  # unknown notification
        try:
            if method == "session/request_permission":
                self.client.respond(mid, self.on_permission(params))
            elif method == "fs/read_text_file":
                self.client.respond(mid, self.fs_read(params))
            elif method == "fs/write_text_file":
                self.client.respond(mid, self.fs_write(params))
            else:
                self.client.respond(mid, error={"code": -32601, "message": f"method not supported by Artel: {method}"})
        except AcpError as e:
            self.client.respond(mid, error={"code": e.code or -32603, "message": str(e)})
        except Exception as e:  # noqa: BLE001 - answer the agent, keep the run alive
            self.client.respond(mid, error={"code": -32603, "message": repr(e)})

    def flush_message(self) -> None:
        text, thought = self.msg["text"], self.msg["thought"]
        self.msg = {"id": None, "text": "", "thought": ""}
        if not text.strip() and not thought.strip():
            return
        self.out.turns += 1
        blocks = ([{"type": "thinking", "thinking": thought}] if thought.strip() else []) + \
                 ([{"type": "text", "text": text}] if text.strip() else [])
        self.project.log.append("llm/response", actor=self.actor, role=self.role, task_id=self.task_id,
                                turn=self.out.turns, model=self.model, blocks=blocks, runtime="acp")
        if text.strip():
            self.out.digest.append(f"ASSISTANT: {_short(text, 600)}")
            verdict = None if self.out.error else self.detector.observe_text(text)
            if verdict:
                self.log("agent/loop", loop_kind="text", stage=verdict, details={"text": _short(text, 200)})
                if verdict == "stop":
                    self.out.status, self.out.error = "stalled", "loop: repeated text"
        self.heartbeat()
        if self.out.turns >= self.max_turns and not self.out.error:
            self.out.status, self.out.error = "turn_limit", f"{self.max_turns} turns"

    def on_update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            mid = u.get("messageId")
            if mid and self.msg["id"] and mid != self.msg["id"]:
                self.flush_message()
            self.msg["id"] = mid or self.msg["id"]
            self.msg["text" if kind == "agent_message_chunk" else "thought"] += _text_of(u.get("content"))
            return
        if kind in ("tool_call", "tool_call_update"):
            self.flush_message()
            self.on_tool(u, new=kind == "tool_call")
        elif kind == "plan":
            self.flush_message()
            entries = [{"content": e.get("content", ""), "status": e.get("status", ""),
                        "priority": e.get("priority", "")} for e in u.get("entries") or []]
            self.log("agent/plan", entries=entries)
            self.out.digest.append("PLAN: " + "; ".join(f"[{e['status']}] {e['content']}" for e in entries)[:600])
        elif kind == "usage_update":
            cost = (u.get("cost") or {})
            if cost.get("currency", "USD") == "USD" and cost.get("amount") is not None:
                self.session_cost = max(self.session_cost, float(cost["amount"]))
            self.ctx.state["acp_context"] = {"used": u.get("used"), "size": u.get("size")}
        self.heartbeat()

    def _is_bridge(self, name: str, title: str) -> bool:
        """The agent's own report of a call to Artel's MCP server: the bridge has already logged it."""
        hay = f"{name} {title}"
        return "artel" in hay.lower() and any(n in hay for n in self.bridge_names)

    def on_tool(self, u: dict, new: bool) -> None:
        tid = u.get("toolCallId") or uuid.uuid4().hex[:10]
        t = self.tools.get(tid)
        if t is None:
            t = self.tools[tid] = {"name": "", "title": "", "kind": "other", "input": None, "logged": False,
                                   "done": False, "locations": []}
        for key, field in (("name", "name"), ("title", "title"), ("kind", "kind"), ("input", "rawInput")):
            if u.get(field) is not None:
                t[key] = u[field]
        if u.get("locations"):
            t["locations"] = [x.get("path") for x in u["locations"] if isinstance(x, dict) and x.get("path")]
        diffs = [c.get("path") for c in u.get("content") or [] if isinstance(c, dict) and c.get("type") == "diff"]
        tool = t["name"] or t["kind"] or "tool"
        if not t["logged"]:
            t["logged"] = True
            t["bridge"] = self._is_bridge(t["name"], t["title"])
            if t["bridge"]:
                self.out.turns += 1
                self.out.digest.append(f"TOOL {t['name'] or t['title']}")
            else:
                self.out.turns += 1
                self.out.tool_calls += 1
                inp = t["input"] if t["input"] is not None else {"title": t["title"]}
                self.log("tool/call", tool=tool, input=inp, call_id=tid, title=t["title"], tool_kind=t["kind"])
                observed = t["input"] if t["input"] is not None else t["title"]
                verdict = None if self.out.error else self.detector.observe_tool(tool, observed)
                if verdict:
                    self.log("agent/loop", loop_kind="tool", stage=verdict, details={"tool": tool,
                                                                                     "input": _short(inp)})
                    if verdict == "stop":
                        self.out.status, self.out.error = "stalled", f"loop: repeated tool call {tool}"
            if self.out.turns >= self.max_turns and not self.out.error:
                self.out.status, self.out.error = "turn_limit", f"{self.max_turns} turns"
        if t["kind"] in EDIT_KINDS:
            for p in t["locations"] + diffs:
                if p and p not in self.ctx.edited_files:
                    self.ctx.edited_files.append(p)
        status = u.get("status")
        if status in ("completed", "failed") and not t["done"]:
            t["done"] = True
            if t.get("bridge"):
                return
            text = _text_of(u.get("content"))
            if not text and u.get("rawOutput") is not None:
                raw = u["rawOutput"]
                text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
            self.log("tool/result", tool=tool, call_id=tid, is_error=status == "failed", output=text[:4000])
            self.out.digest.append(f"TOOL {tool}({_short(t['input'] or t['title'])}) -> {_short(text, 300)}")
            if t["kind"] == "execute":
                cmd = self._command(t)
                self.ctx.outputs.append(f"$ {cmd}\n{text[:3000]}")

    def close_open_tools(self) -> None:
        """Keep tool/call <-> tool/result paired when a turn ends with calls the agent never finished."""
        for tid, t in self.tools.items():
            if t["logged"] and not t["done"] and not t.get("bridge"):
                t["done"] = True
                self.log("tool/result", tool=t["name"] or t["kind"] or "tool", call_id=tid, is_error=True,
                         output="no result reported before the turn ended")

    # ---- permission policy ----
    @staticmethod
    def _command(t: dict) -> str:
        inp = t.get("input")
        if isinstance(inp, dict):
            c = inp.get("command") or inp.get("cmd")
            if isinstance(c, list):
                return " ".join(map(str, c))
            if c:
                return str(c)
        if isinstance(inp, str):
            return inp
        return t.get("title") or ""

    def _paths(self, t: dict, tc: dict) -> list:
        paths = list(t.get("locations") or [])
        paths += [x.get("path") for x in tc.get("locations") or [] if isinstance(x, dict)]
        paths += [c.get("path") for c in tc.get("content") or [] if isinstance(c, dict) and c.get("type") == "diff"]
        inp = t.get("input")
        if isinstance(inp, dict):
            paths += [inp.get(k) for k in ("file_path", "path", "absolute_path", "filePath", "target")
                      if isinstance(inp.get(k), str)]
        return [p for p in paths if p]

    def decide(self, t: dict, tc: dict) -> tuple[bool, str]:
        """(allowed, reason) for one permission request."""
        kind = t.get("kind") or "other"
        if kind in EDIT_KINDS:
            if self.role in READONLY_ROLES:
                return False, f"role {self.role} is read-only: it may not edit files"
            for p in self._paths(t, tc):
                rp = (Path(self.ctx.workspace) / p).resolve()
                if _within(rp, self.refs):
                    return False, f"{p} is a read-only reference source"
                if not _within(rp, self.write_roots):
                    return False, f"{p} is outside the workspace and the shared directory"
        if kind == "execute":
            cmd = self._command(t)
            verdict = policy.evaluate(cmd, self.project.root)
            if verdict.decision != "allow":
                self.log("policy/blocked", command=cmd, decision=verdict.decision, rule=verdict.rule.id,
                         justification=verdict.justification)
                if verdict.decision == "prompt" and not self.ctx.terminal:
                    self.ctx.terminal = policy.prompt_terminal(verdict, cmd)
                    return False, "needs a person's approval"
                return False, (f"The command `{cmd}` was blocked by policy ({verdict.rule.id}): "
                               f"{verdict.justification or 'forbidden'}. It was not run; choose another way.")
        return True, ""

    def on_permission(self, params: dict) -> dict:
        tc = params.get("toolCall") or {}
        tid = tc.get("toolCallId")
        if tid and (tid not in self.tools or tc.get("rawInput") is not None or tc.get("kind")):
            self.on_tool({**tc, "toolCallId": tid}, new=tid not in self.tools)
        t = self.tools.get(tid) or {"kind": tc.get("kind") or "other", "input": tc.get("rawInput"),
                                     "title": tc.get("title", ""), "locations": []}
        options = params.get("options") or []
        if self.cancelled or self.ctx.terminal:
            allowed, reason = False, "the run is finishing"
        else:
            allowed, reason = self.decide(t, tc)
        self.log("acp/permission", call_id=tid, tool_kind=t.get("kind"), title=t.get("title", ""),
                 decision="allow" if allowed else "reject", reason=reason)
        if not allowed and reason.startswith("The command"):
            self.blocked.append(reason)
        if self.cancelled:
            return {"outcome": {"outcome": "cancelled"}}
        prefer = ("allow_once", "allow_always") if allowed else ("reject_once", "reject_always")
        for k in prefer:
            opt = next((o for o in options if o.get("kind") == k), None)
            if opt:
                return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}  # no matching option: refuse without choosing

    # ---- client file system ----
    def _path(self, params: dict) -> Path:
        p = params.get("path")
        if not isinstance(p, str) or not p:
            raise AcpError("path is required", -32602)
        if not os.path.isabs(p):
            raise AcpError(f"path must be absolute: {p}", -32602)
        return Path(p).resolve()

    def fs_read(self, params: dict) -> dict:
        p = self._path(params)
        if not _within(p, self.read_roots):
            self.log("policy/blocked", command=f"fs/read_text_file {p}", decision="forbidden", rule="acp-fs-scope",
                     justification="outside the workspace, the shared directory and refs")
            raise AcpError(f"access denied: {p} is outside the workspace, the shared directory and refs", -32602)
        if not p.is_file():
            raise AcpError(f"file not found: {p}", -32002)
        text = p.read_text(encoding="utf-8", errors="replace")
        line, limit = params.get("line"), params.get("limit")
        if line or limit:
            lines = text.splitlines(keepends=True)
            start = max(int(line or 1) - 1, 0)
            text = "".join(lines[start:start + int(limit)] if limit else lines[start:])
        self.ctx.record_stamp(p)
        return {"content": text}

    def fs_write(self, params: dict) -> dict:
        p = self._path(params)
        reason = ""
        if self.role in READONLY_ROLES:
            reason = f"role {self.role} is read-only"
        elif _within(p, self.refs):
            reason = f"{p} is a read-only reference source"
        elif not _within(p, self.write_roots):
            reason = f"{p} is outside the workspace and the shared directory"
        call_id = "fs_" + uuid.uuid4().hex[:10]
        content = params.get("content")
        if not isinstance(content, str):
            raise AcpError("content must be a string", -32602)
        self.log("tool/call", tool="fs/write_text_file", input={"path": str(p), "bytes": len(content)},
                 call_id=call_id)
        if reason:
            self.log("tool/result", tool="fs/write_text_file", call_id=call_id, is_error=True,
                     output=f"denied: {reason}")
            raise AcpError(f"access denied: {reason}", -32602)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.ctx.record_stamp(p)
        if str(p) not in self.ctx.edited_files:
            self.ctx.edited_files.append(str(p))
        self.log("tool/result", tool="fs/write_text_file", call_id=call_id, is_error=False,
                 output=f"wrote {len(content)} chars")
        return {}

    # ---- the run ----
    def start(self, env: dict) -> None:
        self.client = AcpClient(self.cfg["command"], Path(self.ctx.workspace), env)
        hs = float(self.cfg["handshake_timeout_s"])
        init = self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": False},
            "clientInfo": {"name": "artel", "title": "Artel", "version": _version()}}, hs)
        version = init.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise AcpError(f"ACP-агент говорит на версии протокола {version!r}, Artel поддерживает {PROTOCOL_VERSION}")
        self.agent_caps = init.get("agentCapabilities") or {}
        self.agent_info = init.get("agentInfo") or {}
        self.auth_methods = init.get("authMethods") or []
        if self.cfg.get("auth_method"):
            self.call("authenticate", {"methodId": self.cfg["auth_method"]}, hs)
        pkg_root = str(Path(__file__).resolve().parents[2])
        params: dict[str, Any] = {
            "cwd": str(Path(self.ctx.workspace).resolve()),
            "mcpServers": [{"name": "artel", "command": sys.executable,
                            "args": [PROXY_SCRIPT, self.bridge.url, self.bridge.token_arg],
                            "env": [{"name": "PYTHONPATH", "value": pkg_root}]}]}
        if (self.agent_caps.get("sessionCapabilities") or {}).get("additionalDirectories") is not None:
            extra = [str(r) for r in self.write_roots[1:] + self.refs if r != self.write_roots[0]]
            if extra:
                params["additionalDirectories"] = extra
        try:
            sess = self.call("session/new", params, hs)
        except AcpError as e:
            if e.code == -32000:
                methods = ", ".join(m.get("name") or m.get("id", "") for m in self.auth_methods) or "—"
                raise AcpError(f"ACP-агент «{self.cfg['name']}» требует входа (not logged in): войдите в его CLI "
                               f"или задайте acp.auth_method (доступно: {methods}). {e}", -32000) from None
            raise
        self.session_id = sess.get("sessionId")
        if not self.session_id:
            raise AcpError("session/new returned no sessionId")
        if self.agent_info.get("name"):
            self.model = f"acp:{self.cfg['name']}:{self.agent_info['name']}" + \
                (f"@{self.agent_info['version']}" if self.agent_info.get("version") else "")

    def prompt(self, text: str) -> str:
        """One prompt turn; returns the stop reason ('cancelled' after Artel cancelled it)."""
        self.cancelled = False
        self.tools = {}
        try:
            res = self.call("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
                            float(self.cfg["timeout_s"]))
        except AcpError as e:
            if self.cancelled and (self.ctx.terminal or e.code in (-32800, RPC_TIMEOUT) or "cancel" in str(e).lower()):
                return "cancelled"  # agents may answer a cancelled prompt with an error; that is not a failure
            raise
        finally:
            self.flush_message()
            self.close_open_tools()
        u = res.get("usage") or {}
        tin = int(u.get("inputTokens") or 0) + int(u.get("cachedReadTokens") or 0)
        tout = int(u.get("outputTokens") or 0) + int(u.get("thoughtTokens") or 0)
        if tin or tout:
            self.out.usage["input_tokens"] += tin
            self.out.usage["output_tokens"] += tout
            self.project.add_usage(tin, tout, cost_usd=0.0)
        self.heartbeat(force=True)
        return res.get("stopReason") or "end_turn"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("artel")
    except Exception:  # noqa: BLE001
        return "0"


def acp_env(cfg: dict) -> dict:
    env = child_env()
    env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
    return env


def run_acp(kernel, project, *, role: str, system: str, first_user: str, ctx: ToolContext, max_turns: int,
            purpose: str, persona=None, stop_event: Optional[threading.Event] = None,
            tool_names: Optional[list] = None, variant: Optional[str] = None) -> Outcome:
    from ..tools import resolve

    actor = persona.name if persona else role
    task_id = (ctx.task or {}).get("id")

    def fail(msg: str) -> Outcome:
        o = Outcome(status="error", error=msg)
        o.cost_usd = 0.0
        project.log.append("llm/error", actor=actor, task_id=task_id, error=msg, runtime="acp")
        return o

    try:
        cfg = acp_settings(kernel.config, variant)
    except AcpError as e:
        return fail(str(e))
    if not cfg["command"]:
        return fail("ACP-агент не настроен (not installed): задайте acp.command или acp.agent")
    binary = resolve(cfg["command"][0])
    if not binary:
        return fail(f"ACP-агент «{cfg['name']}» не установлен: команда `{cfg['command'][0]}` не найдена "
                    f"(not installed). Установите агента или укажите acp.command — «Подключения» → «ACP-агенты» (режим разработчика)")
    cfg["command"] = [binary, *cfg["command"][1:]]
    bridge = ToolBridge(kernel, project, ctx, role, tool_names, actor, runtime="acp")
    run = AcpRun(kernel, project, role=role, ctx=ctx, cfg=cfg, actor=actor, bridge=bridge, max_turns=max_turns,
                 stop_event=stop_event)
    out = run.out
    terminal_names = [s.name for s in bridge.specs if s.terminal]
    note = (f"\n\n## Runtime\nYou run as an ACP agent driven by Artel. Use your own tools for files and shell. Artel "
            f"coordination tools come from the MCP server `artel`: {', '.join(s.name for s in bridge.specs)}. The "
            f"ONLY way to finish is calling {' or '.join(terminal_names)} on that server. Your working directory "
            f"is your workspace; the team's shared deliverables directory is {project.shared}."
            + (" Your role is read-only: do not edit files." if role in READONLY_ROLES else "")
            + (f" Read-only reference sources: {', '.join(map(str, project.refs))}." if project.refs else ""))
    project.log.append("agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id, model=run.model,
                       effort=ctx.state.get("effort") or "default", system=system + note, user=first_user,
                       tools=[s.name for s in bridge.specs], runtime="acp", command=cfg["command"])
    budget = kernel.get("budget")
    prompt = f"<role_instructions>\n{system}{note}\n</role_instructions>\n\n{first_user}"
    try:
        try:
            run.start(acp_env(cfg))
        except (AcpError, OSError) as e:
            detail = str(e)
            tail = run.client.stderr_tail() if run.client else ""
            if tail and tail not in detail:
                detail += f"\n{tail}"
            out.status, out.error = "error", f"acp agent {cfg['name']} failed to start: {detail}"
            project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="acp")
            raise _Done()
        project.log.append("acp/session", actor=actor, task_id=task_id, runtime="acp", session_id=run.session_id,
                           agent=run.agent_info, capabilities=run.agent_caps)
        for attempt in range(1 + int(cfg["max_resumes"])):
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            reason = budget(project) if budget else None
            if reason:
                out.status, out.error = "budget", reason
                break
            try:
                stop = run.prompt(prompt)
            except AcpError as e:
                if ctx.terminal:
                    break
                detail = str(e)
                tail = run.client.stderr_tail(400)
                if tail and tail not in detail:
                    detail += f"\n{tail}"
                out.status, out.error = "error", f"acp agent {cfg['name']} failed: {detail}"
                project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="acp")
                break
            project.log.append("acp/turn_end", actor=actor, task_id=task_id, runtime="acp", stop_reason=stop)
            if ctx.terminal:
                break
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            if out.status == "turn_limit" or out.error.startswith("loop"):
                break
            if stop == "refusal":
                out.status, out.error = "error", f"acp agent {cfg['name']} refused to continue"
                project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="acp")
                break
            if attempt == int(cfg["max_resumes"]):
                break
            project.log.append("agent/continuation", actor=actor, task_id=task_id, idle_turns=attempt + 1,
                               runtime="acp", stop_reason=stop)
            prompt = ("\n\n".join(run.blocked) + "\n\n" if run.blocked else "") + CONTINUATION
            run.blocked = []
        if ctx.terminal:
            out.status, out.terminal = "terminal", ctx.terminal
        elif out.status == "stalled" and not out.error:
            out.error = f"no terminal tool after {cfg['max_resumes']} resumes"
    except _Done:
        pass
    finally:
        if run.client:
            run.cancel()
            run.client.close()
        bridge.close()
    if run.session_cost:
        out.cost_usd = run.session_cost
        project.add_usage(0, 0, cost_usd=run.session_cost)
    out.tool_calls += bridge.calls
    project.log.append("agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
                       terminal=out.name, turns=out.turns, tool_calls=out.tool_calls, usage=out.usage,
                       cost_usd=round(out.cost_usd, 4), error=out.error, runtime="acp", model=run.model)
    return out


class _Done(Exception):
    pass
