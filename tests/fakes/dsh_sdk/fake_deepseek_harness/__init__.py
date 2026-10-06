"""Stand-in for the DeepSeek Harness Python SDK (``deepseek_harness``) in tests.

Mirrors the public surface Artel uses — ``DeepSeekHarness(**options)``, ``start()``, ``run(prompt, session_id=,
on_notification=)``, ``close()`` — and the wire shapes of ``session.event`` notifications. Like the real runtime it
reads the ``--patch`` layers, starts the stdio MCP server that the ``@deepseek-ai/dsh-mcp-client`` row names, and
calls Artel's tools through it as ``mcp__artel__<tool>``. Every launch is logged to $FAKE_DSH_LOG.
"""
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path


class TransportClosedError(Exception):
    pass


@dataclass
class Notification:
    method: str
    payload: dict


@dataclass
class RunResult:
    session_id: str
    final_response: str
    finish_reason: str
    events: list = field(default_factory=list)
    notifications: list = field(default_factory=list)


_runs: dict = {}  # session id -> prompts seen (the durable session survives a runtime restart)


class DeepSeekHarness:
    def __init__(self, **options):
        self.opts = options
        self.closed = threading.Event()
        self.mcp = None
        self._id = 0

    # -- MCP over stdio, like dsh-mcp-client -------------------------------------------------------------------
    def _rpc(self, method, params=None):
        self._id += 1
        self.mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
                             + "\n")
        self.mcp.stdin.flush()
        return json.loads(self.mcp.stdout.readline())["result"]

    def start(self):
        rows = []
        for patch in self.opts.get("patches") or ():
            text = Path(patch).read_text()
            try:
                rows += json.loads(text)
            except ValueError:
                pass  # a user YAML layer: the fake does not interpret it
        mcp = next(r for layer in rows for r in layer.get("insert", []) if r["name"] == "@deepseek-ai/dsh-mcp-client")
        c = mcp["config"]
        env = {**os.environ, **(self.opts.get("env") or {}), **c.get("env", {})}
        self.mcp = subprocess.Popen([c["command"], *c["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    text=True, env=env)
        self._rpc("initialize", {"protocolVersion": "2025-06-18"})
        tools = ["mcp__%s__%s" % (c["serverName"], t["name"]) for t in self._rpc("tools/list")["tools"]]
        log = Path(os.environ["FAKE_DSH_LOG"])
        rec = {"opts": {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.opts.items() if k != "env"},
               "permission_mode": (self.opts.get("env") or {}).get("DSH_PERMISSION_MODE"), "tools": tools, "rows": rows}
        with log.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        if os.environ.get("FAKE_DSH_BOOT_FAIL"):
            raise TransportClosedError("DeepSeek Harness runtime stdout closed\nexit code: 1")

    def close(self):
        self.closed.set()
        if self.mcp and self.mcp.poll() is None:
            self.mcp.terminate()
            self.mcp.wait()

    def run(self, prompt, *, session_id=None, on_notification=None):
        prompts = _runs.setdefault(session_id, [])
        prompts.append(prompt)
        resumed = len(prompts) > 1
        events = []
        turn = len(prompts)

        def emit(typ, data):
            if self.closed.is_set():
                raise TransportClosedError("DeepSeek Harness runtime closed")
            ev = {"type": typ, "data": data}
            events.append(ev)
            if on_notification:
                on_notification(Notification("session.event", {"sessionId": session_id, "event": ev}))

        def end(kind, **extra):
            emit("turn/end", {"turn": turn, "reason": {"kind": kind, **extra}})
            return RunResult(session_id, "", kind, events)

        emit("turn/start", {"turn": turn})
        emit("request/header", {"header": {"config": {"provider": "deepseek-official",
                                                      "model": self.opts.get("model", "fake-default")}}})
        if os.environ.get("FAKE_DSH_LIMIT"):
            return end("error", error={"message": "Rate limit reached for requests", "code": "RATE_LIMIT",
                                       "status": 429})
        cmd = os.environ.get("FAKE_DSH_CMD") or "python hello.py"
        emit("assistant/message", {"turn": turn, "step": 1, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Finishing." if resumed else "Writing the file."},
            {"type": "tool-call", "id": "c1", "name": "write", "arguments": json.dumps({"path": "hello.py"})},
            {"type": "tool-call", "id": "c2", "name": "bash", "arguments": json.dumps({"command": cmd})}]},
            "usage": {"inputTokens": 1000, "outputTokens": 200}})
        emit("tool/call", {"turn": turn, "step": 1, "callId": "c1", "name": "write",
                           "arguments": json.dumps({"path": "hello.py", "content": "print('hi')\n"})})
        Path(self.opts["cwd"], "hello.py").write_text("print('hi')\n")
        emit("tool/result", {"turn": turn, "step": 1, "message": {"role": "tool", "toolCallId": "c1",
                                                                   "content": [{"type": "text", "text": "written"}]}})
        emit("tool/call", {"turn": turn, "step": 1, "callId": "c2", "name": "bash",
                           "arguments": json.dumps({"command": cmd})})
        if os.environ.get("FAKE_DSH_CMD"):  # Artel's policy must stop the runtime before the command finishes
            self.closed.wait(5)
            raise TransportClosedError("DeepSeek Harness runtime closed")
        emit("tool/result", {"turn": turn, "step": 1, "message": {"role": "tool", "toolCallId": "c2",
                                                                   "content": [{"type": "text", "text": "hi\n"}]}})
        if os.environ.get("FAKE_DSH_LAZY") and not resumed:  # ends without a terminal tool -> Artel continues
            return end("completed")
        args = {"summary": "hello.py prints hi", "evidence": "$ python hello.py\nhi"}
        emit("tool/call", {"turn": turn, "step": 2, "callId": "c3", "name": "mcp__artel__task_submit",
                           "arguments": json.dumps(args)})
        r = self._rpc("tools/call", {"name": "task_submit", "arguments": args})
        emit("tool/result", {"turn": turn, "step": 2, "message": {"role": "tool", "toolCallId": "c3",
                                                                   "content": r["content"]}})
        self.closed.wait(5)  # a real model would keep going; Artel closes the runtime once the terminal is accepted
        if self.closed.is_set():
            raise TransportClosedError("DeepSeek Harness runtime closed")
        return end("completed")
