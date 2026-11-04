"""Mutation gate on changed files, driven by mutmut (imported by changed_gates.py).

mutmut mutates only the changed files (``only_mutate``) and only lines that tests execute
(``mutate_only_covered_lines``), within a wall-clock budget. Survivors whose mutated line is a CHANGED line are
listed; the gate fails on every survivor that is not explained in scripts/mutation_explained.txt as
``path:line: reason``. If the budget runs out the verdict covers the mutants tried so far and says how many were not.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

EXPLAINED = "scripts/mutation_explained.txt"
SURVIVOR = re.compile(r"^\s+(?P<name>\S+): (?P<status>survived|no tests|suspicious|timeout|not checked)\s*$")


def split_name(mutant_name: str) -> tuple[str, str | None, str]:
    """artel.pkg.mod.x_func__mutmut_3 -> ('artel/pkg/mod.py', None, 'func');
    artel.pkg.mod.xǁClassǁmeth__mutmut_1 -> ('artel/pkg/mod.py', 'Class', 'meth')."""
    module, _, rest = mutant_name.rpartition(".")
    rest = rest.rsplit("__mutmut_", 1)[0]
    path = module.replace(".", "/") + ".py"
    if rest.startswith("xǁ"):
        _, cls, meth = rest.split("ǁ", 2)
        return path, cls, meth
    return path, None, rest[2:] if rest.startswith("x_") else rest


def function_range(source: str, cls: str | None, func: str) -> tuple[int, int] | None:
    """First (decorators included) and last line of the function or method, or None if it cannot be found."""
    tree = ast.parse(source)
    scope: list[ast.stmt] = tree.body
    if cls:
        owner = next((n for n in scope if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if owner is None:
            return None
        scope = owner.body
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return first, node.end_lineno or node.lineno
    return None


def mutated_lines(diff: str, source: str, cls: str | None, func: str) -> set[int]:
    """Absolute lines of the file that the mutant changed.

    mutmut prints the diff against the function snippet, so its line numbers are relative to the function; the
    removed lines are located by their text inside the function's range in the real file instead."""
    span = function_range(source, cls, func)
    if span is None:
        return set()
    first, last = span
    lines = source.splitlines()
    removed = [r[1:].strip() for r in diff.splitlines() if r.startswith("-") and not r.startswith("---")]
    hit = {n for n in range(first, last + 1) if lines[n - 1].strip() in removed and removed}
    return hit or {first}  # a pure insertion: attribute it to the function as a whole (its first line)


def tests_for(root: Path, files: list[str]) -> list[str]:
    """Test files that mention one of the mutated modules (by module stem, or package name for __init__)."""
    words = {Path(f).parent.name if Path(f).stem == "__init__" else Path(f).stem for f in files}
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(words)) + r")\b")
    return sorted(
        f"tests/{p.name}" for p in (root / "tests").glob("test_*.py") if pattern.search(p.read_text(encoding="utf-8"))
    )


def run_mutation(root: Path, changed: dict[str, set[int]], *, budget_min: float, require: bool) -> int:
    from changed_gates import parse_explained, skip

    class _A:  # the tiny part of argparse.Namespace that skip() reads
        pass

    _A.require = require  # type: ignore[attr-defined]
    files = sorted(changed)
    if not files:
        print("mutation: no changed code under artel/")
        return 0
    if shutil.which("mutmut") is None:
        return skip("mutation", "mutmut is not installed (pip install -e '.[gates]')", _A)
    explained = parse_explained((root / EXPLAINED).read_text() if (root / EXPLAINED).exists() else "")

    tests = tests_for(root, files)
    print(
        f"mutation: tests selected for {len(files)} file(s): "
        + (", ".join(tests) if tests else "all (none refer to them)")
    )
    cfg = root / "setup.cfg"
    if cfg.exists():
        raise SystemExit("mutation: setup.cfg exists; the gate writes its own temporary mutmut config there")
    cfg.write_text(
        "[mutmut]\nsource_paths=artel\nmutate_only_covered_lines=true\nonly_mutate=\n"
        + "".join(f"    {f}\n" for f in files)
        + "pytest_add_cli_args=\n    -p\n    no:randomly\n    -q\n"
        + ("pytest_add_cli_args_test_selection=\n" + "".join(f"    {t}\n" for t in tests) if tests else ""),
        encoding="utf-8",
    )
    shutil.rmtree(root / "mutants", ignore_errors=True)
    start = time.monotonic()
    try:
        log = open(root / "mutmut-run.log", "w", encoding="utf-8")  # noqa: SIM115 - closed in finally
        proc = subprocess.Popen(
            ["mutmut", "run"], cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            proc.wait(timeout=budget_min * 60)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        if not timed_out and proc.returncode:
            log.flush()
            print(f"mutation: mutmut run failed (exit {proc.returncode}); tail of mutmut-run.log:")
            print(
                "\n".join(
                    (root / "mutmut-run.log")
                    .read_text(encoding="utf-8", errors="replace")
                    .replace("\r", "\n")
                    .splitlines()[-25:]
                )
            )
            return 1
        listing = subprocess.run(["mutmut", "results"], cwd=root, capture_output=True, text=True, check=False).stdout
        survivors = [m.groupdict() for m in map(SURVIVOR.match, listing.splitlines()) if m]
        unexplained, explained_hits, unchecked = [], 0, 0
        for s in survivors:
            if s["status"] == "not checked":
                unchecked += 1
                continue
            path, cls, func = split_name(s["name"])
            diff = subprocess.run(
                ["mutmut", "show", s["name"]], cwd=root, capture_output=True, text=True, check=False
            ).stdout
            source = (root / path).read_text(encoding="utf-8")
            hit = sorted(mutated_lines(diff, source, cls, func) & changed.get(path, set()))
            if not hit:
                continue  # the function has changed lines, but this mutant sits on an unchanged one
            if any((path, n) in explained for n in hit):
                explained_hits += 1
            else:
                unexplained.append((path, hit[0], s["name"], s["status"]))
    finally:
        log.close()
        cfg.unlink(missing_ok=True)
        shutil.rmtree(root / "mutants", ignore_errors=True)

    minutes = (time.monotonic() - start) / 60
    print(
        f"mutation: {len(files)} changed file(s), {minutes:.1f} min"
        + (f"; BUDGET of {budget_min:g} min exhausted, {unchecked} mutant(s) not tried" if timed_out else "")
    )
    print(
        f"mutation: {len(unexplained)} unexplained survivor(s) on changed lines, "
        f"{explained_hits} explained in {EXPLAINED}"
    )
    for path, line, name, status in sorted(unexplained):
        print(f"  {path}:{line}: {status}  ({name})")
    if unexplained:
        print(f"mutation: kill each with a behaviour test, or explain it in {EXPLAINED} as 'path:line: reason'.")
        return 1
    print("mutation: ok")
    return 0


if __name__ == "__main__":
    sys.exit("run through changed_gates.py mutation")
