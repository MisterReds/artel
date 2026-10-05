"""Minimal MCP stdio server that forwards tool calls to an Artel ToolBridge over HTTP.

Spawned by Claude Code via --mcp-config. Implements initialize, tools/list, tools/call.
"""

from __future__ import annotations

import json
import sys
import urllib.request

PROTOCOL = "2025-06-18"


def _http(url: str, token: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", "X-Artel-Token": token})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read())


def main() -> None:
    url, token = sys.argv[1], sys.argv[2]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "artel", "version": "0.2"}}
            elif method == "tools/list":
                result = {"tools": _http(url, token, "/tools")["tools"]}
            elif method == "tools/call":
                p = msg["params"]
                r = _http(url, token, "/call", {"name": p["name"], "args": p.get("arguments") or {}})
                result = {"content": [{"type": "text", "text": r["text"]}], "isError": bool(r["is_error"])}
            elif method == "ping":
                result = {}
            else:
                out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()
                continue
            out = {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # noqa: BLE001 - report to the client, keep serving
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": repr(exc)}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
