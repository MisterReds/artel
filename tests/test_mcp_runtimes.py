import sys
from pathlib import Path

from artel.project import DEFAULT_CONFIG, deep_merge
from artel.runtimes import mcp_client
from artel.runtimes.claude_code import mcp_servers_json
from artel.runtimes.codex import build_args

FAKE = str(Path(__file__).parent / "fakes" / "mcp_server")
BRIDGE = {"artel": {"command": "py", "args": ["-m", "artel.runtimes.mcp_proxy", "u", "t"]}}


def cfg():
    servers = {
        "fake": {"type": "stdio", "command": sys.executable, "args": [FAKE], "allow": ["worker"], "env": {"K": "v"}},
        "remote": {"type": "http", "url": "http://h/mcp", "headers": {"X-A": "b"}, "allow": ["worker"]},
    }
    return deep_merge(DEFAULT_CONFIG, {"mcp_servers": servers})


def codex(role, tmp_path):
    return build_args({"binary": "codex", "sandbox": "workspace-write"}, workspace=tmp_path / "w", shared=tmp_path / "s",
                      role=role, bridge_url="http://b", bridge_token="tok", effort=None, thread_id=None,
                      last_message=tmp_path / "m", mcp_servers=mcp_client.codex_secrets(mcp_client.cli_config(cfg(), role))[0])


def test_claude_code_config_has_granted_servers_and_bridge():
    out = mcp_servers_json(mcp_client.cli_config(cfg(), "worker"), BRIDGE)
    assert out["artel"] == BRIDGE["artel"]
    assert out["fake"] == {"command": sys.executable, "args": [FAKE], "env": {"K": "v"}}
    assert out["remote"] == {"type": "http", "url": "http://h/mcp", "headers": {"X-A": "b"}}


def test_claude_code_config_ungranted_role_gets_only_bridge():
    assert mcp_servers_json(mcp_client.cli_config(cfg(), "reviewer"), BRIDGE) == BRIDGE


def test_external_server_cannot_replace_bridge():
    out = mcp_servers_json({"artel": {"command": "evil", "args": [], "env": {}}}, BRIDGE)
    assert out["artel"] == BRIDGE["artel"]


def test_codex_args_have_granted_servers_and_bridge(tmp_path):
    a = codex("worker", tmp_path)
    assert f'mcp_servers.fake.command="{sys.executable}"' in a
    assert any(x.startswith("mcp_servers.fake.args=") and FAKE in x for x in a)
    assert any(x.startswith("mcp_servers.fake.env_vars=") and "K" in x for x in a)
    assert 'mcp_servers.remote.url="http://h/mcp"' in a
    assert any(x.startswith("mcp_servers.remote.env_http_headers=") and "X-A" in x for x in a)
    assert not any('"b"' in x or "=v" in x for x in a)  # header and env values stay off the command line
    assert any(x.startswith("mcp_servers.artel.command=") for x in a)
    assert any(x.startswith("mcp_servers.artel.args=") and "http://b" in x for x in a)


def test_codex_args_ungranted_role_has_no_external(tmp_path):
    a = codex("reviewer", tmp_path)
    assert not [x for x in a if x.startswith("mcp_servers.fake") or x.startswith("mcp_servers.remote")]
    assert any(x.startswith("mcp_servers.artel.command=") for x in a)
