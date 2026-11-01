"""Vetting a rewritten set of task checks (pure functions).

A check is the only automated evidence a task has, so a rewrite (after an environment failure) must not weaken it.
The boundary is behaviour, not syntax: a rewritten check is accepted only if it FAILS on the task's baseline (the
state before the task started) and PASSES on the submitted result (``discrimination_problem``, fed by runs in the
gates' own sandbox and environment). ``rewrite_problem`` is a cheap pre-check in front of that (no checks, fewer
checks than before, obvious no-ops, programs that do not exist): it saves the two runs, it is not the boundary.
The reviewer is told the old and the new checks and confirms the new ones verify the same criteria.
"""

from __future__ import annotations

import re
import shlex

_SPLIT = re.compile(r"(&&|\|\||;|\||\n)")
_NOOP_WORDS = {"true", ":", "echo", "printf", "sleep", "pwd", "ls", "date", "whoami", "id", "uname"}


def _words(seg: str) -> list:
    try:
        return shlex.split(seg.strip().lstrip("({!").rstrip(")}"), posix=True)
    except ValueError:
        return seg.split()


def _noop(seg: str) -> bool:
    """A command whose only effect is printing or succeeding: it cannot verify anything."""
    w = _words(re.sub(r"\d*[<>]&\d*|\d*>>?\s*\S+", " ", seg))
    if not w:
        return True
    if w[0] in _NOOP_WORDS:
        return True
    if w[0] == "exit":
        return len(w) == 1 or w[1] == "0"
    if w[0] in ("test", "[", "[["):
        args = [a for a in w[1:] if a not in ("]", "]]")]
        return len(args) == 1 and (args[0].isdigit() and args[0] != "0" or args[0] in ("1", "true"))
    return False


def trivial(check: str) -> bool:
    """True when the check cannot fail for a reason about the work: every part is a no-op, or its last part is a
    no-op after ``;`` / ``||`` (``pytest || true``)."""
    parts = _SPLIT.split(str(check))
    segs = parts[0::2]
    ops = parts[1::2]
    if all(_noop(s) for s in segs):
        return True
    return bool(ops) and _noop(segs[-1]) and ops[-1] in (";", "||", "\n")


def rewrite_problem(old: list, new: list, absent: list) -> str:
    """Why the rewrite of ``old`` into ``new`` must be refused ('' = acceptable). ``absent``: first programs of
    ``new`` that do not resolve on the gate's PATH."""
    if not new:
        return "no checks"
    if len(new) < len(old):
        return f"fewer checks than before ({len(new)} < {len(old)})"
    bad = [c for c in new if trivial(c)]
    if bad:
        return "trivial check(s) that verify nothing: " + "; ".join(bad)
    if absent:
        return "the program(s) do not exist on this machine: " + ", ".join(absent)
    return ""


_BARE = {"ls", "cat", "wc", "stat"}
_BARE_TEST_FLAGS = {"-e", "-f", "-s", "-d", "-r"}


def _bare_segment(seg: str) -> bool:
    """Only asks whether a file exists or has content (``test -e f``, ``[ -s f ]``, ``ls``, ``cat``, ``wc``, ``stat``):
    nothing about what the work does."""
    w = _words(re.sub(r"\d*[<>]&\d*|\d*>>?\s*\S+", " ", seg))
    if not w:
        return True
    if w[0] in _BARE:
        return True
    if w[0] in ("test", "[", "[["):
        args = [a for a in w[1:] if a not in ("]", "]]")]
        return bool(args) and args[0] in _BARE_TEST_FLAGS
    return False


def bare_existence(check: str) -> bool:
    """True when every part of ``check`` is a bare existence / content-free test."""
    return all(_bare_segment(s) for s in _SPLIT.split(str(check))[0::2])


def _program(check: str):
    w = _words(_SPLIT.split(str(check))[0])
    while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
        w.pop(0)
    return w[0] if w else ""


def _artefacts(check: str) -> set:
    """What a check refers to: its non-option words after the program (paths, test ids, modules, patterns)."""
    out = set()
    for seg in _SPLIT.split(str(check))[0::2]:
        w = _words(seg)
        while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
        out |= {a[2:] if a.startswith("./") else a for a in w[1:] if not a.startswith("-") and a not in ("&", ".", "..")}
    return out


_EQUIVALENT = ({"rg", "grep", "egrep", "fgrep", "ag", "ack"}, {"python", "python3"}, {"pytest", "py.test"})


def _same_program(a: str, b: str) -> bool:
    return a == b or any(a in g and b in g for g in _EQUIVALENT)


def confirmation_reasons(old: list, new: list, absent_old: set) -> list:
    """Why the reviewer must confirm that ``new`` still covers the criteria (empty = nothing special). A new check
    replacing ``old[i]`` keeps its leading program (or an equivalent one: rg/grep, python/python3; or the program is absent
    here, which is what the rewrite fixes) and at least one artefact it referred to; none of the new checks is a bare existence test."""
    reasons = []
    for i, cmd in enumerate(new):
        if bare_existence(cmd):
            reasons.append(f"`{cmd}` only tests existence or content-free output")
        if i < len(old):
            prog, was = _program(cmd), _program(old[i])
            if was and not _same_program(prog, was) and was not in absent_old:
                reasons.append(f"`{cmd}` drops the program `{was}` of `{old[i]}`")
            refs = _artefacts(old[i])
            if refs and not refs & _artefacts(cmd):
                reasons.append(f"`{cmd}` refers to none of the artefacts of `{old[i]}`")
    return reasons


def discrimination_problem(new: list, on_baseline: list, on_result: list) -> str:
    """Why ``new`` cannot be accepted ('' = it can). ``on_baseline``: gate results (``exit``, optional ``env``) of the
    same checks run before the task; ``on_result``: the same on the submission, ``None`` where the check passed the
    baseline (not run: a guard, see ``guards``). A check must fail on the baseline and pass on the result."""
    for cmd, base, res in zip(new, on_baseline, on_result):
        if base.get("env"):
            return f"`{cmd}` cannot run on the baseline here ({base['env']}), so it cannot be shown to discriminate"
        if res is None:
            continue
        if res.get("env") or res["exit"] != 0:
            why = "cannot run here" if res.get("env") else f"exit {res['exit']}"
            return f"`{cmd}` fails on the submitted result ({why}): {str(res.get('output', ''))[:200]}"
    return ""


def guards(new: list, on_baseline: list) -> list:
    """The checks that already pass on the baseline: they guard against a regression and say nothing about the task,
    so only a reviewer's confirmation can accept them."""
    return [cmd for cmd, base in zip(new, on_baseline) if base["exit"] == 0 and not base.get("env")]
