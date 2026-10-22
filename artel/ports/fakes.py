"""In-memory fakes for every port, for tests and for running the kernel without adapters."""
from __future__ import annotations

import copy
import hashlib
import math
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from artel.contracts import (ErrorClass, Event, ExecutorResult, ModelCard, NormRequest, NormResponse,
                             TaskContract, ToolResult, Usage)


class ScriptedLLM:
    """Returns the scripted responses in order; records every request."""

    def __init__(self, responses: Sequence[NormResponse], card: Optional[ModelCard] = None):
        self._script = list(responses)
        self.requests: list = []
        self._card = card or ModelCard("scripted", "fake", 100_000, (), 0.0, 0.0)

    def call(self, req: NormRequest) -> NormResponse:
        self.requests.append(req)
        if not self._script:
            raise RuntimeError("scripted LLM has no more responses")
        return self._script.pop(0)

    def stream(self, req: NormRequest) -> Iterator[dict]:
        yield {"type": "end", "response": self.call(req)}

    def count_tokens(self, req: NormRequest) -> int:
        return sum(len(str(m.get("content", "")).split()) for m in req.messages)

    def estimate_cost(self, usage: Usage) -> float:
        return (usage.in_ * self._card.usd_per_mtok_in + usage.out * self._card.usd_per_mtok_out) / 1_000_000

    def classify(self, err: BaseException) -> ErrorClass:
        return ErrorClass("unknown", False)

    def card(self) -> ModelCard:
        return self._card


class ScriptedAgent:
    """Returns the scripted results in order; records every contract."""

    def __init__(self, results: Sequence[ExecutorResult], card: Optional[ModelCard] = None):
        self._script = list(results)
        self.runs: list = []
        self._card = card or ModelCard("scripted-agent", "fake", 100_000, (), 0.0, 0.0)

    def run(self, contract: TaskContract, workspace: str, tools: Sequence[str]) -> ExecutorResult:
        self.runs.append((contract, workspace, tuple(tools)))
        if not self._script:
            raise RuntimeError("scripted agent has no more results")
        return self._script.pop(0)

    def card(self) -> ModelCard:
        return self._card

    def classify(self, err: BaseException) -> ErrorClass:
        return ErrorClass("unknown", False)


class DictMemory:
    """Blobs keyed by content hash; the summary is a dict rendered in key order."""

    def __init__(self) -> None:
        self._blobs: dict = {}
        self._state: dict = {}

    def put(self, blob: bytes) -> str:
        ref = "sha256:" + hashlib.sha256(blob).hexdigest()
        self._blobs[ref] = bytes(blob)
        return ref

    def get(self, ref: str) -> bytes:
        return self._blobs[ref]

    def summary(self) -> str:
        return "\n".join(f"{k}: {self._state[k]}" for k in sorted(self._state))

    def apply_delta(self, delta: Mapping[str, Any]) -> None:
        for k, v in delta.items():
            if v is None:
                self._state.pop(k, None)
            else:
                self._state[k] = copy.deepcopy(v)


class ListEventLog:
    """Append-only list; sequence numbers start at 1."""

    def __init__(self) -> None:
        self._events: list = []

    def append(self, event: Event) -> int:
        stored = Event.from_dict({**event.to_dict(), "seq": len(self._events) + 1})
        self._events.append(stored)
        return stored.seq

    def read(self, after: int = 0) -> Iterator[Event]:
        return iter([e for e in self._events if e.seq > after])


class RecordingToolRunner:
    """Records calls; answers with the handler for the tool name, or an error result when none or not granted."""

    def __init__(self, handlers: Optional[Mapping[str, Callable[[Mapping[str, Any]], str]]] = None):
        self._handlers = dict(handlers or {})
        self.calls: list = []

    def run(self, name: str, args: Mapping[str, Any], grants: Sequence[str]) -> ToolResult:
        self.calls.append((name, copy.deepcopy(dict(args)), tuple(grants)))
        if name not in grants:
            return ToolResult(f"tool not granted: {name}", True)
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(f"unknown tool: {name}", True)
        try:
            out = handler(copy.deepcopy(dict(args)))
            if not isinstance(out, str):
                return ToolResult(f"handler returned {type(out).__name__}, not str", True)
            return ToolResult(out, False)
        except Exception as exc:  # noqa: BLE001 - a failing tool is a result, not a crash
            return ToolResult(f"{type(exc).__name__}: {exc}", True)


class FakeClock:
    """Time moves only when sleep is called."""

    def __init__(self, start: float = 0.0) -> None:
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start):
            raise ValueError("start must be a finite number")
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        try:
            finite = not isinstance(seconds, bool) and isinstance(seconds, (int, float)) and math.isfinite(seconds)
        except OverflowError:
            finite = False
        if not finite or seconds < 0:
            raise ValueError("sleep: seconds must be a finite number >= 0")
        self._now += seconds


class DictConfig:
    """Read-only view over a nested dict; values are copied on the way in and out."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = copy.deepcopy(dict(data))

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in path.split("."):
            if not isinstance(cur, Mapping) or part not in cur:
                return default
            cur = cur[part]
        return copy.deepcopy(cur)
