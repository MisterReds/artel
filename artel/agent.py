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

from .contracts import REQUIRED_FIELDS, Event
from .contracts._base import ContractError
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

# Context compaction: adapted from OpenCode's session/compaction.ts (https://github.com/anomalyco/opencode),
# MIT License, Copyright (c) opencode contributors. Stage 1 prunes old tool outputs (cf. prune/PRUNE_PROTECT),
# stage 2 summarizes the head of the conversation and keeps the most recent turns.
COMPACT_PRUNE_TOKENS = 60_000       # estimated tokens above which old tool results are cleared
COMPACT_SUMMARIZE_TOKENS = 120_000  # estimated tokens above which the conversation head is summarized
COMPACT_KEEP_TURNS = 2              # most recent assistant turns kept verbatim
PRUNED_PLACEHOLDER = "[old tool result cleared to save context]"
_SUMMARY_PROMPT = ("Summarize the conversation so far for an agent that will continue the work: the goal, what was "
                   "done, key findings, files touched, and what remains. Be concise and factual.")


def _blocks(content: Any) -> list:
    return content if isinstance(content, list) else []


def estimate_tokens(messages: list) -> int:
    """Rough token estimate (about 4 characters per token) of a message list."""
    return len(json.dumps(messages, default=str)) // 4


def _recent_start(messages: list, keep_turns: int) -> int:
    """Index of the assistant message that starts the last ``keep_turns`` turns, or 0 if there are fewer."""
    idx = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    return idx[-keep_turns] if len(idx) > keep_turns else 0


def prune_tool_results(messages: list, keep_turns: int = COMPACT_KEEP_TURNS) -> tuple[list, int]:
    """Replace tool_result contents older than the last ``keep_turns`` turns with a placeholder.

    Returns (new messages, number of results cleared). tool_use_id, is_error and message structure are kept.
    """
    cut = _recent_start(messages, keep_turns)
    cleared = 0
    out = []
    for i, m in enumerate(messages):
        if i >= cut or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(m["content"])):
            out.append(m)
            continue
        blocks = []
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("content") != PRUNED_PLACEHOLDER:
                b = {**b, "content": PRUNED_PLACEHOLDER}
                cleared += 1
            blocks.append(b)
        out.append({**m, "content": blocks})
    return out, cleared


def _render(messages: list, limit: int = 400) -> str:
    lines = []
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            lines.append(f"{m['role'].upper()}: {_short(c, limit)}")
            continue
        for b in _blocks(c):
            if not isinstance(b, dict):
                b = {"type": getattr(b, "type", ""), "text": getattr(b, "text", ""), "name": getattr(b, "name", ""),
                     "input": getattr(b, "input", "")}
            if b.get("type") == "text" and b.get("text"):
                lines.append(f"{m['role'].upper()}: {_short(b['text'], limit)}")
            elif b.get("type") == "tool_use":
                lines.append(f"TOOL_CALL {b.get('name')}({_short(b.get('input'), 200)})")
            elif b.get("type") == "tool_result":
                lines.append(f"TOOL_RESULT: {_short(b.get('content'), 200)}")
    return "\n".join(lines)


def compact_messages(messages: list, summarize=None, keep_turns: int = COMPACT_KEEP_TURNS) -> Optional[list]:
    """Summarize everything before the last ``keep_turns`` turns into the first user message.

    ``summarize(transcript) -> str`` may call an LLM; on failure or empty output a deterministic transcript
    digest is used. The kept tail starts at an assistant message, so tool_use/tool_result pairs stay valid.
    Returns None when there is nothing to compact.
    """
    cut = _recent_start(messages, keep_turns)
    if cut < 2:
        return None
    transcript = _render(messages[1:cut])
    summary = ""
    if summarize is not None:
        try:
            summary = (summarize(transcript) or "").strip()
        except Exception:
            summary = ""
    if not summary:
        summary = _short(transcript, 6000)
    first = messages[0]["content"]
    goal = [{"type": "text", "text": first}] if isinstance(first, str) else list(first)
    goal.append({"type": "text", "text": "Summary of earlier work (older context was compacted):\n" + summary})
    return [{"role": "user", "content": goal}] + messages[cut:]


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


def journal_agent_event(log, kind: str, **payload: Any) -> Optional[dict]:
    """Append an agent event to the journal only if it satisfies the Event contract (artel/contracts/events.py).
    An invalid event is never written: a ``contract/violation`` (event_kind, missing fields, problem, actor) takes its
    place and the run goes on. Returns the written event, or None when it was rejected."""
    try:
        Event.from_dict({"seq": 0, "ts": 0.0, "kind": kind, **payload})
    except ContractError as exc:
        missing = [f for f in REQUIRED_FIELDS.get(kind, ()) if f not in payload]
        log.append("contract/violation", actor=payload.get("actor"), event_kind=kind, missing=missing,
                   problem=str(exc)[:300])
        return None
    return log.append(kind, **payload)


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
    journal_agent_event(project.log, "agent/start", actor=actor, role=role, purpose=purpose, task_id=task_id, model=model,
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
        threshold = int(kernel.config.get("compact_prune_tokens", COMPACT_PRUNE_TOKENS))
        if estimate_tokens(messages) > threshold:
            messages, cleared = prune_tool_results(messages)
            if cleared:
                project.log.append("agent/compaction", actor=actor, task_id=task_id, stage="prune", cleared=cleared,
                                   tokens=estimate_tokens(messages))
            if estimate_tokens(messages) > int(kernel.config.get("compact_summarize_tokens",
                                                                 COMPACT_SUMMARIZE_TOKENS)):
                def _summarize(transcript: str) -> str:
                    r = llm.chat(system=_SUMMARY_PROMPT, messages=[{"role": "user", "content": transcript}],
                                 tools=[], model=model, effort=effort, max_tokens=2000, purpose="compaction")
                    return "\n".join(b["text"] for b in r.blocks if b["type"] == "text")

                before = len(messages)
                compacted = compact_messages(messages, _summarize)
                if compacted:
                    messages = compacted
                    project.log.append("agent/compaction", actor=actor, task_id=task_id, stage="summarize",
                                       messages_before=before, messages_after=len(messages),
                                       tokens=estimate_tokens(messages))
        req = {"system": system, "messages": messages, "tools": tools, "model": model, "effort": effort,
               "max_tokens": max_tokens, "purpose": purpose}
        try:
            resp = kernel.waterfall("llm/request", req, lambda c: llm.chat(**c))
        except LLMError as exc:
            out.status, out.error = "error", str(exc)
            journal_agent_event(project.log, "llm/error", actor=actor, task_id=task_id, error=str(exc))
            break
        out.turns = turn
        for k in ("input_tokens", "output_tokens"):
            out.usage[k] += resp.usage.get(k, 0)
        from .analytics import cost_usd as _price

        cost = _price(resp.model, resp.usage.get("input_tokens", 0), resp.usage.get("output_tokens", 0)) or 0.0
        out.cost_usd += cost
        project.add_usage(resp.usage.get("input_tokens", 0), resp.usage.get("output_tokens", 0), cost_usd=cost)
        journal_agent_event(project.log, "llm/response", actor=actor, role=role, task_id=task_id, turn=turn, model=resp.model,
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
                journal_agent_event(project.log, "tool/call", actor=actor, task_id=task_id, tool=b["name"], input=b["input"],
                                   call_id=b["id"])
                text, is_error = execute_tool(kernel, ctx, b["name"], b["input"])
                journal_agent_event(project.log, "tool/result", actor=actor, task_id=task_id, tool=b["name"], call_id=b["id"],
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

    journal_agent_event(project.log, "agent/end", actor=actor, role=role, purpose=purpose, task_id=task_id, status=out.status,
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
