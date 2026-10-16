"""Minimal MCP client (JSON-RPC over stdio or streamable http) and the ``mcp_servers:`` config of artel.yaml.

External MCP servers are configured once and reach every runtime: the native runtime lists their tools and calls
them as Artel tools (``mcp__<server>__<tool>``, logged and policy-checked); Claude Code and Codex get the same
servers in their own MCP config next to Artel's bridge.

Config (artel.yaml)::

    mcp_servers:
      browser: {preset: playwright, allow: [worker]}
      docs:    {type: http, url: "https://x/mcp", headers: {Authorization: "Bearer ${TOKEN}"}, allow: [worker, reviewer]}
      tool:    {type: stdio, command: my-server, args: [--x], env: {KEY: "${KEY}"}, allow: [worker], enabled: true}

A server is visible only to the roles in its ``allow`` list; without one no role sees it. Roles that judge or plan
(reviewer, auditor, planner…) get a server only when it is marked ``read_only: true`` — the owner's statement that it
cannot change anything (static analysis, documentation). A browser server (Playwright) is always forced headless: it
never opens a window on the owner's screen. Secrets belong in ``${VAR}`` references: their values are never written
to artel.yaml, logs or a command line (Codex receives them through its environment, by variable name).

The shape of the client (initialize / tools/list / tools/call, name sanitising) follows the donor
opencode packages/opencode/src/mcp (MIT License, Copyright (c) 2025 opencode); the code is written for Artel.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from typing import Any, Optional

PROTOCOL = "2025-06-18"
TIMEOUT_S = 60.0
PREFIX = "mcp__"

PRESETS: dict[str, dict] = {
    "playwright": {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"]},
    "fetch": {"type": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]},
    "filesystem": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
}
_HEADED = re.compile(r"^--(no-headless|headed)$|^--headless=(false|0|no)$", re.I)
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class McpError(Exception):
    pass


def expand(value: Any, environ: Optional[dict] = None) -> Any:
    """``${VAR}`` / ``${VAR:-default}`` from the environment in strings, lists and dicts; unset -> empty."""
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


def _is_browser(command: str, args: list) -> bool:
    return any("@playwright/mcp" in str(x) for x in [command, *args])


def force_headless(args: list) -> list:
    """Drop every headed switch and make sure ``--headless`` is present exactly once."""
    out = [a for a in args if not _HEADED.match(str(a)) and str(a) != "--headless"]
    return [*out, "--headless"]


RESERVED = {"artel"}  # Artel's own bridge


def parse_servers(config: Optional[dict], environ: Optional[dict] = None) -> dict[str, dict]:
    """``config['mcp_servers']`` -> ``{name: {type, command, args, env | url, headers, allow}}`` (enabled ones only).

    Presets fill in what the entry leaves out; env and url/headers get ``${VAR}`` expansion; invalid entries are
    skipped. ``allow`` is a list of roles (empty = nobody).
    """
    out: dict[str, dict] = {}
    raw = (config or {}).get("mcp_servers") or {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        name = re.sub(r"[^A-Za-z0-9-]", "-", str(name)).strip("-")
        if not name or name in RESERVED or name in out or not isinstance(entry, dict) \
                or entry.get("enabled", True) is False:
            continue
        base = PRESETS.get(str(entry.get("preset") or (name if name in PRESETS and not (
            entry.get("command") or entry.get("url")) else "")), {})
        spec = {**base, **{k: v for k, v in entry.items() if k not in ("preset", "enabled")}}
        read_only = bool(spec.get("read_only"))
        if "args" in base and "args" in entry:
            spec["args"] = [*base["args"], *entry["args"]]
        if base is PRESETS["filesystem"] and len(spec["args"]) == len(base["args"]):
            continue  # which directory it may touch must be named; "the current one" differs between runtimes
        allow = spec.get("allow") or []
        allow = [allow] if isinstance(allow, str) else [str(r) for r in allow]
        if not read_only:  # a server that can write never reaches the roles that must not change what they judge
            from ..tooling import READONLY_ROLES

            allow = [r for r in allow if r not in READONLY_ROLES]
        kind = spec.get("type") or ("http" if spec.get("url") else "stdio")
        s: dict = {"type": kind, "allow": allow, "read_only": read_only}
        if kind == "http":
            if not spec.get("url"):
                continue
            s["url"] = expand(str(spec["url"]), environ)
            s["headers"] = {str(k): str(v) for k, v in expand(spec.get("headers") or {}, environ).items()}
        elif kind == "stdio":
            if not spec.get("command"):
                continue
            s["command"] = str(expand(spec["command"], environ))
            s["args"] = [str(a) for a in expand(list(spec.get("args") or []), environ)]
            s["env"] = {str(k): str(v) for k, v in expand(spec.get("env") or {}, environ).items()}
            if _is_browser(s["command"], s["args"]):
                s["args"] = force_headless(s["args"])
        else:
            continue
        out[name] = s
    return out


def servers_for_role(config: Optional[dict], role: str, environ: Optional[dict] = None) -> dict[str, dict]:
    """The servers a role may use: those whose ``allow`` list names it."""
    return {n: s for n, s in parse_servers(config, environ).items() if role in s["allow"]}


def tool_name(server: str, tool: str) -> str:
    return (PREFIX + server + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", tool))[:64]


_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)


def server_environ() -> dict:
    """The environment an external MCP server starts with: Artel's, minus every other secret (API keys, tokens);
    a server gets the secrets its own ``env`` entry names."""
    return {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}


class _Rpc:
    def request(self, method: str, params: Optional[dict] = None) -> Any: ...
    def notify(self, method: str, params: Optional[dict] = None) -> None: ...
    def close(self) -> None: ...


class StdioClient(_Rpc):
    """JSON-RPC over a child process's stdin/stdout, one JSON message per line."""

    def __init__(self, command: str, args: list, env: Optional[dict] = None, cwd: Optional[str] = None):
        from ..tools import resolve, shell_env

        try:
            self.proc = subprocess.Popen([resolve(command) or command, *args], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                         encoding="utf-8", errors="replace", cwd=cwd,
                                         env=shell_env({**server_environ(), **(env or {})}),
                                         start_new_session=os.name == "posix")
        except OSError as exc:
            raise McpError(f"cannot start MCP server {command!r}: {exc}")
        self._id = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, dict] = {}
        self._cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self._cond:
                    self._waiting[msg["id"]] = msg
                    self._cond.notify_all()
        with self._cond:
            self._waiting[-1] = {"error": {"message": "MCP server exited"}}
            self._cond.notify_all()

    def _send(self, msg: dict) -> None:
        try:
            with self._lock:  # parallel workers share the connection: one whole line at a time
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"MCP server is gone: {exc}")

    def request(self, method: str, params: Optional[dict] = None, timeout: float = TIMEOUT_S) -> Any:
        with self._lock:
            self._id += 1
            mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        with self._cond:
            if not self._cond.wait_for(lambda: mid in self._waiting or -1 in self._waiting, timeout):
                raise McpError(f"{method} timed out after {timeout:g}s")
            msg = self._waiting.pop(mid, None) or self._waiting[-1]
        if "error" in msg:
            raise McpError(str(msg["error"].get("message", msg["error"])))
        return msg["result"]

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        """Stop the server and what it started (npx → node → a browser): the whole process group."""
        import signal

        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if os.name == "posix":
                os.killpg(self.proc.pid, signal.SIGTERM)
            else:
                from ..tools import terminate_tree

                terminate_tree(self.proc)
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(self.proc.pid, signal.SIGKILL) if os.name == "posix" else self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


class HttpClient(_Rpc):
    """Streamable http transport: POST each message, the reply is JSON or an SSE stream."""

    def __init__(self, url: str, headers: Optional[dict] = None):
        self.url, self.headers, self.session, self._id = url, dict(headers or {}), None, 0
        self._lock = threading.Lock()

    def _post(self, body: dict, timeout: float) -> Optional[dict]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.session = r.headers.get("Mcp-Session-Id") or self.session
                raw = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
        except OSError as exc:
            raise McpError(f"http MCP request failed: {exc}")
        if "id" not in body or not raw.strip():
            return None
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == body["id"]:
                        return msg
            raise McpError("no response in event stream")
        return json.loads(raw)

    def request(self, method: str, params: Optional[dict] = None, timeout: float = TIMEOUT_S) -> Any:
        with self._lock:
            self._id += 1
            mid = self._id
            msg = self._post({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, timeout)
        if msg is None or "error" in msg:
            raise McpError(str((msg or {}).get("error", {}).get("message", "empty response")))
        return msg["result"]

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, TIMEOUT_S)

    def close(self) -> None:
        pass


class McpServer:
    """One configured server: connects lazily, initializes once, lists and calls tools."""

    def __init__(self, name: str, spec: dict, cwd: Optional[str] = None):
        self.name, self.spec, self.cwd = name, spec, cwd
        self._rpc: Optional[_Rpc] = None
        self._lock = threading.Lock()

    def _connect(self) -> _Rpc:
        with self._lock:
            if self._rpc is None:
                s = self.spec
                rpc = HttpClient(s["url"], s.get("headers")) if s["type"] == "http" else \
                    StdioClient(s["command"], s["args"], s.get("env"), self.cwd)
                try:
                    rpc.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                               "clientInfo": {"name": "artel", "version": "0.2"}})
                    rpc.notify("notifications/initialized")
                except Exception:
                    rpc.close()
                    raise
                self._rpc = rpc
            return self._rpc

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            res = self._connect().request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call(self, tool: str, arguments: dict, timeout: float = 300.0) -> tuple[str, bool]:
        res = self._connect().request("tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)
        parts = []
        for c in res.get("content") or []:
            parts.append(c.get("text", "") if c.get("type") == "text" else f"[{c.get('type', 'content')}]")
        if not parts and res.get("structuredContent") is not None:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(res.get("isError"))

    def close(self) -> None:
        with self._lock:
            if self._rpc is not None:
                self._rpc.close()
                self._rpc = None


def cli_config(config: Optional[dict], role: str) -> dict[str, dict]:
    """Servers for a role in the shape both CLI runtimes need (stdio: command/args/env; http: url/headers)."""
    out = {}
    for name, s in servers_for_role(config, role).items():
        out[name] = ({"url": s["url"], "headers": s.get("headers") or {}} if s["type"] == "http" else
                     {"command": s["command"], "args": s["args"], "env": s.get("env") or {}})
    return out


def env_name(server: str, key: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", f"ARTEL_MCP_{server}_{key}".upper())


def codex_secrets(servers: dict) -> tuple[dict, dict]:
    """Codex takes MCP env values and headers by variable name (``env_vars``, ``env_http_headers``), so no value
    reaches its command line. Returns (servers rewritten to names, the variables to put into Codex's environment)."""
    out, inject = {}, {}
    for name, s in servers.items():
        if "url" in s:
            names = {h: env_name(name, h) for h in s.get("headers") or {}}
            inject.update({names[h]: v for h, v in (s.get("headers") or {}).items()})
            out[name] = {"url": s["url"], "env_http_headers": names}
        else:
            inject.update(s.get("env") or {})
            out[name] = {"command": s["command"], "args": s["args"], "env_vars": sorted(s.get("env") or {})}
    return out, inject


def register_tools(kernel) -> Any:
    """Register ``mcp__<server>__<tool>`` Artel tools for every configured server (setup of builtin_tools).

    A server that fails to start or list is skipped (recorded in ``kernel.failed``), never fatal. Returns a teardown
    that closes the connections.
    """
    from ..kernel import ToolSpec
    from . import runtime_for

    servers = []
    for name, spec in parse_servers(kernel.config).items():
        # Claude Code and Codex start their own copy of the server; Artel connects only for roles that run natively
        native = [r for r in spec["allow"] if _native(kernel.config, r, runtime_for)]
        if not native:
            continue
        spec = {**spec, "allow": native}
        srv = McpServer(name, spec)
        try:
            tools = srv.list_tools()
        except Exception as exc:  # noqa: BLE001
            kernel.failed[f"mcp:{name}"] = str(exc)
            srv.close()
            continue
        servers.append(srv)
        seen = set()
        for t in tools:
            if not t.get("name"):
                continue
            full = tool_name(name, t["name"])
            if full in seen or full in kernel.tools:  # a sanitised or truncated name that collides: keep the first
                continue
            seen.add(full)

            fn = _tool_fn(srv, t["name"], full)
            roles = spec["allow"]
            if (t.get("annotations") or {}).get("destructiveHint") is True:  # never for judges, even on read_only
                from ..tooling import READONLY_ROLES

                roles = [r for r in roles if r not in READONLY_ROLES]
            schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
            schema = {"type": "object", "properties": {}, **schema}
            desc = str(t.get("description") or t["name"])[:1000]  # untrusted text from the server: bounded
            if roles:
                kernel.register_tool(ToolSpec(full, f"[MCP {name}] {desc}", schema, fn, roles=tuple(roles)))

    def close() -> None:
        for s in servers:
            s.close()

    return close


def _tool_fn(srv: "McpServer", tool: str, full: str):
    """The Artel tool that calls one MCP tool (ctx is positional-only: a tool argument may be called anything)."""
    from .. import policy
    from ..tooling import ToolError

    def fn(ctx, /, **args: Any) -> str:
        cmd = f"{full} {json.dumps(args, ensure_ascii=False, sort_keys=True)}"
        try:
            verdict = policy.evaluate(cmd, ctx.project.root)
        except policy.PolicyError as exc:
            raise ToolError(f"command policy is invalid, MCP tools disabled until fixed: {exc}")
        if verdict.decision == "forbidden":
            raise ToolError(f"MCP call blocked by policy ({verdict.rule.id}): {verdict.justification or 'forbidden'}")
        if verdict.decision == "prompt":
            ctx.terminal = policy.prompt_terminal(verdict, cmd)
            return f"MCP call NOT run: policy requires human approval ({verdict.justification})"
        ctx.project.log.append("mcp/call", actor=ctx.actor, server=srv.name, tool=tool,
                               args=json.dumps(args, ensure_ascii=False)[:500])
        try:
            text, is_error = srv.call(tool, args, timeout=float(ctx.kernel.config.get("tool_timeout_s", 300)))
        except McpError as exc:
            raise ToolError(f"MCP server {srv.name}: {exc}")
        if is_error:
            raise ToolError(text or "MCP tool returned an error")
        from ..plugins.builtin_tools import _bound

        return _bound(ctx, text, "mcp") or "(empty result)"

    return fn


def _native(config: dict, role: str, runtime_for) -> bool:
    try:
        return runtime_for(config, role) == "native"
    except Exception:  # noqa: BLE001 - routing errors surface elsewhere
        return False


if __name__ == "__main__":  # pragma: no cover - debugging aid: python -m artel.runtimes.mcp_client <command> [args]
    srv = McpServer("cli", {"type": "stdio", "command": sys.argv[1], "args": sys.argv[2:], "env": {}})
    print(json.dumps(srv.list_tools(), indent=2))
    srv.close()
