"""Execution tools: shell, files, worker memory."""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

from ..kernel import ToolSpec
from ..tooling import ToolContext, ToolError, window_text

PLUGIN = {"name": "artel.plugins.builtin_tools", "version": "1",
          "provides": ["tool:bash", "tool:read_file", "tool:write_file", "tool:edit_file", "tool:list_files",
                       "tool:memory_write"]}
MAX_OUT = 20_000
MAX_LINES = 2000
MAX_BYTES = 50 * 1024
SPILL_DIR = ".spill"
_spill_seq = itertools.count(1)
EXEC_ROLES = ("worker", "reviewer", "auditor", "evolver", "absorber")


def _clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n…[{len(text) - limit} chars omitted]…\n" + text[-half:]


def spill_output(ctx: ToolContext, text: str, tool: str) -> str:
    """Return ``text`` unchanged, or spill oversized output to a file and return a preview plus its path.

    Over MAX_LINES lines or MAX_BYTES bytes, the full text goes to <workspace>/.spill/ (hidden, so
    list_files skips it and it stays out of shared deliverables). Idea adapted from OpenCode
    packages/opencode/src/tool/truncate.ts (MIT License, Copyright (c) 2025 opencode).
    """
    nbytes = len(text.encode())
    nlines = text.count("\n") + 1
    if nlines <= MAX_LINES and nbytes <= MAX_BYTES:
        return text
    d = Path(ctx.workspace) / SPILL_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{tool}-{os.getpid()}-{next(_spill_seq)}.txt"
    while f.exists():
        f = d / f"{tool}-{os.getpid()}-{next(_spill_seq)}.txt"
    f.write_text(text)
    rel = f"{SPILL_DIR}/{f.name}"
    return (f"{window_text(text, MAX_OUT)}\n\n[output too large ({nlines} lines, {nbytes} bytes); "
            f"full output saved to {rel}. Use read_file with path={rel} and offset/limit to view parts of it.]")


def _bound(ctx: ToolContext, text: str, tool: str) -> str:
    """Spill oversized output; otherwise apply the plain char clip."""
    spilled = spill_output(ctx, text, tool)
    return _clip(text) if spilled == text else spilled


def bash(ctx: ToolContext, command: str, timeout: int = 0) -> str:
    timeout = int(timeout or ctx.kernel.config.get("tool_timeout_s", 300))
    env = {**os.environ, "ARTEL_SHARED": str(ctx.project.shared), "ARTEL_WORKSPACE": str(ctx.workspace)}
    try:
        proc = subprocess.run(command, shell=True, cwd=ctx.workspace, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"timed out after {timeout}s; partial output:\n{_clip(str(exc.stdout or ''), 4000)}")
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    result = f"[exit {proc.returncode}]\n{_bound(ctx, out, 'bash')}"
    ctx.outputs.append(f"$ {command}\n{window_text(result, 3000)}")
    return result


def read_file(ctx: ToolContext, path: str, offset: int = 0, limit: int = 0) -> str:
    p = ctx.resolve(path)
    if not p.is_file():
        raise ToolError(f"{path} is not a file")
    lines = p.read_text(errors="replace").splitlines()
    end = offset + limit if limit else len(lines)
    ctx.record_stamp(p)
    body = "\n".join(f"{i + 1:>5}\t{ln}" for i, ln in enumerate(lines[offset:end], start=offset))
    return _bound(ctx, body, "read_file") or "(empty file)"


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = ctx.resolve(path, write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    ctx.record_stamp(p)
    ctx.edited_files.append(ctx.display(p))
    ctx.project.log.append("file/written", actor=ctx.actor, path=ctx.display(p), bytes=len(content.encode()))
    return f"wrote {len(content)} chars to {ctx.display(p)}"


def edit_file(ctx: ToolContext, path: str, old: str, new: str) -> str:
    p = ctx.resolve(path, write=True)
    if not p.is_file():
        raise ToolError(f"{path} is not a file")
    if ctx.is_stale(p):
        raise ToolError(f"{path} changed on disk since you last read it; re-read the file with read_file before editing it")
    text = p.read_text()
    n = text.count(old)
    if n != 1:
        raise ToolError(f"old text must match exactly once (matched {n})")
    p.write_text(text.replace(old, new))
    ctx.record_stamp(p)
    ctx.edited_files.append(ctx.display(p))
    ctx.project.log.append("file/edited", actor=ctx.actor, path=ctx.display(p))
    return f"edited {ctx.display(p)}"


def list_files(ctx: ToolContext, path: str = ".", depth: int = 3) -> str:
    base = ctx.resolve(path)
    if not base.exists():
        raise ToolError(f"{path} does not exist")
    out = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if len(rel.parts) > depth or any(part.startswith(".") for part in rel.parts):
            continue
        out.append(f"{rel}{'/' if p.is_dir() else ''}" + ("" if p.is_dir() else f"  ({p.stat().st_size} B)"))
        if len(out) >= 500:
            out.append("…truncated")
            break
    return _bound(ctx, "\n".join(out), "list_files") or "(empty)"


def memory_write(ctx: ToolContext, content: str, mode: str = "append") -> str:
    if ctx.persona is None:
        raise ToolError("only workers keep a MEMORY.md")
    p = ctx.project.worker_dir(ctx.persona.name) / "MEMORY.md"
    if mode == "replace":
        p.write_text(content if content.startswith("# MEMORY") else "# MEMORY\n\n" + content)
    else:
        with p.open("a") as fh:
            fh.write(content.rstrip() + "\n")
    ctx.project.log.append("worker/memory", worker=ctx.persona.name, mode=mode, content=content[:2000])
    return f"MEMORY.md {mode}d"


def setup(kernel) -> None:
    S = {"type": "object", "additionalProperties": False}
    kernel.register_tool(ToolSpec(
        "bash", "Run a shell command in your workspace (cwd). $ARTEL_SHARED is the project's shared deliverables "
        "directory. Returns exit code and output. Use it to build, test, inspect and produce evidence.",
        {**S, "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]},
        bash, roles=EXEC_ROLES))
    kernel.register_tool(ToolSpec(
        "read_file", "Read a text file. Paths: workspace-relative, or shared/<path> for project deliverables.",
        {**S, "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
         "required": ["path"]}, read_file, roles=EXEC_ROLES + ("librarian", "refiner", "manager")))
    kernel.register_tool(ToolSpec(
        "write_file", "Create or overwrite a file. Put deliverables under shared/ so teammates and reviewers see them.",
        {**S, "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        write_file, roles=("worker",)))
    kernel.register_tool(ToolSpec(
        "edit_file", "Replace one exact, unique occurrence of `old` with `new` in a file.",
        {**S, "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         "required": ["path", "old", "new"]}, edit_file, roles=("worker",)))
    kernel.register_tool(ToolSpec(
        "list_files", "List files under a directory (workspace-relative or shared/).",
        {**S, "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}, "required": []},
        list_files, roles=EXEC_ROLES + ("librarian", "refiner", "manager")))

    kernel.register_tool(ToolSpec(
        "memory_write", "Persist a durable note to your MEMORY.md (facts, decisions, lessons about this project). "
        "mode=append (default) or replace.",
        {**S, "properties": {"content": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]}},
         "required": ["content"]}, memory_write, roles=("worker",)))
