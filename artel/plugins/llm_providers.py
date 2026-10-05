"""LLM provider plugins. The canonical in-harness message format is the Anthropic
Messages shape (``user``/``assistant`` turns with ``text``/``tool_use``/``tool_result``
blocks); other providers translate at the edge.

Providers return :class:`LLMResponse` with ``raw_content`` — the exact assistant
content to append back to history. For Claude this keeps thinking blocks
byte-identical, which preserved thinking requires.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    blocks: list            # normalized dicts: {"type":"text","text"} | {"type":"tool_use","id","name","input"}
    raw_content: Any
    stop_reason: str
    usage: dict = field(default_factory=dict)
    model: str = ""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, config: dict):
        import anthropic  # deferred so scripted/offline runs need no SDK

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=4)
        self.fallbacks = bool(config.get("fallbacks", True))

    def chat(self, *, system: str, messages: list, tools: list, model: str, effort: str, max_tokens: int,
             purpose: str = "") -> LLMResponse:
        a = self._anthropic
        params: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
        )
        if tools:
            params["tools"] = tools
        try:
            if self.fallbacks:
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"}, **params)
            else:
                resp = self.client.messages.create(**params)
        except a.BadRequestError as exc:
            raise LLMError(f"bad request: {exc}") from exc
        except a.AuthenticationError as exc:
            raise LLMError(f"authentication failed: {exc}") from exc
        except a.RateLimitError as exc:
            raise LLMError(f"rate limited after retries: {exc}") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc}") from exc
        except a.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}") from exc

        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        usage = {"input_tokens": resp.usage.input_tokens or 0, "output_tokens": resp.usage.output_tokens or 0}
        return LLMResponse(blocks=blocks, raw_content=resp.content, stop_reason=resp.stop_reason or "end_turn",
                           usage=usage, model=resp.model)


class OpenAICompatProvider:
    """Chat Completions-compatible endpoints: DeepSeek, OpenRouter, vLLM, Ollama, etc."""

    name = "openai_compat"

    def __init__(self, config: dict):
        import httpx

        oc = config.get("openai_compat", {})
        self.base_url = oc.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
        key = os.environ.get(oc.get("api_key_env", "OPENAI_API_KEY"), "")
        self.client = httpx.Client(timeout=600, headers={"Authorization": f"Bearer {key}"} if key else {})

    @staticmethod
    def _to_openai(system: str, messages: list) -> list:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
                continue
            if m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                         for b in content if b.get("type") == "tool_use"]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                texts = []
                for b in content:
                    if b.get("type") == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            c = "".join(x.get("text", "") for x in c)
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(c)})
                    elif b.get("type") == "text":
                        texts.append(b["text"])
                if texts:
                    out.append({"role": "user", "content": "\n".join(texts)})
        return out

    def chat(self, *, system: str, messages: list, tools: list, model: str, effort: str, max_tokens: int,
             purpose: str = "") -> LLMResponse:
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": self._to_openai(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in tools]
        last_exc: Optional[Exception] = None
        for attempt in range(5):
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                break
            except Exception as exc:  # noqa: BLE001 - retry transport and 5xx
                last_exc = exc
                if isinstance(exc, LLMError) and "HTTP 4" in str(exc) and "HTTP 429" not in str(exc):
                    raise
                time.sleep(2 ** attempt)
        else:
            raise LLMError(f"openai_compat failed: {last_exc}")
        choice = data["choices"][0]
        msg = choice["message"]
        blocks: list[dict] = []
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {"_invalid_json": c["function"].get("arguments")}
            blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
        finish = choice.get("finish_reason")
        stop = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(finish, "end_turn")
        u = data.get("usage") or {}
        return LLMResponse(blocks=blocks, raw_content=blocks, stop_reason=stop,
                           usage={"input_tokens": u.get("prompt_tokens", 0),
                                  "output_tokens": u.get("completion_tokens", 0)},
                           model=data.get("model", model))


PROVIDERS = {"anthropic": AnthropicProvider, "openai_compat": OpenAICompatProvider}


def register_provider(name: str, cls: type) -> None:
    PROVIDERS[name] = cls


def setup(kernel) -> None:
    from . import scripted  # registers "scripted"

    scripted.register()

    name = kernel.config.get("provider", "anthropic")
    spec = (kernel.config.get("providers") or {}).get(name)
    if spec:
        kernel.provide("llm_factory", lambda: OpenAICompatProvider({"openai_compat": spec}))
        return
    if name not in PROVIDERS:
        raise LLMError(f"unknown provider {name!r}; known: {sorted(PROVIDERS)}")
    kernel.provide("llm_factory", lambda: PROVIDERS[name](kernel.config))
