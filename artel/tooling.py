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


READONLY_ROLES = {"planner", "reviewer", "auditor", "manager", "evolver", "absorber"}


def effective_grants(project: Any, role: str, workspace: Any = None) -> dict:
    """Effective ``{"read": [...], "write": [...]}`` path grants of ``role`` as plain string lists.

    Defaults: write = role workspace + project shared deliverables (workdir); read = write + project root +
    refs. Refs are read-only and never writable. Overrides come from project meta
    ``grants = {"read": [..], "write": [..], "roles": {role: {"read": [..], "write": [..]}}}``; entries add
    to the defaults. Read-only roles (READONLY_ROLES) get no write grants.
    """
    meta = {}
    try:
        meta = project.meta or {}
    except (OSError, ValueError, AttributeError):
        pass
    over = meta.get("grants") or {}
    per_role = (over.get("roles") or {}).get(role) or {}
    ws = Path(workspace) if workspace is not None else project.shared
    write: list = []
    if role not in READONLY_ROLES:
        write = [ws, project.shared, *over.get("write", []), *per_role.get("write", [])]
    refs = [Path(r).resolve() for r in getattr(project, "refs", [])]
    # a ref can be read, never written, even if an override asks for it
    def writable(x: Path) -> bool:
        return not any(x == r or r in x.parents for r in refs)

    write_p = [Path(w).resolve() for w in write]
    write_p = [w for w in write_p if writable(w)]
    read_p = [*write_p, project.root.resolve(), *refs,
              *[Path(r).resolve() for r in [*over.get("read", []), *per_role.get("read", [])]]]

    def uniq(paths: list) -> list:
        return list(dict.fromkeys(str(x) for x in paths))

    return {"read": uniq(read_p), "write": uniq(write_p)}


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
            target = (self.project.shared / Path(*p.parts[1:])).resolve()
        else:
            target = (self.workspace / p).resolve()
        grants = self.grants()
        kind = "write" if write else "read"
        if not any(target == Path(g) or Path(g) in target.parents for g in grants[kind]):
            raise ToolError(f"{kind} to {path!r} denied: {target} is not in {kind} grants {grants[kind]}"
                            + ("" if write or not self.role else f" (role {self.role})"))
        if write:
            protected = {"events.jsonl", "board.db", "project.json"}
            if target.parent == root and target.name in protected:
                raise ToolError(f"{target.name} is managed by the harness")
        return target

    def grants(self) -> dict:
        return effective_grants(self.project, self.role, self.workspace)

    def display(self, p: Path) -> str:
        p = p.resolve()
        shared = self.project.shared.resolve()
        if p == shared or shared in p.parents:
            return "shared/" + str(p.relative_to(shared)) if p != shared else "shared/"
        try:
            return str(p.relative_to(self.workspace.resolve()))
        except ValueError:
            return str(p)
