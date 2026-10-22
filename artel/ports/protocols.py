"""Port protocols. Each docstring states the contract every adapter and fake must keep."""
from __future__ import annotations

from typing import Any, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from artel.contracts import (ErrorClass, Event, ExecutorResult, ModelCard, NormRequest, NormResponse,
                             TaskContract, ToolResult, Usage)


@runtime_checkable
class LLMPort(Protocol):
    """One call to a model. Responses always carry Usage; failures raise and are mapped by classify()."""

    def call(self, req: NormRequest) -> NormResponse:
        """Run one request to completion; the response has non-negative usage and latency."""

    def stream(self, req: NormRequest) -> Iterator[dict]:
        """Yield incremental events; the last one is {"type": "end", "response": NormResponse}."""

    def count_tokens(self, req: NormRequest) -> int:
        """Return the input token count of req without running it."""

    def estimate_cost(self, usage: Usage) -> float:
        """Return the cost in USD of usage under this model's prices; never negative."""

    def classify(self, err: BaseException) -> ErrorClass:
        """Map any error raised by call/stream to an ErrorClass; never raises."""

    def card(self) -> ModelCard:
        """Describe the model; capabilities are facts, not requirements."""


@runtime_checkable
class AgentPort(Protocol):
    """A whole agent with its own loop executing a contract."""

    def run(self, contract: TaskContract, workspace: str, tools: Sequence[str]) -> ExecutorResult:
        """Execute contract inside workspace using only the named tools; the result carries usage."""

    def card(self) -> ModelCard:
        """Describe the agent."""

    def classify(self, err: BaseException) -> ErrorClass:
        """Map an error raised by run to an ErrorClass; never raises."""


@runtime_checkable
class MemoryPort(Protocol):
    """Content-addressed blob store plus a rolling summary."""

    def put(self, blob: bytes) -> str:
        """Store blob and return its reference; equal blobs give equal references."""

    def get(self, ref: str) -> bytes:
        """Return the blob for ref; raise KeyError when unknown."""

    def summary(self) -> str:
        """Return the current summary text."""

    def apply_delta(self, delta: Mapping[str, Any]) -> None:
        """Merge delta into the summary state; keys with None values are removed."""


@runtime_checkable
class EventPort(Protocol):
    """Append-only journal. Existing events never change and sequence numbers grow by one."""

    def append(self, event: Event) -> int:
        """Validate event, store it under the next sequence number (the given seq is ignored) and return it."""

    def read(self, after: int = 0) -> Iterator[Event]:
        """Yield stored events with seq greater than after, in order."""


@runtime_checkable
class ToolPort(Protocol):
    """Run a named tool under explicit grants."""

    def run(self, name: str, args: Mapping[str, Any], grants: Sequence[str]) -> ToolResult:
        """Run the tool; an error in the tool is a ToolResult with is_error, a missing grant is is_error too."""


@runtime_checkable
class ClockPort(Protocol):
    """Time source."""

    def now(self) -> float:
        """Seconds since the epoch; monotonic non-decreasing."""

    def sleep(self, seconds: float) -> None:
        """Wait for seconds (negative is an error)."""


@runtime_checkable
class ConfigPort(Protocol):
    """Read-only configuration, read at every decision."""

    def get(self, path: str, default: Any = None) -> Any:
        """Return the value at a dotted path, or default when absent; callers cannot mutate the source."""
