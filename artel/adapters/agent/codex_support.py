"""Codex helpers kept out of the codec: model catalogue, sandbox probe, environment and `codex exec` arguments.
Probes run the CLI (`codex debug models`, `codex sandbox`); nothing here touches the journal."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from .claude_code import PROXY_SCRIPT, child_env

DEFAULTS = {"binary": "codex", "model": None, "sandbox": "workspace-write", "max_resumes": 3, "timeout_s": 2400,
            "home": None, "reasoning_effort": None, "extra_args": [],
            "network": True,          # agents install packages; files stay confined by the sandbox and grants
            "sandbox_fallback": True,  # sandbox unusable on this machine: run without it instead of blocking every worker
            # Codex features that reach outside the worker's workspace (the owner's apps, screen and browser) are off
            "disable_features": ["apps", "computer_use", "browser_use", "browser_use_external"]}
# Signs that Codex's own sandbox could not even start a command (not that the command failed)
SANDBOX_BROKEN = re.compile(r"bwrap: |landlock|Failed RTM_NEWADDR|setting up uid map|unshare.*Operation not permitted|"
                            r"Sandbox(?:ing)? (?:is )?not supported|seccomp", re.I)
_sandbox_mode: dict = {}  # (binary, home) -> "sandbox" | "sandbox+network" | "none", decided once per process
_catalog_cache: dict = {}


def codex_env(cfg: dict, config: Optional[dict] = None) -> dict:
    from ...tools import env_with_tools

    env = env_with_tools(child_env(config, "codex"))
    if cfg.get("home"):
        env["CODEX_HOME"] = str(Path(cfg["home"]).expanduser())
    return env


def model_catalog(cc: Optional[dict] = None, ttl: float = 600.0) -> list[dict]:
    """Models this Codex account can use (`codex debug models`), cached; [] when the CLI is missing or fails."""
    from ...tools import resolve

    cc = cc or DEFAULTS
    key = (cc.get("binary"), cc.get("home"), os.environ.get("CODEX_HOME"))
    hit = _catalog_cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
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
        _catalog_cache[key] = (models, time.time())
    return models


def normalize_model(model: str, cc: Optional[dict] = None) -> str:
    """Accept a display name ("GPT-6-Luna") or any capitalisation for the model id Codex expects ("gpt-6-luna")."""
    m = (model or "").strip()
    for item in model_catalog(cc):
        if m.lower() in (str(item["id"]).lower(), str(item["name"]).lower()):
            return item["id"]
    return m


def _toml(value) -> str:
    return json.dumps(value)  # JSON strings and arrays of strings are valid TOML


def sandbox_mode(cc: dict, refresh: bool = False, config: Optional[dict] = None) -> str:
    """How Codex's own sandbox can run on this machine, probed once with `codex sandbox -- true`:
    "sandbox" (as configured), "sandbox+network" (works only with network on — e.g. bubblewrap may not set up a
    loopback in a new network namespace), or "none" (unusable; Artel's grants and command policy still apply)."""
    from ...tools import resolve

    key = (cc.get("binary"), cc.get("home"))
    if key in _sandbox_mode and not refresh:
        return _sandbox_mode[key]
    binary = resolve(cc.get("binary", "codex"))
    if not binary or cc.get("sandbox") != "workspace-write":
        _sandbox_mode[key] = "sandbox"
        return "sandbox"
    probe_cmd = (["cmd", "/c", "echo ok> probe && type probe"] if sys.platform == "win32"
                 else ["sh", "-c", "echo ok > probe && cat probe"])

    def works(network: bool) -> tuple[bool, str]:
        cfg = ["-c", 'sandbox_mode="workspace-write"'] + (["-c", "sandbox_workspace_write.network_access=true"] if network else [])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p = subprocess.run([binary, "sandbox", *cfg, "--", *probe_cmd], cwd=tmp, capture_output=True,
                                   text=True, timeout=60, env=codex_env(cc, config), encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return p.returncode == 0 and "ok" in p.stdout and not SANDBOX_BROKEN.search(p.stderr), (p.stdout + p.stderr)[-300:]

    ok, why = works(bool(cc.get("network", True)))
    if ok:
        mode = "sandbox+network" if cc.get("network", True) else "sandbox"
    elif not cc.get("network", True) and works(True)[0]:
        mode = "sandbox+network"
    else:
        mode = "none" if cc.get("sandbox_fallback", True) else "sandbox"
    _sandbox_mode[key] = mode
    _sandbox_mode[(key, "why")] = "" if ok else why
    return mode


def sandbox_note(cc: dict) -> str:
    return _sandbox_mode.get(((cc.get("binary"), cc.get("home")), "why"), "")


def build_args(cc: dict, *, workspace: Path, shared: Path, role: str, bridge_url: str, bridge_token: str,
               effort: Optional[str], thread_id: Optional[str], last_message: Path,
               grants: Optional[dict] = None, mode: str = "sandbox", mcp_servers: Optional[dict] = None) -> list:
    from ...tooling import READONLY_ROLES
    from ...tools import resolve

    pkg_root = str(Path(__file__).resolve().parents[3])
    no_write = role in READONLY_ROLES or (grants is not None and not grants["write"])
    sandbox = cc["sandbox"]
    if no_write and sandbox != "danger-full-access":
        # Judges must not change what they judge. Codex's read-only mode always cuts the network, which on some machines
        # breaks its sandbox (bwrap cannot set up a loopback). Instead they get workspace-write rooted at a private
        # scratch directory (the caller passes it as `workspace`): the deliverables are outside every writable root.
        sandbox = "workspace-write"
    if mode == "none":
        sandbox = "danger-full-access"  # read-only roles are then guarded by a snapshot of the deliverables
    args = [resolve(cc["binary"]) or cc["binary"], "exec", "--json", "--skip-git-repo-check", "--color", "never", "-C",
            str(workspace), "-o", str(last_message),
            "-c", f"mcp_servers.artel.command={_toml(sys.executable)}",
            "-c", f"mcp_servers.artel.args={_toml([PROXY_SCRIPT, bridge_url, bridge_token])}",
            "-c", f"mcp_servers.artel.env={{PYTHONPATH={_toml(pkg_root)}}}",
            "-c", "mcp_servers.artel.tool_timeout_sec=900",
            # Autonomous: Codex never stops to ask. Its own commands stay inside the sandbox below and Artel's command
            # policy; Artel's coordination tools run without a per-call approval, or the planner cannot submit its plan.
            "-c", 'approval_policy="never"', "-c", 'mcp_servers.artel.default_tools_approval_mode="approve"']
    for name, srv in (mcp_servers or {}).items():  # external MCP servers (mcp_client.cli_config), next to Artel's
        if name == "artel":
            continue
        pre = f"mcp_servers.{name}"
        if "url" in srv:  # header values travel in Codex's environment, by name (mcp_client.codex_secrets)
            args += ["-c", f"{pre}.url={_toml(srv['url'])}"]
            if srv.get("env_http_headers"):
                table = "{" + ", ".join(f"{_toml(k)}={_toml(v)}" for k, v in srv["env_http_headers"].items()) + "}"
                args += ["-c", f"{pre}.env_http_headers={table}"]
        else:
            args += ["-c", f"{pre}.command={_toml(srv['command'])}", "-c", f"{pre}.args={_toml(srv['args'])}"]
            if srv.get("env_vars"):
                args += ["-c", f"{pre}.env_vars={_toml(srv['env_vars'])}"]
        args += ["-c", f'{pre}.default_tools_approval_mode="approve"']
    for feature in cc.get("disable_features") or []:
        args += ["-c", f"features.{feature}=false"]
    if sandbox == "danger-full-access":
        args += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        args += ["-s", sandbox]
        if sandbox == "workspace-write" and (mode == "sandbox+network" or cc.get("network", True)):
            args += ["-c", "sandbox_workspace_write.network_access=true"]
        if sandbox == "workspace-write" and not no_write:
            # one --add-dir per write grant other than the workspace itself (never refs: those stay read-only)
            extra = [str(shared)] if grants is None else grants["write"]
            ws = {str(workspace), str(Path(workspace).resolve())}
            args += [x for d in dict.fromkeys(extra) if d == str(shared) or d not in ws for x in ("--add-dir", d)]
    if cc.get("model"):
        args += ["-m", normalize_model(cc["model"], cc)]
    eff = {"low": "low", "medium": "medium", "high": "high"}.get(effort or cc.get("reasoning_effort") or "")
    if eff:
        args += ["-c", f"model_reasoning_effort={_toml(eff)}"]
    return args + list(cc.get("extra_args") or []) + (["resume", thread_id, "-"] if thread_id else ["-"])
