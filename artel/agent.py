"""The agent loop shared by every role.

A loop ends only through a terminal tool (``task_submit``, ``review_verdict`` …).
A turn that ends without one gets the continuation prompt (Prime Agent goal
continuation); ``NO_PROGRESS_CAP`` consecutive turns without any tool call end the
run as ``stalled`` so the manager can back off and retry with a fresh context.
Every request and response is written to the event log.

``LoopDetector`` watches for a model repeating itself: the same tool call in a row, or
the same 50-character chunk of text over and over. The first detection injects a
corrective note; a recurrence ends the run as ``stalled`` with error ``loop``.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .plugins.llm_providers import LLMError
from .prompts import CONTINUATION
from .tooling import ToolContext, ToolError

NO_PROGRESS_CAP = 3
TOOL_LOOP_THRESHOLD = 5      # identical consecutive tool calls that count as a loop
TEXT_CHUNK_SIZE = 50         # characters per hashed chunk of assistant text
TEXT_LOOP_THRESHOLD = 10     # repeats of one chunk that count as a loop
TEXT_HISTORY_CAP = 20_000    # characters of assistant text kept for chunk analysis
LOOP_NOTE = ("System note: you are repeating yourself ({what}). Stop and change your approach: use a different "
             "tool or different arguments, or finish with the terminal tool. If you repeat this again the run "
             "will be ended.")
_llm_lock = threading.Lock()


class LoopDetector:
    """Spot repeated tool calls and repeated text; each ``observe_*`` returns None, "warn" or "stop".

    Derived from Qwen Code's loopDetectionService.ts (https://github.com/QwenLM/qwen-code),
    Copyright Qwen Team, licensed under the Apache License, Version 2.0: the consecutive
    identical tool-call counter (threshold 5) and the 50-char chunk repetition check
    (threshold 10, chunks clustered within 1.5 chunk lengths on average).
    The first detection of a kind warns; a recurrence after the warning stops the run.
    """

    def __init__(self) -> None:
        self._tool_key: Optional[str] = None
        self._tool_count = 0
        self._tool_warned = False
        self._text_warned = False
        self._reset_text()

    def _reset_text(self) -> None:
        self._text = ""
        self._chunks: dict[str, list[int]] = {}

    def observe_tool(self, name: str, args: Any) -> Optional[str]:
        key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if key != self._tool_key:
            self._tool_key, self._tool_count, self._tool_warned = key, 1, False
            return None
        self._tool_count += 1
        if self._tool_warned:
            return "stop"
        if self._tool_count >= TOOL_LOOP_THRESHOLD:
            self._tool_warned = True
            return "warn"
        return None

    def observe_text(self, text: str) -> Optional[str]:
        start = len(self._text)
        self._text += text
        if len(self._text) > TEXT_HISTORY_CAP:
            self._text = self._text[-TEXT_HISTORY_CAP // 2:]
            self._chunks, start = {}, 0
        looped = False
        for i in range(max(0, start - TEXT_CHUNK_SIZE + 1), len(self._text) - TEXT_CHUNK_SIZE + 1):
            chunk = self._text[i:i + TEXT_CHUNK_SIZE]
            if len(set(chunk)) <= 2:     # separators such as "-----" are not a loop
                continue
            seen = self._chunks.setdefault(chunk, [])
            if seen and seen[-1] >= i:
                continue
            seen.append(i)
            if len(seen) >= TEXT_LOOP_THRESHOLD:
                recent = seen[-TEXT_LOOP_THRESHOLD:]
                if (recent[-1] - recent[0]) / (TEXT_LOOP_THRESHOLD - 1) <= TEXT_CHUNK_SIZE * 1.5:
                    looped = True
                    break
        if not looped:
            return None
        self._reset_text()
        if self._text_warned:
            return "stop"
        self._text_warned = True
        return "warn"


@dataclass
class Outcome:
    status: str                      # terminal | stalled | turn_limit | refused | error | budget | stopped
    terminal: Optional[tuple] = None
    turns: int = 0
    tool_calls: int = 0
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    digest: list = field(default_factory=list)
    error: str = ""
    cost_usd: float = 0.0

    @property
    def name(self) -> Optional[str]:
        return self.terminal[0] if self.terminal else None

    @property
    def payload(self) -> dict:
        return self.terminal[1] if self.terminal else {}

    def digest_text(self, limit: int = 30_000) -> str:
        text = "\n".join(self.digest)
        return text if len(text) <= limit else text[: limit // 3] + "\n…\n" + text[-2 * limit // 3:]


def get_llm(kernel, role: Optional[str] = None) -> tuple[Any, Optional[str]]:
    """The provider for a role and an optional model override.

    ``provider_by_role`` routes a role to another provider; names in ``providers`` are
    OpenAI-compatible endpoints (e.g. a local llama.cpp server with Gemma or Qwen).
    """
    cfg = kernel.config
    name = (cfg.get("provider_by_role") or {}).get(role or "", cfg.get("provider", "anthropic"))
    spec = (cfg.get("providers") or {}).get(name)
    with _llm_lock:
        key = f"llm:{name}"
        llm = kernel.get(key)
        if llm is None:
            if spec:
                from .plugins.llm_providers import OpenAICompatProvider

                llm = OpenAICompatProvider({"openai_compat": spec})
            elif name == cfg.get("provider", "anthropic"):
                llm = kernel.get("llm") or kernel.get("llm_factory")()
            else:
                from .plugins.llm_providers import PROVIDERS

                llm = PROVIDERS[name](cfg)
            kernel.provide(key, llm)
        return llm, (spec or {}).get("model")


def _short(obj: Any, n: int = 240) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def execute_tool(kernel, ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
    spec = kernel.tools.get(name)
    if spec is None or ctx.role not in spec.roles:
        return f"unknown tool {name!r} for role {ctx.role}", True
    missing = [k for k in spec.input_schema.get("required", []) if k not in args]
    if missing:
        return f"missing required arguments: {missing}", True

    def final(c: dict) -> tuple[str, bool]:
        try:
            return str(spec.fn(ctx, **c["args"])), False
        except ToolError as exc:
            return str(exc), True
        except TypeError as exc:
            return f"bad arguments: {exc}", True
        except Exception as exc:  # noqa: BLE001 - tool crashes go back to the model
            return f"tool crashed: {exc!r}", True

    return kernel.waterfall("tool/execute", {"name": name, "args": args, "ctx": ctx}, final)


def run_loop(kernel, project, *, role: str, system: str, first_user: str, ctx: ToolContext, max_turns: int,
             purpose: str, persona=None, stop_event: Optional[threading.Event] = None,
             tool_names: Optional[list] = None) -> Outcome:
    llm, model_override = get_llm(kernel, role)
    specs = kernel.tools_for(role, tool_names)
    tools = [s.schema() for s in specs]
    model = model_override or project.model_for(role, persona)
    effort = ctx.state.get("effort") or project.effort_for(role, persona)
    max_tokens = int(kernel.config.get("max_tokens", 16000))
    actor = persona.name if persona else role
    task_id = (ctx.task or {}).get("id")
    out = Outcome(status="turn_limit")
    messages: list[dict] = [{"role": "user", "content": first_user}]
    project.log.append("agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id, model=model,
                       effort=effort, system=system, user=first_user, tools=[t["name"] for t in tools])
    seen_comments = len(project.board.comments(task_id)) if task_id else 0
    idle_turns = 0
    detector = LoopDetector()

    def loop_event(loop_kind: str, stage: str, **details: Any) -> None:
        project.log.append("agent/loop", actor=actor, task_id=task_id, loop_kind=loop_kind,
                           stage=stage, details=details)

    for turn in range(1, max_turns + 1):
        if stop_event is not None and stop_event.is_set():
            out.status = "stopped"
            break
        budget = kernel.get("budget")
        reason = budget(project) if budget else None
        if reason:
            out.status, out.error = "budget", reason
            break
        req = {"system": system, "messages": messages, "tools": tools, "model": model, "effort": effort,
               "max_tokens": max_tokens, "purpose": purpose}
        try:
            resp = kernel.waterfall("llm/request", req, lambda c: llm.chat(**c))
        except LLMError as exc:
            out.status, out.error = "error", str(exc)
            project.log.append("llm/error", actor=actor, task_id=task_id, error=str(exc))
            break
        out.turns = turn
        for k in ("input_tokens", "output_tokens"):
            out.usage[k] += resp.usage.get(k, 0)
        from .analytics import cost_usd as _price

        cost = _price(resp.model, resp.usage.get("input_tokens", 0), resp.usage.get("output_tokens", 0)) or 0.0
        out.cost_usd += cost
        project.add_usage(resp.usage.get("input_tokens", 0), resp.usage.get("output_tokens", 0), cost_usd=cost)
        project.log.append("llm/response", actor=actor, role=role, task_id=task_id, turn=turn, model=resp.model,
                           stop_reason=resp.stop_reason, usage=resp.usage, blocks=resp.blocks)
        if ctx.run_id:
            project.board.heartbeat(ctx.run_id, turns=turn, tool_calls=out.tool_calls,
                                    tokens_in=out.usage["input_tokens"], tokens_out=out.usage["output_tokens"])
        messages.append({"role": "assistant", "content": resp.raw_content})
        for b in resp.blocks:
            if b["type"] == "text" and b["text"].strip():
                out.digest.append(f"ASSISTANT: {_short(b['text'], 600)}")

        if resp.stop_reason == "refusal":
            out.status, out.error = "refused", "model declined the request"
            break

        notes: list[str] = []
        for b in resp.blocks:
            if b["type"] == "text" and (verdict := detector.observe_text(b["text"])):
                loop_event("text", verdict, chunk_size=TEXT_CHUNK_SIZE, repeats=TEXT_LOOP_THRESHOLD)
                if verdict == "stop":
                    out.status, out.error = "stalled", "loop: repeated text"
                    break
                notes.append(LOOP_NOTE.format(what="the same text keeps recurring"))
        if out.error:
            break

        uses = [b for b in resp.blocks if b["type"] == "tool_use"]
        if uses:
            idle_turns = 0
            results = []
            for b in uses:
                if verdict := detector.observe_tool(b["name"], b["input"]):
                    loop_event("tool", verdict, tool=b["name"], input=_short(b["input"]), count=TOOL_LOOP_THRESHOLD)
                    if verdict == "stop":
                        out.status, out.error = "stalled", f"loop: repeated tool call {b['name']}"
                        break
                    notes.append(LOOP_NOTE.format(what=f"identical {b['name']} calls"))
                out.tool_calls += 1
                project.log.append("tool/call", actor=actor, task_id=task_id, tool=b["name"], input=b["input"],
                                   call_id=b["id"])
                text, is_error = execute_tool(kernel, ctx, b["name"], b["input"])
                project.log.append("tool/result", actor=actor, task_id=task_id, tool=b["name"], call_id=b["id"],
                                   is_error=is_error, output=text[:4000])
                out.digest.append(f"TOOL {b['name']}({_short(b['input'])}) -> {'ERROR ' if is_error else ''}"
                                  f"{_short(text, 300)}")
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": text, "is_error": is_error})
            if out.error:
                break
            content: list[dict] = results
            if task_id:
                comments = project.board.comments(task_id)
                fresh = [c for c in comments[seen_comments:] if c["author"] == "human"]
                seen_comments = len(comments)
                if fresh:
                    content = results + [{"type": "text", "text": "Message from the human operator:\n" +
                                          "\n".join(c["body"] for c in fresh)}]
            content = content + [{"type": "text", "text": n} for n in notes]
            messages.append({"role": "user", "content": content})
            if ctx.terminal:
                out.status, out.terminal = "terminal", ctx.terminal
                break
            continue

        if resp.stop_reason == "max_tokens":
            messages.append({"role": "user", "content": "\n\n".join(
                ["Your output was cut off at the token limit. Continue, and put large content into files with "
                 "tools."] + notes)})
            continue
        idle_turns += 1
        if idle_turns >= NO_PROGRESS_CAP:
            out.status, out.error = "stalled", f"{idle_turns} consecutive turns without tool use"
            break
        project.log.append("agent/continuation", actor=actor, task_id=task_id, idle_turns=idle_turns)
        messages.append({"role": "user", "content": "\n\n".join([CONTINUATION] + notes)})

    project.log.append("agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
                       terminal=out.name, turns=out.turns, tool_calls=out.tool_calls, usage=out.usage, error=out.error)
    return out


def budget_guard(config: dict):
    """Return a callable that names the exhausted budget, or None while within limits."""
    b = config.get("budgets", {})

    def check(project) -> Optional[str]:
        meta = project.meta
        u = meta["usage"]
        if b.get("max_total_tokens") and u["input_tokens"] + u["output_tokens"] >= b["max_total_tokens"]:
            return f"token budget {b['max_total_tokens']} exhausted"
        if b.get("max_hours") and time.time() - meta["created_at"] >= b["max_hours"] * 3600:
            return f"time budget {b['max_hours']}h exhausted"
        return None

    return check
