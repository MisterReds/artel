import sys
from pathlib import Path

import pytest

from artel.agent import execute_tool
from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.runtimes import mcp_client
from artel.runtimes.claude_code import mcp_servers_json
from artel.runtimes.codex import build_args
from artel.tooling import ToolContext

FAKE = str(Path(__file__).parent / "fakes" / "mcp_server")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_SECRET", "s3cret")
    servers = {"fake": {"type": "stdio", "command": sys.executable, "args": [FAKE], "allow": ["worker"],
                        "env": {"TOKEN": "${FAKE_SECRET}", "OTHER": "${NOPE_UNSET:-dflt}"}}}
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05, "mcp_servers": servers})
    project, _ = run_goal("mcp", cfg)
    return cfg, project


def ctx_for(k, project, role):
    return ToolContext(k, project, role=role, workspace=project.shared)


def test_native_worker_calls_fake_server_tool(env):
    cfg, project = env
    k = new_kernel(cfg)
    assert "mcp__fake__echo" in [s.name for s in k.tools_for("worker")]
    out, err = execute_tool(k, ctx_for(k, project, "worker"), "mcp__fake__echo", {"text": "hi"})
    assert (out, err) == ("echo: hi", False)
    assert any(e["kind"] == "mcp/call" and e["tool"] == "echo" for e in project.log.read())


def test_env_expansion_reaches_server(env):
    cfg, project = env
    k = new_kernel(cfg)
    assert execute_tool(k, ctx_for(k, project, "worker"), "mcp__fake__env", {"name": "TOKEN"}) == ("s3cret", False)
    assert execute_tool(k, ctx_for(k, project, "worker"), "mcp__fake__env", {"name": "OTHER"}) == ("dflt", False)


def test_role_without_grant_sees_nothing(env):
    cfg, project = env
    k = new_kernel(cfg)
    assert not [s for s in k.tools_for("reviewer") if s.name.startswith("mcp__")]
    out, err = execute_tool(k, ctx_for(k, project, "reviewer"), "mcp__fake__echo", {"text": "x"})
    assert err and "unknown tool" in out
    assert mcp_client.servers_for_role(cfg, "reviewer") == {}
    assert list(mcp_client.servers_for_role(cfg, "worker")) == ["fake"]
    # no allow list at all: nobody sees it
    assert mcp_client.servers_for_role({"mcp_servers": {"x": {"command": "c"}}}, "worker") == {}


def test_policy_blocks_mcp_call(env, tmp_path):
    cfg, project = env
    (tmp_path / "home" / "policy.yaml").write_text(
        "rules:\n- {id: no-echo, prefix: mcp__fake__echo, decision: forbidden, justification: nope}\n", encoding="utf-8")
    k = new_kernel(cfg)
    out, err = execute_tool(k, ctx_for(k, project, "worker"), "mcp__fake__echo", {"text": "x"})
    assert err and "no-echo" in out


def test_playwright_preset_is_headless_and_cannot_be_headed():
    cfg = {"mcp_servers": {"browser": {"preset": "playwright", "allow": ["worker"],
                                       "args": ["--headed", "--no-headless", "--headless=false", "--browser", "chromium"]},
                           "playwright": {"allow": ["worker"]},
                           "raw": {"command": "npx", "args": ["@playwright/mcp", "--headed"], "allow": ["worker"]}}}
    for name, s in mcp_client.parse_servers(cfg).items():
        assert s["args"].count("--headless") == 1, name
        assert not any("headed" in a or a == "--headless=false" for a in s["args"]), name
    assert mcp_client.parse_servers(cfg)["browser"]["args"][-3:] == ["--browser", "chromium", "--headless"]
    assert {"fetch", "filesystem"} <= set(mcp_client.parse_servers(
        {"mcp_servers": {"fetch": {"allow": ["worker"]}, "filesystem": {"allow": ["worker"], "args": ["/data"]}}}))
    # without a directory the filesystem preset is refused: "the current directory" differs between runtimes
    assert "filesystem" not in mcp_client.parse_servers({"mcp_servers": {"filesystem": {"allow": ["worker"]}}})


def test_disabled_and_invalid_skipped():
    cfg = {"mcp_servers": {"a": {"command": "c", "enabled": False}, "b": {"type": "http"}, "c": {"type": "ws"}}}
    assert mcp_client.parse_servers(cfg) == {}


def test_cli_runtimes_get_the_servers(env, tmp_path):
    cfg, project = env
    ext = mcp_client.cli_config(cfg, "worker")
    assert mcp_client.cli_config(cfg, "reviewer") == {}
    assert mcp_servers_json(ext, {"artel": {}})["fake"]["args"] == [FAKE]
    ws = project.root / "w"
    a = build_args({"binary": "codex", "sandbox": "workspace-write"}, workspace=ws, shared=project.shared, role="worker",
                   bridge_url="u", bridge_token="t", effort=None, thread_id=None, last_message=tmp_path / "m",
                   mcp_servers=mcp_client.codex_secrets(ext)[0])
    assert "mcp_servers.fake.command=" + '"' + sys.executable + '"' in a
    # the secret reaches Codex's environment by name, never its command line (visible in `ps`)
    assert any(x.startswith("mcp_servers.fake.env_vars=") for x in a) and not any("s3cret" in x for x in a)
    assert "s3cret" in mcp_client.codex_secrets(ext)[1].values()
    # the native bridge does not proxy them a second time
    from artel.runtimes.claude_code import ToolBridge
    k = new_kernel(cfg)
    b = ToolBridge(k, project, ctx_for(k, project, "worker"), "worker", None, "w")
    assert not [s for s in b.specs if s.name.startswith("mcp__")]


def test_http_transport_json_and_sse():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "id" not in msg:
                self.send_response(202)
                self.end_headers()
                return
            res = {"tools": [{"name": "t", "inputSchema": {}}]} if msg["method"] == "tools/list" else \
                {"content": [{"type": "text", "text": "via http"}]} if msg["method"] == "tools/call" else {}
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res})
            sse = msg["method"] == "tools/call"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Mcp-Session-Id", "abc")
            self.end_headers()
            self.wfile.write((f"event: message\ndata: {body}\n\n" if sse else body).encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = mcp_client.McpServer("h", {"type": "http", "url": f"http://127.0.0.1:{srv.server_port}/mcp", "allow": []})
        assert [t["name"] for t in s.list_tools()] == ["t"]
        assert s.call("t", {}) == ("via http", False)
    finally:
        srv.shutdown()


def test_judges_get_only_read_only_servers_and_never_destructive_tools():
    """A server that can write must not reach the roles that judge (reviewer, auditor, planner…)."""
    cfg = {"mcp_servers": {"browser": {"preset": "playwright", "allow": ["worker", "reviewer"]},
                           "docs": {"command": "x", "allow": ["worker", "reviewer"], "read_only": True},
                           "artel": {"command": "evil", "allow": ["worker"]}}}
    s = mcp_client.parse_servers(cfg)
    assert s["browser"]["allow"] == ["worker"] and s["docs"]["allow"] == ["worker", "reviewer"]
    assert "artel" not in s  # the bridge's name is reserved
    assert set(mcp_client.servers_for_role(cfg, "reviewer")) == {"docs"}


def test_external_servers_do_not_inherit_artels_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = mcp_client.server_environ()
    assert "ANTHROPIC_API_KEY" not in env and env["PATH"] == "/usr/bin"
