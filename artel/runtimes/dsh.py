"""DeepSeek Harness (``dsh``, github.com/deepseek-ai/deepseek-harness, MIT) as an Artel runtime.

Integration path: the official Python SDK (``deepseek_harness``, pip package ``deepseek-harness-sdk``). The SDK starts
the version-matched bundled ``dsh --profile <profile>`` runtime as a subprocess and talks newline-delimited JSON-RPC
to it; every durable session event arrives as a ``session.event`` notification. The runtime boots a full Cordis
composition: the profile's bundles, the plugins installed into that profile with ``dsh plugin add`` and the ordered
``--patch`` layers. dsh plugins therefore load unchanged inside the worker — Artel only adds one patch layer of its
own that mounts ``@deepseek-ai/dsh-mcp-client`` with Artel's coordination tools (the same localhost bridge and MCP
proxy as the Claude Code and Codex runtimes), so tool execution, validation and logging are identical.

Every assistant message, tool call and tool result is mirrored into the project event log. If dsh ends without a
terminal tool, the same durable session gets the continuation prompt, up to ``max_resumes`` times. Read-only roles
run with dsh's ``read-only`` file sandbox. The DeepSeek API key is read by dsh itself from ``DEEPSEEK_API_KEY`` in
the environment (or dsh's own credential store in its home); Artel never reads it out or stores it.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .. import policy
from ..agent import LoopDetector, Outcome, _short
from ..prompts import CONTINUATION
from ..tooling import ToolContext
from .claude_code import PROXY_SCRIPT, READONLY_ROLES, ToolBridge, child_env

KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULTS = {"sdk": "deepseek_harness", "binary": None, "home": None, "profile": "sdk",
            "provider": "deepseek-official", "model": None, "reasoning_effort": None, "max_tokens": None,
            "plugins": [], "patches": [], "sandbox": "workspace-write", "share_session_log": False,
            "max_resumes": 3, "timeout_s": 2400, "init_timeout_s": 180}
MCP_PREFIX = "mcp__artel__"
SHELL_TOOLS = {"bash", "pwsh"}
EDIT_TOOLS = {"write", "edit", "str_replace_editor"}
SETUP_MISSING = ("DeepSeek Harness не установлен: «Провайдеры» → «Установить» или `artel providers install dsh` "
                 "(pip-пакет deepseek-harness-sdk в окружение Artel)")
# dsh failure codes (LlmFailure.code) → wording that artel.failures classifies (limit / setup)
CODE_TEXT = {"RATE_LIMIT": "rate limit", "QUOTA": "usage limit (insufficient balance)",
             "ACCOUNT_QUOTA": "usage limit", "AUTH": "authentication failed",
             "INVALID_CREDENTIAL": "invalid api key", "MISSING_CREDENTIAL": f"missing api key ({KEY_ENV} not set)"}
_install_lock = threading.Lock()


def dsh_settings(config: dict) -> dict:
    return {**DEFAULTS, **(config.get("dsh") or {})}


def dsh_home(cfg: dict) -> Path:
    if cfg.get("home"):
        return Path(cfg["home"]).expanduser()
    from ..project import artel_home

    return artel_home() / "dsh"


def sdk_available(cfg: dict) -> bool:
    try:
        return importlib.util.find_spec(cfg["sdk"]) is not None
    except (ImportError, ValueError):
        return False


def key_available(cfg: dict) -> bool:
    """Whether dsh will find a DeepSeek credential: the environment variable or dsh's own store in its home."""
    if cfg.get("provider", "deepseek-official") != "deepseek-official":
        return True  # another adapter (e.g. llm-pi-ai) owns its credentials
    if os.environ.get(KEY_ENV):
        return True
    home = dsh_home(cfg)
    return (home / ".credentials.yaml").exists() or (home / ".env").exists()


def launcher(cfg: dict) -> Optional[list]:
    """argv that runs the ``dsh`` CLI (plugin management): the configured binary or the SDK's bundled runtime."""
    from ..tools import resolve

    if cfg.get("binary"):
        found = resolve(cfg["binary"])
        return [found] if found else None
    try:
        rt = importlib.import_module("deepseek_harness_runtime")
        return list(rt.resolve_bundled_launch_args())
    except Exception:  # noqa: BLE001 - not installed / platform without a bundled runtime
        found = resolve("dsh")
        return [found] if found else None


def _split_plugins(cfg: dict) -> tuple[list, list]:
    """``plugins`` entries: package specs for ``dsh plugin add`` and Cordis patch files (.yml/.yaml/.json)."""
    packages, patches = [], []
    for p in cfg.get("plugins") or []:
        p = str(p)
        (patches if p.lower().endswith((".yml", ".yaml", ".json")) and not p.startswith(("github:", "npm:"))
         else packages).append(p)
    return packages, patches + [str(x) for x in cfg.get("patches") or []]


def ensure_plugins(cfg: dict, env: dict) -> Optional[str]:
    """Install the configured dsh plugin packages into the profile once (``dsh plugin --profile P add …``).

    Returns an error text, or None. Installed specs are remembered in the profile directory, so a run only pays for
    new entries. Package management is dsh's own (pnpm); Artel only forwards the list.
    """
    packages, _ = _split_plugins(cfg)
    if not packages:
        return None
    home = dsh_home(cfg)
    marker = home / "profiles" / cfg["profile"] / ".artel-plugins.json"
    with _install_lock:
        done = set(json.loads(marker.read_text())) if marker.exists() else set()
        missing = [p for p in packages if p not in done]
        if not missing:
            return None
        argv = launcher(cfg)
        if not argv:
            return SETUP_MISSING
        home.mkdir(parents=True, exist_ok=True)
        try:
            p = subprocess.run([*argv, "plugin", "--profile", cfg["profile"], "add", *missing], capture_output=True,
                               text=True, env={**env, "DSH_HOME": str(home)}, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"dsh-плагины не установлены (not installed): dsh plugin add: {e}"
        if p.returncode != 0:
            return (f"dsh-плагины не установлены (not installed): dsh plugin add {' '.join(missing)}: "
                    f"{(p.stdout + p.stderr).strip()[-800:]}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(sorted(done | set(missing))))
    return None


def artel_patch(cfg: dict, *, bridge_url: str, bridge_token: str) -> list:
    """The Cordis patch layer Artel adds on top of the profile: Artel's tools over MCP (and no session-log upload)."""
    pkg_root = str(Path(__file__).resolve().parents[2])
    rows = [{"insert": [{"id": "artel-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": {
        "serverName": "artel", "transport": "stdio", "command": sys.executable,
        "args": [PROXY_SCRIPT, bridge_url, bridge_token],
        "env": {"PYTHONPATH": pkg_root}, "toolCallTimeoutMs": 900_000, "failOnStartupError": True}}]}]
    if not cfg.get("share_session_log"):
        rows.append({"id": "session-log-deepseek", "config": {"enabled": False}})
    return rows


def run_env(cfg: dict, role: str) -> dict:
    env = child_env()
    env["DSH_PERMISSION_MODE"] = "read-only" if role in READONLY_ROLES else (cfg.get("sandbox") or "workspace-write")
    return env


def _failure(err) -> str:
    if not isinstance(err, dict):
        return str(err or "")
    code, msg = str(err.get("code") or ""), str(err.get("message") or "")
    label = CODE_TEXT.get(code)
    return f"{label} [{code}]: {msg}" if label else (f"[{code}] {msg}" if code else msg)


def _text(content) -> str:
    return "".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"raw": str(raw)}


class _Mirror:
    """Turns dsh session events into Artel event-log entries for one run."""

    def __init__(self, project, ctx, out, actor, task_id, model, session_id, max_turns):
        self.project, self.ctx, self.out, self.actor, self.task_id = project, ctx, out, actor, task_id
        self.model, self.session_id, self.max_turns = model, session_id, max_turns
        self.detector = LoopDetector()
        self.pending: dict[str, tuple] = {}
        self.failure = ""
        self.finish: Optional[str] = None
        self.blocked = ""
        self.abort = False  # the watchdog closes the runtime: terminal accepted, policy stop, loop, turn limit

    def __call__(self, note) -> None:
        method = getattr(note, "method", None)
        payload = getattr(note, "payload", None) or {}
        if method != "session.event" or payload.get("sessionId") != self.session_id:
            return
        ev = payload.get("event") or {}
        typ, data = ev.get("type"), ev.get("data") or {}
        log, out, actor, task_id = self.project.log.append, self.out, self.actor, self.task_id
        if typ == "request/header":
            self.model = f"dsh:{((data.get('header') or {}).get('config') or {}).get('model') or self.model[4:]}"
        elif typ == "assistant/message":
            msg = data.get("message") or {}
            blocks = []
            for b in msg.get("content") or []:
                if b.get("type") == "text" and b.get("text"):
                    blocks.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "tool-call":
                    blocks.append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                                   "input": _args(b.get("arguments"))})
            u = data.get("usage") or {}
            tin, tout = int(u.get("inputTokens") or 0), int(u.get("outputTokens") or 0)
            out.usage["input_tokens"] += tin
            out.usage["output_tokens"] += tout
            self.project.add_usage(tin, tout, cost_usd=0.0)
            if not blocks:
                return
            out.turns += 1
            log("llm/response", actor=actor, task_id=task_id, turn=out.turns, model=self.model, blocks=blocks,
                usage=u, runtime="dsh")
            text = _text(blocks)
            if text.strip():
                out.digest.append(f"ASSISTANT: {_short(text, 600)}")
            if self.ctx.run_id:
                self.project.board.heartbeat(self.ctx.run_id, turns=out.turns, tool_calls=out.tool_calls)
            if out.turns >= self.max_turns:
                out.status, out.error = "turn_limit", f"{self.max_turns} turns"
                self.abort = True
        elif typ == "tool/call":
            name, args, cid = str(data.get("name") or ""), _args(data.get("arguments")), str(data.get("callId") or "")
            if name.startswith(MCP_PREFIX):  # the bridge logs the call and its result itself
                out.digest.append(f"TOOL {name[len(MCP_PREFIX):]}({_short(args)})")
                return
            out.tool_calls += 1
            self.pending[cid] = (name, args)
            log("tool/call", actor=actor, task_id=task_id, tool=name, input=args, call_id=cid, runtime="dsh")
            if name in EDIT_TOOLS:
                path = args.get("path") or args.get("file_path")
                if path:
                    self.ctx.edited_files.append(str(path))
            if name in SHELL_TOOLS:
                self._policy(str(args.get("command") or ""))
            verdict = self.detector.observe_tool(name, args)
            if verdict:
                log("agent/loop", actor=actor, task_id=task_id, loop_kind="tool", stage=verdict,
                    details={"tool": name, "input": _short(args)})
                if verdict == "stop":
                    out.status, out.error = "stalled", f"loop: repeated tool call {name}"
                    self.abort = True
        elif typ == "tool/result":
            msg = data.get("message") or {}
            cid = str(msg.get("toolCallId") or "")
            if cid not in self.pending:
                return
            name, args = self.pending.pop(cid)
            text = _text(msg.get("content"))
            log("tool/result", actor=actor, task_id=task_id, tool=name, call_id=cid, is_error=bool(msg.get("isError")),
                output=text[:4000], runtime="dsh")
            out.digest.append(f"TOOL {name}({_short(args)}) -> {_short(text, 300)}")
            if name in SHELL_TOOLS:
                self.ctx.outputs.append(f"$ {args.get('command', '')}\n{text[:3000]}")
        elif typ == "turn/end":
            reason = data.get("reason") or {}
            self.finish = reason.get("kind")
            if self.finish == "error":
                self.failure = _failure(reason.get("error"))
        if self.ctx.terminal:
            self.abort = True

    def _policy(self, command: str) -> None:
        # dsh has no pre-exec hook for SDK clients: stop the runtime as soon as a disallowed command is requested
        verdict = policy.evaluate(command, self.project.root)
        if verdict.decision == "allow":
            return
        self.project.log.append("policy/blocked", actor=self.actor, task_id=self.task_id, command=command,
                                decision=verdict.decision, rule=verdict.rule.id, justification=verdict.justification,
                                runtime="dsh")
        if verdict.decision == "prompt":
            if not self.ctx.terminal:
                self.ctx.terminal = policy.prompt_terminal(verdict, command)
        else:
            self.blocked = (f"The command `{command}` was blocked by policy ({verdict.rule.id}): "
                            f"{verdict.justification or 'forbidden'}. Choose another way.")
        self.abort = True


def _setup_error(project, actor, task_id, error: str) -> Outcome:
    out = Outcome(status="error", error=error)
    out.cost_usd = 0.0
    project.log.append("llm/error", actor=actor, task_id=task_id, error=error, runtime="dsh")
    return out


def run_dsh(kernel, project, *, role: str, system: str, first_user: str, ctx: ToolContext, max_turns: int,
            purpose: str, persona=None, stop_event: Optional[threading.Event] = None,
            tool_names: Optional[list] = None) -> Outcome:
    cfg = dsh_settings(kernel.config)
    actor = persona.name if persona else role
    task_id = (ctx.task or {}).get("id")
    if not sdk_available(cfg) or (cfg.get("binary") and not launcher(cfg)):
        return _setup_error(project, actor, task_id, SETUP_MISSING)
    if not key_available(cfg):
        return _setup_error(project, actor, task_id,
                            f"DeepSeek API key not set: задайте переменную окружения {KEY_ENV} (значение Artel "
                            f"не хранит) и перезапустите Artel")
    sdk = importlib.import_module(cfg["sdk"])
    env = run_env(cfg, role)
    err = ensure_plugins(cfg, env)
    if err:
        return _setup_error(project, actor, task_id, err)

    out = Outcome(status="stalled")
    out.cost_usd = 0.0  # tokens are counted; DeepSeek bills them on its side
    bridge = ToolBridge(kernel, project, ctx, role, tool_names, actor)
    terminal_names = [s.name for s in bridge.specs if s.terminal]
    note = (f"\n\n## Runtime\nYou run inside DeepSeek Harness. Use its own tools for files and shell. Artel "
            f"coordination tools are MCP tools named {MCP_PREFIX}<name>: {', '.join(s.name for s in bridge.specs)}. "
            f"The ONLY way to finish is calling {' or '.join(MCP_PREFIX + n for n in terminal_names)}. Your working "
            f"directory is your workspace; the team's shared deliverables directory is {project.shared}."
            + (" Your file sandbox is read-only." if role in READONLY_ROLES else "")
            + (f" Read-only reference sources: {', '.join(map(str, project.refs))}." if project.refs else ""))
    model = f"dsh:{cfg.get('model') or 'default'}"
    session_id = f"artel-{task_id or role}-{uuid.uuid4().hex[:8]}"
    _, user_patches = _split_plugins(cfg)
    patch_path = project.root / ".cc" / f"dsh-{uuid.uuid4().hex[:8]}.patch.yml"
    patch_path.parent.mkdir(exist_ok=True)
    patch_path.write_text(json.dumps(artel_patch(cfg, bridge_url=bridge.url, bridge_token=bridge.token_arg), indent=1))
    project.log.append("agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id, model=model,
                       effort=cfg.get("reasoning_effort") or "default", system=system + note,
                       user=first_user, tools=[MCP_PREFIX + s.name for s in bridge.specs],
                       plugins=list(cfg.get("plugins") or []), runtime="dsh")
    mirror = _Mirror(project, ctx, out, actor, task_id, model, session_id, max_turns)
    harness = None
    budget = kernel.get("budget")
    prompt = f"<role_instructions>\n{system}{note}\n</role_instructions>\n\n{first_user}"
    kw = {"provider": cfg["provider"], "cwd": str(ctx.workspace), "dsh_home": str(dsh_home(cfg)),
          "profile": cfg["profile"], "patches": tuple([*user_patches, str(patch_path)]), "env": env,
          "initialize_timeout_seconds": float(cfg["init_timeout_s"])}
    for k, sk in (("model", "model"), ("max_tokens", "max_tokens"), ("binary", "dsh_bin")):
        if cfg.get(k):
            kw[sk] = launcher(cfg)[0] if k == "binary" else cfg[k]
    if cfg.get("reasoning_effort"):  # adapter-owned identifiers: only an explicit setting, never Artel's effort hint
        kw["reasoning_effort"] = cfg["reasoning_effort"]
    try:
        for attempt in range(1 + int(cfg["max_resumes"])):
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            reason = budget(project) if budget else None
            if reason:
                out.status, out.error = "budget", reason
                break
            mirror.failure, mirror.finish, mirror.blocked, mirror.abort = "", None, "", False
            exc = None
            deadline = time.time() + float(cfg["timeout_s"])
            try:
                if harness is None:  # a fresh runtime on the same home + session id continues the durable session
                    harness = sdk.DeepSeekHarness(**kw)
                    harness.start()
                done = threading.Event()

                def watchdog(h=harness):
                    while not done.wait(0.2):
                        if mirror.abort or ctx.terminal or time.time() > deadline or \
                                (stop_event is not None and stop_event.is_set()):
                            h.close()
                            return

                threading.Thread(target=watchdog, daemon=True, name="dsh-watchdog").start()
                try:
                    harness.run(prompt, session_id=session_id, on_notification=mirror)
                finally:
                    done.set()
            except Exception as e:  # noqa: BLE001 - runtime closed by the watchdog, boot failure, protocol error
                exc = e
            if mirror.abort or exc is not None:
                if harness is not None:
                    harness.close()
                harness = None
            if ctx.terminal:
                out.status, out.terminal = "terminal", ctx.terminal
                break
            if stop_event is not None and stop_event.is_set():
                out.status = "stopped"
                break
            if out.status == "turn_limit" or out.error.startswith("loop"):
                break
            if mirror.failure or (exc is not None and not mirror.blocked):
                detail = mirror.failure or (f"timeout after {cfg['timeout_s']}s" if time.time() > deadline
                                            else str(exc).strip()[-800:] or repr(exc))
                out.status, out.error = "error", f"dsh failed: {detail}"
                project.log.append("llm/error", actor=actor, task_id=task_id, error=out.error, runtime="dsh")
                break
            project.log.append("agent/continuation", actor=actor, task_id=task_id, idle_turns=attempt + 1,
                               runtime="dsh")
            prompt = (mirror.blocked + "\n\n" if mirror.blocked else "") + CONTINUATION
        else:
            out.status, out.error = "stalled", f"no terminal tool after {cfg['max_resumes']} resumes"
    finally:
        if harness is not None:
            harness.close()
        bridge.close()
        patch_path.unlink(missing_ok=True)
    out.tool_calls += bridge.calls
    project.log.append("agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
                       terminal=out.name, turns=out.turns, tool_calls=out.tool_calls, usage=out.usage, cost_usd=0.0,
                       error=out.error, runtime="dsh", model=mirror.model)
    return out
