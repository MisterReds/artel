"""AgentEvent: one normalised event of a CLI agent's output stream (what a codec's parse(line) returns)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import _base as b

AGENT_EVENT_KINDS = ("session", "text", "tool_call", "tool_result", "command", "usage", "end", "error")
# kind -> required data fields and their accepted types; any other JSON field is allowed and codec-defined
_REQUIRED = {
    "session": {"id": (str,)},
    "text": {"text": (str,)},
    "tool_call": {"id": (str,), "name": (str,), "input": (Mapping,)},
    "tool_result": {"id": (str,), "output": (str,), "is_error": (bool,)},
    "command": {"command": (str,)},
    "usage": {"input_tokens": (int,), "output_tokens": (int,)},
    "end": {"is_error": (bool,)},
    "error": {"message": (str,)},
}


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    data: Mapping

    def __post_init__(self) -> None:
        if self.kind not in AGENT_EVENT_KINDS:
            raise b.ContractError("kind", f"must be one of {sorted(AGENT_EVENT_KINDS)}")
        if not isinstance(self.data, Mapping):
            raise b.ContractError("data", "expected an object")
        object.__setattr__(self, "data", b.freeze(self.data, "data."))
        for key, types_ in _REQUIRED[self.kind].items():
            if key not in self.data:
                raise b.ContractError(f"data.{key}", "required field is missing")
            v = self.data[key]
            if not isinstance(v, types_) or (isinstance(v, bool) and bool not in types_):
                raise b.ContractError(f"data.{key}", "expected " + " or ".join(
                    "object" if t is Mapping else t.__name__ for t in types_))
        if self.kind == "usage":
            for key in ("input_tokens", "output_tokens", "cached"):
                if self.data.get(key, 0) < 0:
                    raise b.ContractError(f"data.{key}", "must not be negative")

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "AgentEvent":
        path = b.check_path(path)
        d = b.obj(d, path)
        b.reject_unknown(d, cls, path)
        return cls(b.enum(d, "kind", AGENT_EVENT_KINDS, path), b.mapping(d, "data", path))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "data": b.thaw(self.data)}
