"""Execution tools: shell, files, worker memory."""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from pathlib import Path

from .. import guardian, policy, sandbox
from ..agent_env import agent_env
from ..kernel import ToolSpec
from ..tooling import READONLY_ROLES, ToolContext, ToolError, window_text
from ..tools import shell_env, shell_run

PLUGIN = {"name": "artel.plugins.builtin_tools", "version": "1",
          "provides": ["tool:bash", "tool:read_file", "tool:write_file", "tool:edit_file", "tool:list_files",
                       "tool:memory_write", "tool:search"]}
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
    f.write_text(text, encoding="utf-8")
    rel = f"{SPILL_DIR}/{f.name}"
    return (f"{window_text(text, MAX_OUT)}\n\n[output too large ({nlines} lines, {nbytes} bytes); "
            f"full output saved to {rel}. Use read_file with path={rel} and offset/limit to view parts of it.]")


def _bound(ctx: ToolContext, text: str, tool: str) -> str:
    """Spill oversized output; otherwise apply the plain char clip."""
    spilled = spill_output(ctx, text, tool)
    return _clip(text) if spilled == text else spilled


def bash(ctx: ToolContext, command: str, timeout: int = 0) -> str:
    if ctx.state.get("guardian") == "blocking":
        denial = guardian.blocking_error(command)
        if denial:
            ctx.project.log.append("guardian/denied", task_id=(ctx.task or {}).get("id"), command=command[:500])
            raise ToolError(denial)
    try:
        verdict = policy.evaluate(command, ctx.project.root)
    except policy.PolicyError as exc:
        raise ToolError(f"command policy is invalid, bash disabled until fixed: {exc}")
    if verdict.decision == "forbidden":
        raise ToolError(f"command blocked by policy ({verdict.rule.id}): {verdict.justification or 'forbidden'}; command: {command}")
    if verdict.decision == "prompt":
        ctx.terminal = policy.prompt_terminal(verdict, command)
        return f"command NOT run: policy requires human approval ({verdict.justification}); task sent to a person"
    timeout = int(timeout or ctx.kernel.config.get("tool_timeout_s", 300))
    env = agent_env("native", {"ARTEL_SHARED": str(ctx.shared_dir), "ARTEL_WORKSPACE": str(ctx.workspace)},
                    ctx.kernel.config)
    # OS sandbox (W2-sandbox): writes only to the deliverables this agent works on, its workspace and temp
    # judges (read-only roles) write only to their own workspace; run_agent's judge_guard undoes the rest
    judge = ctx.role in READONLY_ROLES or not ctx.grants()["write"]
    writable = [ctx.workspace] if judge else [ctx.shared_dir, ctx.workspace]
    config = ctx.kernel.config
    run = sandbox.plan(config, command, cwd=ctx.workspace, writable=writable, project_root=ctx.project.root)
    if run.note and not ctx.state.get("sandbox_noted"):
        ctx.state["sandbox_noted"] = True
        ctx.project.log.append("sandbox/unavailable", actor=ctx.actor, note=run.note)
    if sandbox.refusal(run, config):
        raise ToolError(f"command NOT run: {run.error}")
    try:
        proc = subprocess.run(run.argv, cwd=ctx.workspace, capture_output=True, text=True, timeout=timeout,
                              env=shell_env(env), encoding="utf-8", errors="replace")
        failed = sandbox.start_failure(run, proc.returncode, proc.stderr)
        if failed:  # fail closed: the sandbox exists but did not start; never fall back unless the owner said so
            ctx.project.log.append("sandbox/start_failed", actor=ctx.actor, error=failed)
            if sandbox.settings(config)["require"]:
                raise ToolError(f"command NOT run: {failed}")
            run = sandbox.plan(config, command, cwd=ctx.workspace, writable=writable, mode="off")
            run.note = f"{failed.split(';')[0]}; sandbox.require is off, so the command ran WITHOUT a sandbox"
            proc = subprocess.run(run.argv, cwd=ctx.workspace, capture_output=True, text=True, timeout=timeout,
                                  env=shell_env(env), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"timed out after {timeout}s; partial output:\n{_clip(str(exc.stdout or ''), 4000)}")
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    note = f"[note: {run.note}]\n" if run.note else ""
    result = f"[exit {proc.returncode}]\n{note}{_bound(ctx, out, 'bash')}"
    ctx.outputs.append(f"$ {command}\n{window_text(result, 3000)}")
    return result


def read_file(ctx: ToolContext, path: str, offset: int = 0, limit: int = 0) -> str:
    p = ctx.resolve(path)
    if not p.is_file():
        raise ToolError(f"{path} is not a file")
    lines = p.read_text(errors="replace", encoding="utf-8").splitlines()
    end = offset + limit if limit else len(lines)
    ctx.record_stamp(p)
    body = "\n".join(f"{i + 1:>5}\t{ln}" for i, ln in enumerate(lines[offset:end], start=offset))
    return _bound(ctx, body, "read_file") or "(empty file)"


def _diagnostics(path: Path, timeout: float = 10.0) -> str:
    """Post-write checks for .py files: compile() syntax check plus ruff when on PATH.

    Idea (feedback on the write result itself) borrowed from OpenCode's write tool /
    LSP diagnostics (MIT, anomalyco/opencode). Never raises; returns "" when clean.
    """
    try:
        if path.suffix != ".py":
            return ""
        errors: list[str] = []
        try:
            compile(path.read_text(errors="replace", encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            errors.append(f"SyntaxError: {exc.msg} ({path.name}:{exc.lineno}:{exc.offset or 0})")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        ruff = shutil.which("ruff")
        if ruff:
            try:
                proc = subprocess.run([ruff, "check", "--quiet", "--output-format", "concise", str(path)],
                                      capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
                out = (proc.stdout or "").strip()
                if proc.returncode == 1 and out:
                    errors.append(out)
            except Exception:
                pass
        if not errors:
            return ""
        return "\n\nDiagnostics:\n" + _clip("\n".join(errors), 4000)
    except Exception:
        return ""


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = ctx.resolve(path, write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    ctx.record_stamp(p)
    ctx.edited_files.append(ctx.display(p))
    ctx.project.log.append("file/written", actor=ctx.actor, path=ctx.display(p), bytes=len(content.encode()))
    return f"wrote {len(content)} chars to {ctx.display(p)}" + _diagnostics(p)


# Fuzzy edit fallbacks (line-trimmed, block-anchor with Levenshtein similarity) adapted from
# OpenCode packages/opencode/src/tool/edit.ts -- MIT License, Copyright (c) 2025 opencode (anomalyco).
# Permission is hereby granted, free of charge, to use, copy, modify and distribute this software
# subject to inclusion of the above copyright notice and this permission notice.
BLOCK_ANCHOR_THRESHOLD = 0.65


def _levenshtein(a: str, b: str) -> int:
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similarity(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1 - _levenshtein(a, b) / longest


def _line_trimmed_spans(lines: list[str], search: list[str]) -> list[tuple[int, int]]:
    want = [s.strip() for s in search]
    stripped = [ln.strip() for ln in lines]
    return [(i, i + len(want) - 1) for i in range(len(lines) - len(want) + 1)
            if stripped[i:i + len(want)] == want]


def _block_anchor_spans(lines: list[str], search: list[str]) -> list[tuple[int, int]]:
    if len(search) < 3:
        return []
    first, last = search[0].strip(), search[-1].strip()
    middle = [s.strip() for s in search[1:-1]]
    found = []
    for i, ln in enumerate(lines):
        if ln.strip() != first:
            continue
        for j in range(i + 2, len(lines)):
            if lines[j].strip() != last:
                continue
            got = [x.strip() for x in lines[i + 1:j]]
            pairs = max(len(middle), len(got))
            sim = sum(_similarity(m, g) for m, g in zip(middle, got)) / pairs
            if sim >= BLOCK_ANCHOR_THRESHOLD:
                found.append((sim, i, j))
            break
    return [(i, j) for _, i, j in found]


def _fuzzy_replace(text: str, old: str, new: str) -> str:
    """Apply old->new when the exact match failed; refuse ambiguity, raise if nothing matches."""
    lines = text.splitlines(keepends=True)
    bare = [ln.rstrip("\r\n") for ln in lines]
    search = old.split("\n")
    keep_nl = old.endswith("\n")
    if keep_nl:
        search.pop()
    search = [s.rstrip("\r") for s in search]
    if not search or not any(s.strip() for s in search):
        raise ToolError("old text must match exactly once (matched 0)")
    for tier, finder in (("line-trimmed", _line_trimmed_spans), ("block-anchor", _block_anchor_spans)):
        spans = sorted(set(finder(bare, search)))
        if len(spans) > 1:
            where = ", ".join(f"{a + 1}-{b + 1}" for a, b in spans)
            raise ToolError(f"old text has no exact match and {len(spans)} {tier} matches (lines {where}); "
                            "add more surrounding context to make it unique")
        if spans:
            a, b = spans[0]
            tail = lines[b][len(bare[b]):] if keep_nl else ""
            start = sum(len(x) for x in lines[:a])
            end = sum(len(x) for x in lines[:b]) + len(bare[b]) + len(tail)
            return text[:start] + new + text[end:]
    raise ToolError("old text must match exactly once (matched 0)")


def edit_file(ctx: ToolContext, path: str, old: str, new: str) -> str:
    p = ctx.resolve(path, write=True)
    if not p.is_file():
        raise ToolError(f"{path} is not a file")
    if ctx.is_stale(p):
        raise ToolError(f"{path} changed on disk since you last read it; re-read the file with read_file before editing it")
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 1:
        updated = text.replace(old, new)
    elif n == 0:
        updated = _fuzzy_replace(text, old, new)
    else:
        raise ToolError(f"old text must match exactly once (matched {n})")
    p.write_text(updated, encoding="utf-8")
    ctx.record_stamp(p)
    ctx.edited_files.append(ctx.display(p))
    ctx.project.log.append("file/edited", actor=ctx.actor, path=ctx.display(p))
    return f"edited {ctx.display(p)}" + _diagnostics(p)


def list_files(ctx: ToolContext, path: str = ".", depth: int = 3) -> str:
    base = ctx.resolve(path)
    if not base.exists():
        raise ToolError(f"{path} does not exist")
    out = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if len(rel.parts) > depth or any(part.startswith(".") for part in rel.parts):
            continue
        out.append(f"{rel.as_posix()}{'/' if p.is_dir() else ''}" + ("" if p.is_dir() else f"  ({p.stat().st_size} B)"))
        if len(out) >= 500:
            out.append("…truncated")
            break
    return _bound(ctx, "\n".join(out), "list_files") or "(empty)"


def search(ctx: ToolContext, query: str, path: str = ".", kind: str = "code", top: int = 8) -> str:
    """Find where something is by meaning (and by exact words): code, docs and deliverables under ``path``."""
    from ..retrieval import search_files

    base = ctx.resolve(path)
    if not base.is_dir():
        raise ToolError(f"{path} is not a directory")
    task = kind if kind in ("code", "search", "qa") else "code"
    hits = search_files(base, query, ctx.kernel.config, top=max(1, min(int(top), 20)), task=task)
    ctx.project.log.append("search/query", actor=ctx.actor, query=query[:300], path=path, hits=len(hits),
                           how=hits[0]["how"] if hits else "")
    if not hits:
        return "nothing found"
    out = [f"{ctx.display(base / h['path'])}:{h['start']}-{h['end']}\n{h['text'][:700]}" for h in hits]
    how = "by meaning and keywords" if hits[0]["how"] == "hybrid" else "by keywords (no embedding model)"
    return _bound(ctx, f"{len(hits)} matches, {how}:\n\n" + "\n\n---\n".join(out), "search")


def memory_write(ctx: ToolContext, content: str, mode: str = "append") -> str:
    if ctx.persona is None:
        raise ToolError("only workers keep a MEMORY.md")
    p = ctx.project.worker_dir(ctx.persona.name) / "MEMORY.md"
    if mode == "replace":
        p.write_text(content if content.startswith("# MEMORY") else "# MEMORY\n\n" + content, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(content.rstrip() + "\n")
    ctx.project.log.append("worker/memory", worker=ctx.persona.name, mode=mode, content=content[:2000])
    return f"MEMORY.md {mode}d"


def setup(kernel):
    from ..runtimes import mcp_client

    S = {"type": "object", "additionalProperties": False}
    kernel.register_tool(ToolSpec(
        "bash", "Run a shell command in your workspace (cwd). $ARTEL_SHARED is the project's shared deliverables "
        "directory. Returns exit code and output. Use it to build, test, inspect and produce evidence.",
        {**S, "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]},
        bash, roles=EXEC_ROLES))
    kernel.register_tool(ToolSpec(
        "read_file", "Read a text file. Paths: workspace-relative, or shared/<path> for project deliverables.",
        {**S, "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
         "required": ["path"]}, read_file, roles=EXEC_ROLES + ("librarian", "refiner", "manager", "doctor")))
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
        list_files, roles=EXEC_ROLES + ("librarian", "refiner", "manager", "doctor")))
    kernel.register_tool(ToolSpec(
        "search", "Find code, docs or results by what they do or mean, not only by exact words (e.g. 'where are "
        "provider limits handled'); returns file:line ranges with snippets. For code, phrase the query in the code's language (usually English). kind: code | search | qa.",
        {**S, "properties": {"query": {"type": "string"}, "path": {"type": "string"},
                             "kind": {"type": "string", "enum": ["code", "search", "qa"]},
                             "top": {"type": "integer"}}, "required": ["query"]},
        search, roles=EXEC_ROLES + ("librarian", "refiner", "manager", "auditor", "doctor")))

    kernel.register_tool(ToolSpec(
        "memory_write", "Persist a durable note to your MEMORY.md (facts, decisions, lessons about this project). "
        "mode=append (default) or replace.",
        {**S, "properties": {"content": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]}},
         "required": ["content"]}, memory_write, roles=("worker",)))
    # external MCP servers from artel.yaml (mcp_servers:), as mcp__<server>__<tool> tools for the allowed roles
    return mcp_client.register_tools(kernel)
