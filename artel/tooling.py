"""Tool execution context shared by every tool plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class ToolError(Exception):
    """Raised by a tool to return ``is_error`` to the model without crashing the loop."""


@dataclass
class ToolContext:
    kernel: Any
    project: Any
    role: str
    workspace: Path
    persona: Any = None
    task: Optional[dict] = None
    run_id: Optional[int] = None
    viewed: set = field(default_factory=set)          # (skill, file) pairs read this run: read-before-write
    edited_files: list = field(default_factory=list)
    outputs: list = field(default_factory=list)       # recent command outputs, for evidence checks
    terminal: Optional[tuple] = None                  # (tool_name, input) once a terminal tool accepted
    state: dict = field(default_factory=dict)

    @property
    def actor(self) -> str:
        return self.persona.name if self.persona else self.role

    def resolve(self, path: str, write: bool = False) -> Path:
        """``shared/...`` -> project deliverables; relative -> own workspace; never outside the project."""
        p = Path(path)
        root = self.project.root.resolve()
        if p.is_absolute():
            target = p.resolve()
        elif p.parts and p.parts[0] == "shared":
            target = (self.project.shared / Path(*p.parts[1:])).resolve()
        else:
            target = (self.workspace / p).resolve()
        def inside(base: Path) -> bool:
            base = base.resolve()
            return target == base or base in target.parents

        if not inside(root) and not inside(self.project.shared):
            if not write and any(inside(r) for r in getattr(self.project, "refs", [])):
                return target  # read-only reference sources
            raise ToolError(f"path {path!r} is outside the project; use a workspace-relative path or shared/...")
        if write:
            protected = {"events.jsonl", "board.db", "project.json"}
            if target.parent == root and target.name in protected:
                raise ToolError(f"{target.name} is managed by the harness")
        return target

    def display(self, p: Path) -> str:
        p = p.resolve()
        shared = self.project.shared.resolve()
        if p == shared or shared in p.parents:
            return "shared/" + str(p.relative_to(shared)) if p != shared else "shared/"
        try:
            return str(p.relative_to(self.workspace.resolve()))
        except ValueError:
            return str(p)
