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
        from .reports import redact_deep  # the journal is pushed to git by evolution: no secrets in it

        clean = redact_deep(payload)  # before the lock: redaction of a big payload must not stall other writers
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "kind": kind, **clean}
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


class PluginError(Exception):
    pass


@dataclass
class PluginRecord:
    name: str
    module: Any
    manifest: dict
    effects: list = field(default_factory=list)   # undo callbacks, applied in reverse on unload
    loaded_at: float = field(default_factory=time.time)


class Kernel:
    """Plugin microkernel (DeepSeek Harness / Cordis model, kept small).

    Every registration — service, tool, hook, runtime — is an *effect* owned by the plugin that made it.
    Unloading a plugin reverses its effects; services and tools are stacks, so unloading an override
    restores the implementation underneath. A failure inside ``setup`` rolls back what it registered.
    Lookups on the hot path stay plain dict reads: stacks only update the flat views on load/unload.

    A plugin is a module with ``setup(kernel)`` and an optional manifest::

        PLUGIN = {"name": "...", "version": "1", "provides": ["service:x", "tool:y"], "requires": ["service:skills"]}

    ``setup`` may return a callable; it runs as the last undo step on unload (close sockets, stop threads).
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.services: dict = {}          # flat view: name -> active object
        self.tools: dict = {}             # flat view: name -> active ToolSpec
        self.hooks: dict = defaultdict(list)
        self.records: dict[str, PluginRecord] = {}
        self._service_stacks: dict = defaultdict(list)
        self._tool_stacks: dict = defaultdict(list)
        self._loading: list[str] = []     # plugin currently running setup (nested loads allowed)
        self._lock = threading.RLock()
        self.failed: dict[str, str] = {}  # plugin -> error, for plugins that failed to load (never fatal)
        self._shut_down = False

    @property
    def plugins(self) -> list[str]:
        return list(self.records)

    # --- effects ----------------------------------------------------------
    def _effect(self, undo: Callable[[], None]) -> None:
        if self._loading:
            self.records[self._loading[-1]].effects.append(undo)

    # --- services -------------------------------------------------------
    def provide(self, name: str, obj: Any) -> None:
        with self._lock:
            entry = (self._loading[-1] if self._loading else None, obj)
            self._service_stacks[name].append(entry)
            self.services[name] = obj

            def undo() -> None:
                with self._lock:
                    stack = self._service_stacks[name]
                    if entry in stack:
                        stack.remove(entry)
                    if stack:
                        self.services[name] = stack[-1][1]
                    else:
                        self.services.pop(name, None)

            self._effect(undo)

    def get(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def provider_of(self, name: str) -> Optional[str]:
        stack = self._service_stacks.get(name)
        return stack[-1][0] if stack else None

    # --- hooks ----------------------------------------------------------
    def on(self, event: str, fn: Callable, priority: int = 0) -> Callable[[], None]:
        entry = (priority, fn)
        with self._lock:
            self.hooks[event].append(entry)
            self.hooks[event].sort(key=lambda e: -e[0])

        def off() -> None:
            with self._lock:
                if entry in self.hooks[event]:
                    self.hooks[event].remove(entry)

        self._effect(off)
        return off

    def emit(self, event: str, **payload: Any) -> None:
        """Fire-and-forget observers; a failing observer never breaks the caller.

        The error goes to the ``log`` service, or to the project log when the payload carries a ``project``.
        """
        listeners = self.hooks.get(event)
        if not listeners:                 # fast path: nobody listens
            return
        for _, fn in list(listeners):
            try:
                fn(**payload)
            except Exception as exc:  # noqa: BLE001 - observers are untrusted plugins
                log = self.get("log") or getattr(payload.get("project"), "log", None)
                if log is not None:
                    log.append("plugin/error", hook=event, error=repr(exc),
                               handler=f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}")

    def waterfall(self, event: str, ctx: dict, final: Callable[[dict], Any]) -> Any:
        """Run listeners as a chain; each gets ``(ctx, next_)`` and may delegate or return."""
        listeners = self.hooks.get(event)
        if not listeners:                 # fast path: no interception registered
            return final(ctx)
        chain = [fn for _, fn in listeners]

        def call(i: int, c: dict) -> Any:
            if i == len(chain):
                return final(c)
            return chain[i](c, lambda c2=None: call(i + 1, c2 if c2 is not None else c))

        return call(0, ctx)

    # --- tools ----------------------------------------------------------
    def register_tool(self, spec: ToolSpec) -> None:
        with self._lock:
            self._tool_stacks[spec.name].append(spec)
            self.tools[spec.name] = spec

            def undo() -> None:
                with self._lock:
                    stack = self._tool_stacks[spec.name]
                    if spec in stack:
                        stack.remove(spec)
                    if stack:
                        self.tools[spec.name] = stack[-1]
                    else:
                        self.tools.pop(spec.name, None)

            self._effect(undo)

    def tools_for(self, role: str, names: Optional[list] = None) -> list[ToolSpec]:
        specs = [t for t in self.tools.values() if role in t.roles]
        if names is not None:
            specs = [t for t in specs if t.name in names]
        return specs

    # --- runtimes (agent loop implementations are plugins too) -------------
    def register_runtime(self, name: str, fn: Callable[..., Any]) -> None:
        self.provide(f"runtime:{name}", fn)

    # --- plugins --------------------------------------------------------
    def load_plugin(self, dotted: str, module: Any = None) -> PluginRecord:
        with self._lock:
            if dotted in self.records:
                return self.records[dotted]
            module = module or importlib.import_module(dotted)
            manifest = dict(getattr(module, "PLUGIN", {}) or {})
            manifest.setdefault("name", dotted)
            for req in manifest.get("requires", []):
                kind, _, what = req.partition(":")
                ok = (what in self.services) if kind == "service" else (what in self.tools) if kind == "tool" \
                    else (what in self.records) if kind == "plugin" else True
                if not ok:
                    raise PluginError(f"plugin {dotted} requires {req}, which is not available")
            record = PluginRecord(dotted, module, manifest)
            self.records[dotted] = record
            self._loading.append(dotted)
            try:
                teardown = module.setup(self)
                if callable(teardown):
                    record.effects.append(teardown)
            except Exception:
                self._loading.pop()
                self._undo(record)
                del self.records[dotted]
                raise
            self._loading.pop()
            return record

    def load_plugin_safe(self, dotted: str, module: Any = None) -> Optional[PluginRecord]:
        """Load a third-party plugin; a failure (import error, unmet requires, crash in ``setup``) is recorded
        in :attr:`failed` and rolled back instead of crashing Artel."""
        try:
            record = self.load_plugin(dotted, module)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - untrusted code must not take Artel down
            self.failed[dotted] = f"{type(exc).__name__}: {exc}"
            return None
        self.failed.pop(dotted, None)
        return record

    def shutdown(self, reason: str = "exit") -> None:
        """End of the session: emit ``session_shutdown`` once, then unload plugins newest first."""
        with self._lock:
            if self._shut_down:
                return
            self._shut_down = True
        self.emit("session_shutdown", reason=reason)
        for name in reversed(list(self.records)):
            try:
                self.unload_plugin(name)
            except Exception:  # noqa: BLE001 - keep unloading the rest
                continue

    def _undo(self, record: PluginRecord) -> None:
        for undo in reversed(record.effects):
            try:
                undo()
            except Exception as exc:  # noqa: BLE001 - keep unwinding the rest
                log = self.get("log")
                if log is not None:
                    log.append("plugin/error", plugin=record.name, error=repr(exc))
        record.effects.clear()

    def unload_plugin(self, dotted: str) -> None:
        with self._lock:
            record = self.records.pop(dotted, None)
            if record is None:
                raise PluginError(f"plugin {dotted} is not loaded")
            self._undo(record)

    def reload_plugin(self, dotted: str) -> PluginRecord:
        """Hot reload: unload, re-import the module, load again."""
        with self._lock:
            record = self.records.get(dotted)
            if record is not None:
                self.unload_plugin(dotted)
                module = importlib.reload(record.module)
            else:
                module = importlib.import_module(dotted)
            return self.load_plugin(dotted, module)

    def describe(self) -> list[dict]:
        return [{"name": r.name, "version": r.manifest.get("version"), "provides": r.manifest.get("provides", []),
                 "requires": r.manifest.get("requires", []), "effects": len(r.effects)} for r in self.records.values()]


# Lifecycle events (observers via ``kernel.on(event, fn)``; ``fn(**payload)``, errors never reach the caller).
# Payload keys are documented in docs/ARCHITECTURE.md; ``agent`` in before_agent_start is mutable.
LIFECYCLE_EVENTS = {
    "project_start": "project, resumed",
    "before_agent_start": "project, role, purpose, task, persona, agent{system, first_user, tool_names} (mutable)",
    "agent_end": "project, role, purpose, task, outcome",
    "task_done": "project, task, feedback",
    "project_end": "project, status, usage",
    "session_shutdown": "reason",
}

DEFAULT_PLUGINS = [
    "artel.plugins.llm_providers",
    "artel.plugins.runtimes",
    "artel.plugins.builtin_tools",
    "artel.plugins.skill_tools",
    "artel.plugins.board_tools",
    "artel.plugins.evolve_tools",
    "artel.plugins.provision_tools",
]


# Built-ins Artel cannot work without: never disabled from the plugin manager.
CORE_PLUGINS = {"artel.plugins.llm_providers", "artel.plugins.runtimes", "artel.plugins.builtin_tools",
                "artel.plugins.board_tools"}


def build_kernel(config: dict) -> Kernel:
    """Built-ins, then ``plugins:`` from the config, then enabled plugin packages (ARTEL_HOME/plugins.json).

    A built-in that fails is a bug in Artel and raises; a configured or installed plugin that fails is
    recorded in ``kernel.failed`` (packages are also switched off with the error) and Artel keeps going.
    """
    kernel = Kernel(config=config)
    use_packages = config.get("plugin_packages", True)
    disabled = set(config.get("disable_plugins", []))
    if use_packages:
        from .packages import disabled_builtins

        disabled |= disabled_builtins() - CORE_PLUGINS
    for dotted in DEFAULT_PLUGINS:
        if dotted not in disabled:
            kernel.load_plugin(dotted)
    for dotted in config.get("plugins", []):
        if dotted not in disabled:
            kernel.load_plugin_safe(dotted)
    if use_packages:
        from .packages import load_enabled

        load_enabled(kernel)
    return kernel
