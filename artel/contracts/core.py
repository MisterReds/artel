"""Port-level value objects: Usage, NormRequest, NormResponse, ToolResult, ModelCard, ErrorClass."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from . import _base as b

STOP_REASONS = ("end", "tool_use", "max_tokens", "stop_sequence", "error")
MESSAGE_ROLES = ("system", "user", "assistant", "tool")
ERROR_KINDS = ("rate_limit", "overloaded", "auth", "context_length", "invalid_request", "network", "server",
               "unknown")


@dataclass(frozen=True)
class Usage:
    in_: int
    out: int
    cached: int

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "Usage":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        return cls(b.integer(d, "in", path), b.integer(d, "out", path), b.integer(d, "cached", path))

    def to_dict(self) -> dict:
        return {"in": self.in_, "out": self.out, "cached": self.cached}


@dataclass(frozen=True)
class NormRequest:
    messages: tuple
    tools: tuple
    output_schema: Optional[dict]
    max_tokens: int
    cache_hint: Optional[str]

    def __post_init__(self) -> None:
        for name in ("messages", "tools", "output_schema"):
            object.__setattr__(self, name, b.freeze(getattr(self, name)))

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "NormRequest":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        messages = b.mapping_list(d, "messages", path, {"role": (str,), "content": (str, list, tuple)})
        for i, m in enumerate(messages):
            if m.get("role") not in MESSAGE_ROLES:
                raise b.ContractError(f"{path}messages[{i}].role", f"must be one of {sorted(MESSAGE_ROLES)}")
        return cls(messages, b.mapping_list(d, "tools", path, {"name": (str,)}),
                   b.mapping(d, "output_schema", path, nullable=True), b.integer(d, "max_tokens", path, 1),
                   b.string(d, "cache_hint", path, nullable=True))

    def to_dict(self) -> dict:
        return {"messages": b.thaw(self.messages), "tools": b.thaw(self.tools),
                "output_schema": b.thaw(self.output_schema), "max_tokens": self.max_tokens, "cache_hint": self.cache_hint}


@dataclass(frozen=True)
class NormResponse:
    content: str
    tool_calls: tuple
    usage: Usage
    latency_ms: int
    model: str
    stop: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", b.freeze(self.tool_calls))

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "NormResponse":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        return cls(b.string(d, "content", path), b.mapping_list(d, "tool_calls", path, {"id": (str,), "name": (str,), "args": (Mapping,)}),
                   b.nested(d, "usage", Usage, path), b.integer(d, "latency_ms", path),
                   b.string(d, "model", path), b.enum(d, "stop", STOP_REASONS, path))

    def to_dict(self) -> dict:
        return {"content": self.content, "tool_calls": b.thaw(self.tool_calls),
                "usage": self.usage.to_dict(), "latency_ms": self.latency_ms, "model": self.model,
                "stop": self.stop}


@dataclass(frozen=True)
class ToolResult:
    output: str
    is_error: bool
    exit_code: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "ToolResult":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        code = d.get("exit_code")
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            raise b.ContractError(f"{path}exit_code", "expected an integer or null")
        return cls(b.string(d, "output", path), b.boolean(d, "is_error", path), code)

    def to_dict(self) -> dict:
        return {"output": self.output, "is_error": self.is_error, "exit_code": self.exit_code}


@dataclass(frozen=True)
class ModelCard:
    id: str
    provider: str
    context_tokens: int
    capabilities: tuple
    usd_per_mtok_in: float
    usd_per_mtok_out: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", b.freeze(self.capabilities))

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "ModelCard":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        return cls(b.string(d, "id", path, nonempty=True), b.string(d, "provider", path),
                   b.integer(d, "context_tokens", path), b.strings(d, "capabilities", path),
                   b.number(d, "usd_per_mtok_in", path), b.number(d, "usd_per_mtok_out", path))

    def to_dict(self) -> dict:
        return {"id": self.id, "provider": self.provider, "context_tokens": self.context_tokens,
                "capabilities": b.thaw(self.capabilities), "usd_per_mtok_in": self.usd_per_mtok_in,
                "usd_per_mtok_out": self.usd_per_mtok_out}


@dataclass(frozen=True)
class ErrorClass:
    kind: str
    retryable: bool
    retry_after_s: Optional[float] = None

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "ErrorClass":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        after = d.get("retry_after_s")
        if after is not None:
            after = b.number(d, "retry_after_s", path)
        return cls(b.enum(d, "kind", ERROR_KINDS, path), b.boolean(d, "retryable", path), after)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "retryable": self.retryable, "retry_after_s": self.retry_after_s}
