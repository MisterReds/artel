"""Microkernel: services, hooks, tools and the append-only event log.

Everything above this file is a plugin (DeepSeek Harness "everything is a plugin"):
LLM providers, tools, gates and observers register themselves on a Kernel via
``setup(kernel)``. Waterfall hooks let a plugin wrap a step (``tool/execute``,
``llm/request``) and call ``next_`` to delegate, or short-circuit it.

The event log is the single source of truth for transparency: every model
request, tool call, board transition and decision is appended to
``events.jsonl`` ("model-visible means logged") and streamed to the dashboard.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


class EventLog:
    """Append-only JSONL log with a monotonic sequence and live subscribers."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = self._last_seq()

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open("rb") as fh:
            for line in fh:
                try:
                    last = json.loads(line)["seq"]
                except (ValueError, KeyError):
                    continue
        return last

    @property
    def seq(self) -> int:
        return self._seq

    def append(self, kind: str, **payload: Any) -> dict:
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "kind": kind, **payload}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._cond.notify_all()
        return event

    def read(self, after: int = 0, limit: int = 10_000, kinds: Optional[set] = None) -> list[dict]:
        out: list[dict] = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev["seq"] <= after or (kinds and ev["kind"] not in kinds):
                    continue
                out.append(ev)
                if len(out) >= limit:
                    break
        return out

    def wait(self, after: int, timeout: float) -> bool:
        """Block until an event newer than ``after`` exists or the timeout elapses."""
        with self._cond:
            if self._seq > after:
                return True
            self._cond.wait(timeout)
            return self._seq > after


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., str]
    # Tools a role may receive; roles: worker, reviewer, librarian, auditor, manager, planner, refiner.
    roles: tuple = ("worker",)
    terminal: bool = False

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class Kernel:
    config: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=lambda: defaultdict(list))
    tools: dict = field(default_factory=dict)
    plugins: list = field(default_factory=list)

    # --- services -------------------------------------------------------
    def provide(self, name: str, obj: Any) -> None:
        self.services[name] = obj

    def get(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    # --- hooks ----------------------------------------------------------
    def on(self, event: str, fn: Callable, priority: int = 0) -> Callable[[], None]:
        entry = (priority, fn)
        self.hooks[event].append(entry)
        self.hooks[event].sort(key=lambda e: -e[0])
        return lambda: self.hooks[event].remove(entry)

    def emit(self, event: str, **payload: Any) -> None:
        """Fire-and-forget observers; a failing observer never breaks the caller."""
        for _, fn in list(self.hooks.get(event, ())):
            try:
                fn(**payload)
            except Exception as exc:  # noqa: BLE001 - observers are untrusted plugins
                log = self.get("log")
                if log is not None:
                    log.append("plugin/error", hook=event, error=repr(exc))

    def waterfall(self, event: str, ctx: dict, final: Callable[[dict], Any]) -> Any:
        """Run listeners as a chain; each gets ``(ctx, next_)`` and may delegate or return."""
        chain = [fn for _, fn in self.hooks.get(event, ())]

        def call(i: int, c: dict) -> Any:
            if i == len(chain):
                return final(c)
            return chain[i](c, lambda c2=None: call(i + 1, c2 if c2 is not None else c))

        return call(0, ctx)

    # --- tools ----------------------------------------------------------
    def register_tool(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def tools_for(self, role: str, names: Optional[list] = None) -> list[ToolSpec]:
        specs = [t for t in self.tools.values() if role in t.roles]
        if names is not None:
            specs = [t for t in specs if t.name in names]
        return specs

    # --- plugins --------------------------------------------------------
    def load_plugin(self, dotted: str) -> None:
        module = importlib.import_module(dotted)
        module.setup(self)
        self.plugins.append(dotted)


DEFAULT_PLUGINS = [
    "artel.plugins.llm_providers",
    "artel.plugins.builtin_tools",
    "artel.plugins.skill_tools",
    "artel.plugins.board_tools",
    "artel.plugins.evolve_tools",
]


def build_kernel(config: dict) -> Kernel:
    kernel = Kernel(config=config)
    for dotted in DEFAULT_PLUGINS + list(config.get("plugins", [])):
        kernel.load_plugin(dotted)
    return kernel
