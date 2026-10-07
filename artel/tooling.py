"""Tool execution context shared by every tool plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def window_text(text: str, limit: int, head_frac: float = 0.3, marker: str = "\n…[{n} chars cut]…\n") -> str:
    """Keep the head and the (larger) tail of ``text``, cutting the middle; total length is ~``limit``.

    Tails carry summary lines ("412 passed"), heads carry the command context. Idea adapted from
    Qwen Code packages/core/src/goals/goal-evidence.ts (Apache-2.0).
    """
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    # the marker text depends on the cut size, so size it with the final count and give the rest to head/tail
    for _ in range(3):
        mark = marker.format(n=cut)
        keep = max(limit - len(mark), 2)
        head = int(keep * min(max(head_frac, 0.0), 0.5))
        cut = len(text) - keep
    mark = marker.format(n=cut)
    return text[:head] + mark + (text[len(text) - (keep - head):] if keep - head else "")


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
    read_stamps: dict = field(default_factory=dict)   # resolved path -> (mtime_ns, size) at last read/write
    deliverables: Optional[Path] = None               # the task's own worktree of shared/ (isolation), else None

    @property
    def shared_dir(self) -> Path:
        """Where ``shared/...`` points for this agent: the task worktree under isolation, else the project's."""
        return Path(self.deliverables) if self.deliverables else self.project.shared

    @property
    def shared_note(self) -> str:
        """A sentence for runtime prompts explaining an isolated deliverables directory ('' without isolation)."""
        if not self.deliverables:
            return ""
        return (" It is this task's own git worktree of the deliverables: it is merged into the common deliverables "
                "only when a reviewer accepts the task; do not write to other copies.")

    def record_stamp(self, p: Path) -> None:
        """Remember the file's (mtime_ns, size); idea from Qwen Code fileReadCache.ts (Apache-2.0)."""
        try:
            st = p.stat()
        except OSError:
            self.read_stamps.pop(str(p), None)
            return
        self.read_stamps[str(p)] = (st.st_mtime_ns, st.st_size)

    def is_stale(self, p: Path) -> bool:
        """True when the file was read/written here before and has since changed on disk."""
        stamp = self.read_stamps.get(str(p))
        if stamp is None:
            return False
        try:
            st = p.stat()
        except OSError:
            return True
        return (st.st_mtime_ns, st.st_size) != stamp

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
            target = (self.shared_dir / Path(*p.parts[1:])).resolve()
        else:
            target = (self.workspace / p).resolve()
        def inside(base: Path) -> bool:
            base = base.resolve()
            return target == base or base in target.parents

        if not inside(root) and not inside(self.project.shared):
            if not write and any(inside(r) for r in getattr(self.project, "refs", [])):
                return target  # read-only reference sources
            raise ToolError(f"path {path!r} is outside the project; use a workspace-relative path or shared/...")
        if write and self.deliverables and not inside(self.shared_dir) and (
                inside(self.project.shared) or inside(self.project.root / "worktrees")):
            # isolation: the common deliverables change only through an accepted merge
            raise ToolError(f"path {path!r} is the merged deliverables; write to shared/<path> (your task's "
                            "worktree) instead")
        if write:
            protected = {"events.jsonl", "board.db", "project.json"}
            if target.parent == root and target.name in protected:
                raise ToolError(f"{target.name} is managed by the harness")
        return target

    def display(self, p: Path) -> str:
        p = p.resolve()
        shared = self.shared_dir.resolve()
        if p == shared or shared in p.parents:
            return "shared/" + p.relative_to(shared).as_posix() if p != shared else "shared/"
        try:
            return p.relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return str(p)
