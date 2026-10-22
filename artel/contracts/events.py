"""Journal event envelope and the per-kind table of required data fields (defect: readers crashed on missing keys)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import _base as b

# Required data fields for the kinds the current journal writes. Unknown kinds are allowed with no requirements.
REQUIRED_FIELDS = {
    "llm/response": ("turn", "stop_reason", "blocks"),
    "llm/error": ("error",),
    "tool/call": ("tool", "input", "call_id"),
    "tool/result": ("tool", "call_id", "is_error", "output"),
    "agent/start": ("role", "purpose", "model", "tools"),
    "agent/end": ("status", "turns", "tool_calls", "usage"),
    "tier/usage": ("role", "tier", "usage"),
    "review/verdict": ("task_id", "passed", "feedback", "gates"),
    "audit/verdict": ("round", "complete", "summary"),
    "gate/result": ("task_id", "command", "exit"),
    "board/task_created": ("task_id", "title"),
    "board/task_moved": ("task_id", "frm", "to"),
    "board/comment": ("task_id", "author", "body"),
    "run/started": ("task_id", "worker", "run_id"),
    "run/finished": ("run_id", "status", "summary"),
}


@dataclass(frozen=True)
class Event:
    """Not hashable: data is a read-only mapping proxy; compare with ==, do not use as a set member or key."""
    seq: int
    ts: float
    kind: str
    data: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", b.freeze(self.data))

    @classmethod
    def from_dict(cls, d: Any, path: str = "") -> "Event":
        """Extra payload fields are allowed (data is open). Accepts the flat journal line: seq, ts, kind plus the payload fields at the top level."""
        path = b.check_path(path)
        d = b.obj(d, path)
        seq = b.integer(d, "seq", path)
        ts = b.number(d, "ts", path)
        kind = b.string(d, "kind", path, nonempty=True)
        data = {k: v for k, v in d.items() if k not in ("seq", "ts", "kind")}
        for field in REQUIRED_FIELDS.get(kind, ()):
            if field not in data:
                raise b.ContractError(f"{path}{kind}.{field}", "required field is missing")
        return cls(seq, ts, kind, data)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind, **b.thaw(self.data)}
