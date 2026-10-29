"""Tool execution context shared by every tool plugin."""

from __future__ import annotations

import itertools
import os
import re
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


TOOL_OUTPUT_CAP = 12_000          # chars a single tool result may occupy in history, fence included
SPILL_DIR = ".spill"
_FENCE = ("<tool_output>\n", "\n</tool_output>")
_CLOSING = re.compile(r"(<\s*)/(\s*tool_output)", re.I)
_spill_seq = itertools.count(1)


def tool_output_cap(config: Any) -> int:
    return int((config or {}).get("tool_output_cap", TOOL_OUTPUT_CAP))


def cap_output(ctx: "ToolContext", text: str, tool: str, limit: Optional[int] = None) -> str:
    """The one place where tool output is bounded: head + tail within ``limit`` chars, the full text spilled to a file.

    The file lives in <workspace>/.spill/ (hidden, so list_files skips it and it stays out of deliverables); the
    marker names it. Output within the limit is returned unchanged, so applying it twice is harmless. Idea adapted
    from OpenCode packages/opencode/src/tool/truncate.ts (MIT License, Copyright (c) 2025 opencode).
    """
    if limit is None:
        limit = tool_output_cap(ctx.kernel.config) - sum(map(len, _FENCE))    # room for the fence
    if len(text) <= limit:
        return text
    d = Path(ctx.workspace) / SPILL_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{tool}-{os.getpid()}-{next(_spill_seq)}.txt"
    while f.exists():
        f = d / f"{tool}-{os.getpid()}-{next(_spill_seq)}.txt"
    f.write_text(text, encoding="utf-8")
    rel = f"{SPILL_DIR}/{f.name}"
    marker = f"\n… {{n}} символов пропущено, полный вывод: {rel} (read_file с offset/limit)\n"
    return window_text(text, limit, marker=marker)


def fence(text: str) -> str:
    """Wrap untrusted tool output in <tool_output> tags; a closing tag inside it is neutralised (same length)."""
    return _FENCE[0] + _CLOSING.sub(r"\1\\\2", text) + _FENCE[1]


def present_output(ctx: "ToolContext", tool: str, text: str) -> str:
    """What the model sees of a tool result: capped, spilled to a file when long, and fenced."""
    return fence(cap_output(ctx, text, tool))


class ToolError(Exception):
    """Raised by a tool to return ``is_error`` to the model without crashing the loop."""


# roles that never get write grants (the runtimes import it from here)
READONLY_ROLES = {"planner", "reviewer", "auditor", "manager", "evolver", "absorber", "doctor", "intake"}


def _paths(project: Any, values: Any) -> list:
    out = []
    for v in values or []:
        q = Path(str(v)).expanduser()
        out.append(str((q if q.is_absolute() else project.root / q).resolve()))
    return out


def effective_grants(project: Any, role: str, workspace: Optional[Path] = None,
                     deliverables: Optional[Path] = None) -> dict:
    """Resolved path grants as plain string lists: ``{"read": [...], "write": [...]}``.

    Defaults: write = the role's workspace + the deliverables (the task's own worktree under isolation, else the
    project's shared/); read = write + the project root + refs. Refs are never writable. Extra paths come from
    ``grants`` in the project meta or config: ``{"read": [..], "write": [..], "roles": {role: {..}}}``; they only
    add paths, and a write grant also grants read. Relative paths are relative to the project root.
    """
    shared = Path(deliverables) if deliverables else project.shared
    write = [str(Path(workspace if workspace is not None else shared).resolve()), str(shared.resolve())]
    read = [str(project.root.resolve()), *[str(Path(r).resolve()) for r in getattr(project, "refs", [])]]
    refs = set(read[1:])
    for src in (getattr(project, "config", None) or {}, getattr(project, "meta", None) or {}):
        g = src.get("grants") if isinstance(src, dict) else None
        if not isinstance(g, dict):
            continue
        for layer in (g, (g.get("roles") or {}).get(role) or {}):
            read += _paths(project, layer.get("read"))
            write += _paths(project, layer.get("write"))
    if role in READONLY_ROLES:
        write = []
    write = [w for w in dict.fromkeys(write) if w not in refs]
    return {"read": list(dict.fromkeys(write + read)), "write": write}


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

        if write and self.deliverables and not inside(self.shared_dir) and (
                inside(self.project.shared) or inside(self.project.root / "worktrees")):
            # isolation: the common deliverables change only through an accepted merge
            raise ToolError(f"path {path!r} is the merged deliverables; write to shared/<path> (your task's "
                            "worktree) instead")
        allowed = self.grants()["write" if write else "read"]
        if not any(inside(Path(g)) for g in allowed):
            kind = "write" if write else "read"
            shown = "[" + ", ".join(f"'{g}'" for g in allowed) + "]"  # not repr(): it doubles Windows backslashes
            raise ToolError(f"{kind} to '{path}' denied: not in {kind} grants {shown}; "
                            "use a workspace-relative path or shared/...")
        if write:
            protected = {"events.jsonl", "board.db", "project.json"}
            if target.parent == root and target.name in protected:
                raise ToolError(f"{target.name} is managed by the harness")
        return target

    def grants(self) -> dict:
        return effective_grants(self.project, self.role, self.workspace, self.deliverables)

    def display(self, p: Path) -> str:
        p = p.resolve()
        shared = self.shared_dir.resolve()
        if p == shared or shared in p.parents:
            return "shared/" + p.relative_to(shared).as_posix() if p != shared else "shared/"
        try:
            return p.relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return str(p)
