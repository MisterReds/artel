"""The agent loop both CLI runtimes share (Claude Code, Codex): written once, driven by a codec module.

A codec (artel/adapters/agent/claude_code.py, codex.py) knows one CLI: its settings, environment, arguments and how to
turn one stdout line into :class:`AgentEvent` objects. This module does the rest: the Artel tool bridge (MCP proxy),
process start in its own group with a stderr tail and a timeout kill, the stdout line loop, mirroring of the events
into the project journal, the policy gate, loop detection, the terminal tool and its usage grace, resume attempts and
the :class:`Outcome` the callers use.

Codec interface (module attributes): NAME, LABEL, DEFAULTS, INSTALL_HINT, NO_SESSION, OK_EXIT, TRACK_STOP, LOG_ROLE,
GATE_TOOL, SHELL_TOOL, TRACK_MISSING_USAGE, STRIP_STDERR, [TERMINAL_GRACE_S]; settings(config), prepare(run), note(run), first_prompt(),
start_info(run), model_seen(cc), launch(run), parse(line), loop_msg(name, input), [sandbox_recover(cc, text)].
Data keys of AgentEvent the loop understands: text/tool_call "response" ({"n", "fields"} on the first event of one
model response), tool_call "bridged"/"label"/"turn"/"observe"/"edited"/"phase", tool_result "digest"/"sandbox_broken",
usage "cached"/"cost_usd", end "detail", error "fatal", command "phase".
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from ... import policy
from ...agent import LoopDetector, Outcome, _short, execute_tool
from ...contracts._base import thaw
from ...prompts import CONTINUATION
from ...tooling import ToolContext, present_output
from ...tools import StderrTail, group_popen_kwargs, terminate_tree

# The CLIs ship better versions of these; do not expose Artel's duplicates.
NATIVE_DUPLICATES = {"bash", "read_file", "write_file", "edit_file", "list_files"}
BROKEN_KEY = "codex_sandbox_broken"  # ctx.state key: the CLI's own sandbox failed to start a command


class ToolBridge:
    """Localhost HTTP endpoint that executes Artel tools for one run."""

    def __init__(self, kernel, project, ctx: ToolContext, role: str, tool_names: Optional[list], actor: str,
                 runtime: str = "claude_code"):
        self.kernel, self.project, self.ctx, self.actor, self.runtime = kernel, project, ctx, actor, runtime
        # external MCP servers are handed to the CLI directly (see the codecs), not proxied through the bridge
        self.specs = [s for s in kernel.tools_for(role, tool_names)
                      if s.name not in NATIVE_DUPLICATES and not s.name.startswith("mcp__")]
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

        from ...httpd import LocalHTTPServer

        self.httpd = LocalHTTPServer(("127.0.0.1", 0), Handler)
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
        return {"text": present_output(self.ctx, name, text), "is_error": is_error}

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.token_file.unlink(missing_ok=True)


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


class Run:
    """Everything one run shares between the loop and its codec."""

    def __init__(self, codec, kernel, project, *, role, system, first_user, ctx, max_turns, purpose, persona,
                 stop_event, tool_names, cc, actor):
        self.codec, self.kernel, self.project, self.role, self.system, self.first_user = codec, kernel, project, role, system, first_user
        self.ctx, self.max_turns, self.purpose, self.persona, self.stop_event = ctx, max_turns, purpose, persona, stop_event
        self.tool_names, self.cc, self.actor = tool_names, cc, actor
        self.task_id = (ctx.task or {}).get("id")
        self.out = Outcome(status="stalled")
        self.out.cost_usd = 0.0
        self.bridge: Optional[ToolBridge] = None
        self.detector = LoopDetector()
        self.state: dict = {}          # the codec's own, per run
        self.session: Optional[str] = None
        self.prompt = first_user
        self.system_note = system
        self.model = ""
        self.pending: dict = {}        # call id -> (tool name, input, observe late)
        self.proc = None
        self.stopped = False           # the process was ended by Artel (loop, limit, policy, terminal tool)
        self.got_usage = False
        self.failed = False
        self.failure = ""
        self.blocked = ""

    def log(self, kind: str, **fields) -> None:
        self.project.log.append(kind, actor=self.actor, task_id=self.task_id, **fields)

    def kill(self) -> None:
        self.stopped = self.stopped or terminate_tree(self.proc)

    # --- events -> journal -------------------------------------------------------------------------------------
    def consume(self, events) -> None:
        i, step = 0, False
        while i < len(events):
            ev = events[i]
            resp = ev.data.get("response")
            if resp is not None:
                n = int(resp["n"])
                self._response(events[i:i + n], thaw(resp["fields"]))
                i += n
                step = True
                continue
            i += 1
            if ev.kind == "session":
                self.session = ev.data["id"] or self.session
                self.model = ev.data.get("model") or self.model
            elif ev.kind == "usage":
                self._usage(ev.data)
            elif ev.kind == "end":
                self.failed, self.failure = ev.data["is_error"], str(ev.data.get("detail") or "")
            elif ev.kind == "error":
                if ev.data.get("fatal"):
                    self.failure = ev.data["message"]
                    self.failed = bool(self.failure)
                else:
                    self.log("llm/error", error=ev.data["message"], runtime=self.codec.NAME)
                    step = True
            elif ev.kind == "command":
                self._command(ev.data["command"])
            elif ev.kind == "tool_call":
                step = step or ev.data.get("phase") != "start"
                self._tool_call(ev)
            elif ev.kind == "tool_result":
                self._tool_result(ev)
            elif ev.kind == "text":
                step = True
        if step:
            if self.ctx.run_id:
                self.project.board.heartbeat(self.ctx.run_id, turns=self.out.turns,
                                             tool_calls=self.out.tool_calls + self.bridge.calls)
            if self.out.turns >= self.max_turns:
                self.out.status, self.out.error = "turn_limit", f"{self.max_turns} turns"
                self.kill()

    def _response(self, group, fields: dict) -> None:
        out, codec = self.out, self.codec
        blocks = [{"type": "text", "text": e.data["text"]} if e.kind == "text" else
                  {"type": "tool_use", "id": e.data["id"], "name": e.data["name"], "input": thaw(e.data["input"])}
                  for e in group]
        out.turns += 1
        model = fields.pop("model", "") or self.model
        self.log("llm/response", **({"role": self.role} if codec.LOG_ROLE else {}), turn=out.turns, model=model,
                 **fields, blocks=blocks, runtime=codec.NAME)
        for e in group:
            if e.kind == "text":
                if e.data["text"].strip():
                    out.digest.append(f"ASSISTANT: {_short(e.data['text'], 600)}")
            else:
                self._tool_call(e)

    def _usage(self, d) -> None:
        out = self.out
        tin, tout, cost = int(d["input_tokens"]), int(d["output_tokens"]), float(d.get("cost_usd") or 0.0)
        out.usage["input_tokens"] += tin
        out.usage["output_tokens"] += tout
        if "cached" in d:
            out.usage["cached"] = out.usage.get("cached", 0) + int(d["cached"])
        out.cost_usd += cost
        self.got_usage = True
        self.project.add_usage(tin, tout, cost_usd=cost)

    def _observe(self, name: str, inp: dict) -> None:
        verdict = self.detector.observe_tool(name, inp)
        if verdict:
            detail, error = self.codec.loop_msg(name, inp)
            self.log("agent/loop", loop_kind="tool", stage=verdict, details={"tool": name, "input": detail})
            if verdict == "stop":
                self.out.status, self.out.error = "stalled", error
                self.kill()

    def _tool_call(self, e) -> None:
        d, out = e.data, self.out
        if d.get("phase") == "start":
            return  # the CLI started something: only the closing-grace check cares
        name, inp = d["name"], thaw(d["input"])
        if d.get("turn"):
            out.turns += 1
        if d.get("bridged"):  # an Artel tool: the bridge logged the call and its result
            out.digest.append(f"TOOL {d.get('label') or name}({_short(inp)})")
            return
        observe = d.get("observe")
        if observe == "first":
            self._observe(name, inp)
        if name == self.codec.GATE_TOOL and _policy_gate(self.project, self.ctx, self.actor, self.task_id, inp, self.proc):
            out.digest.append(f"POLICY blocked: {_short(inp)}")
        out.tool_calls += 1
        self.pending[d["id"]] = (name, inp, observe == "late")
        self.log("tool/call", tool=name, input=inp, call_id=d["id"], runtime=self.codec.NAME)
        self.ctx.edited_files.extend(d.get("edited") or ())

    def _tool_result(self, e) -> None:
        d = e.data
        ent = self.pending.pop(d["id"], None)
        if ent is None:
            return
        name, inp, late = ent
        text = d["output"]
        self.log("tool/result", tool=name, call_id=d["id"], is_error=d["is_error"], output=text[:4000],
                 runtime=self.codec.NAME)
        if d.get("digest", True):
            self.out.digest.append(f"TOOL {name}({_short(inp)}) -> {_short(text, 300)}")
        if name == self.codec.SHELL_TOOL:
            self.ctx.outputs.append(f"$ {inp.get('command', '')}\n{text[:3000]}")
        if d.get("sandbox_broken"):
            # the command never ran: the CLI's sandbox itself failed. Stop now, before the worker gives up on its tools.
            self.ctx.state[BROKEN_KEY] = d["sandbox_broken"]
            self.kill()
        elif late:
            self._observe(name, inp)

    def _command(self, cmd: str) -> None:
        # a CLI without a pre-exec hook: stop the process as soon as a disallowed command starts
        verdict = policy.evaluate(cmd, self.project.root)
        if verdict.decision == "allow":
            return
        self.log("policy/blocked", command=cmd, decision=verdict.decision, rule=verdict.rule.id,
                 justification=verdict.justification, runtime=self.codec.NAME)
        if verdict.decision == "prompt":
            self.ctx.terminal = policy.prompt_terminal(verdict, cmd)
        else:
            self.blocked = (f"The command `{cmd}` was blocked by policy ({verdict.rule.id}): "
                            f"{verdict.justification or 'forbidden'}. It was not run; choose another way.")
        self.kill()


def _read(run: Run, proc, grace_s: Optional[float]) -> Optional[threading.Timer]:
    """The stdout line loop. Returns the grace timer when one was started (the caller cancels it)."""
    grace: Optional[threading.Timer] = None
    ctx, codec = run.ctx, run.codec
    for line in proc.stdout:
        if run.stop_event is not None and run.stop_event.is_set():
            run.kill()
        try:
            events = codec.parse(line)
        except Exception as exc:  # noqa: BLE001 - a malformed CLI line must not end the read loop with the CLI alive
            run.project.log.append("llm/error", actor=run.actor, error=f"unparsable {codec.NAME} line: {exc}"[:300])
            events = []
        run.consume(events)
        if grace_s is not None and ctx.terminal and not run.stopped:
            # the run is finished as soon as Artel accepted a terminal tool; do not let the CLI continue. Its closing
            # usage event follows shortly: keep reading until it arrives or the grace passes, but stop at once if the
            # CLI starts anything new.
            if any(e.kind == "usage" or e.data.get("phase") == "start" for e in events) or \
                    grace is not None and not grace.is_alive():
                run.stopped = terminate_tree(proc)
            elif grace is None:
                grace = threading.Timer(grace_s, terminate_tree, (proc,))
                grace.start()
    return grace


def run_agent(codec, kernel, project, *, role: str, system: str, first_user: str, ctx: ToolContext, max_turns: int,
              purpose: str, persona=None, stop_event: Optional[threading.Event] = None,
              tool_names: Optional[list] = None) -> Outcome:
    from ...routing import model_for
    from ...tools import resolve

    cc = codec.settings(kernel.config)
    tiered = None if (persona and getattr(persona, "model", None)) else model_for(kernel.config, codec.NAME, role, project)
    if tiered:  # the role's tier: the strongest model only for the manager's decisions (artel/routing.py)
        cc = {**cc, "model": tiered}
    actor = persona.name if persona else role
    task_id = (ctx.task or {}).get("id")
    if not resolve(cc["binary"]):
        out = Outcome(status="error", error=codec.INSTALL_HINT)
        out.cost_usd = 0.0
        project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime=codec.NAME)
        return out
    run = Run(codec, kernel, project, role=role, system=system, first_user=first_user, ctx=ctx, max_turns=max_turns,
              purpose=purpose, persona=persona, stop_event=stop_event, tool_names=tool_names, cc=cc, actor=actor)
    out = run.out
    run.bridge = bridge = ToolBridge(kernel, project, ctx, role, tool_names, actor, runtime=codec.NAME)
    plan = codec.prepare(run)
    for d in plan.get("mkdirs", ()):
        d.mkdir(parents=True, exist_ok=True)
    for path, text in plan.get("files", {}).items():  # may hold secrets: readable by the owner only
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            fh.write(text)
    note = codec.note(run)
    run.system_note = system + note
    run.prompt = codec.first_prompt(system, note, first_user)
    run.model = codec.model_seen(cc)
    info = codec.start_info(run)
    project.log.append("agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id, model=info["model"],
                       effort=info["effort"], system=system + note, user=first_user, tools=info["tools"],
                       runtime=codec.NAME)
    budget = kernel.get("budget")
    usage_missing = False
    try:
        for attempt in range(1 + int(cc["max_resumes"])):
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            reason = budget(project) if budget else None
            if reason:
                out.status, out.error = "budget", reason
                break
            launch = codec.launch(run)
            for kind, fields in launch.get("journal", ()):
                run.log(kind, **fields)
            stdin = launch.get("stdin")
            proc = run.proc = subprocess.Popen(
                launch["args"], cwd=launch["cwd"], **({"stdin": subprocess.PIPE} if stdin is not None else {}),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=launch["env"], bufsize=1,
                encoding="utf-8", errors="replace", **group_popen_kwargs())
            err_tail = StderrTail(proc)
            if stdin is not None:
                proc.stdin.write(stdin)
                proc.stdin.close()
            killer = threading.Timer(float(cc["timeout_s"]), terminate_tree, (proc, True))
            killer.start()
            run.pending, run.got_usage, run.failed, run.failure, run.blocked, run.stopped = {}, False, False, "", "", False
            grace: Optional[threading.Timer] = None
            try:
                grace = _read(run, proc, getattr(codec, "TERMINAL_GRACE_S", None))
            finally:
                killer.cancel()
                if grace is not None:
                    grace.cancel()
                    run.stopped = True
                if proc.poll() is None and sys.exc_info()[0] is not None:
                    terminate_tree(proc, True)  # leaving on an exception: never wait for a CLI that keeps running
                proc.wait()
            usage_missing = not run.got_usage  # the last attempt decides
            stderr = err_tail.text()
            broken = ctx.state.pop(BROKEN_KEY, None)
            if broken and not ctx.terminal:
                fix = codec.sandbox_recover(cc, broken)
                if fix.get("error"):
                    out.status, out.error = "error", fix["error"]
                    project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime=codec.NAME)
                    break
                run.log(fix["journal"][0], **fix["journal"][1])
                run.prompt = fix["prompt"]
                continue
            if ctx.terminal:
                out.status, out.terminal = "terminal", ctx.terminal
                break
            if out.status in ("turn_limit", "stopped") or out.error.startswith("loop"):
                break
            if run.failed or (proc.returncode not in codec.OK_EXIT and not (codec.TRACK_STOP and run.stopped)):
                detail = run.failure or (stderr.strip() if codec.STRIP_STDERR else stderr)[-800:] or f"exit {proc.returncode}"
                out.status, out.error = "error", f"{codec.LABEL} failed: {detail}"
                project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime=codec.NAME)
                break
            if not run.session:
                out.status, out.error = "error", codec.NO_SESSION
                break
            run.log("agent/continuation", idle_turns=attempt + 1, runtime=codec.NAME)
            run.prompt = (run.blocked + "\n\n" if run.blocked else "") + CONTINUATION
        else:
            out.status, out.error = "stalled", f"no terminal tool after {cc['max_resumes']} resumes"
    finally:
        bridge.close()
        for p in plan.get("cleanup", ()):
            p.unlink(missing_ok=True)
    out.tool_calls += bridge.calls
    project.log.append("agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
                       terminal=out.name, turns=out.turns, tool_calls=out.tool_calls, usage=out.usage,
                       cost_usd=round(out.cost_usd, 4), error=out.error, runtime=codec.NAME, model=run.model,
                       **({"usage_missing": True} if usage_missing and codec.TRACK_MISSING_USAGE else {}))
    return out
