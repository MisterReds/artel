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


def discrimination_problem(new: list, on_baseline: list, on_result: list) -> str:
    """Why ``new`` does not verify the task ('' = it does). ``on_baseline`` / ``on_result``: gate results (``exit``,
    optional ``env``) of the same checks, in order, run before the task and on its submission."""
    for cmd, base, res in zip(new, on_baseline, on_result):
        if res.get("env") or res["exit"] != 0:
            why = "cannot run here" if res.get("env") else f"exit {res['exit']}"
            return f"`{cmd}` fails on the submitted result ({why}): {str(res.get('output', ''))[:200]}"
        if base["exit"] == 0:
            return f"`{cmd}` passes on the baseline (before the task started), so it verifies nothing about the task"
        if base.get("env"):
            return f"`{cmd}` cannot run on the baseline here ({base['env']}), so it cannot be shown to discriminate"
    return ""
