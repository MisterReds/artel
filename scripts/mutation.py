"""Mutation gate on changed files, driven by mutmut (imported by changed_gates.py).

mutmut mutates only the changed files (``only_mutate``) and only lines that tests execute
(``mutate_only_covered_lines``), within a wall-clock budget. Survivors whose mutated line is a CHANGED line are
listed; the gate fails on every survivor that is not explained in scripts/mutation_explained.txt as
``path:line: reason``. If the budget runs out the verdict covers the mutants tried so far and says how many were not.
"""

from __future__ import annotations

import ast
import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

EXPLAINED = "scripts/mutation_explained.txt"
# Tests that inspect the SOURCE or the SHAPE of the code under mutation. mutmut rewrites that source (it injects
# ``from mutmut.mutation import trampoline`` into every mutated module), so these tests cannot pass in a mutation run
# although the code is fine; they guard structure, not behaviour, and run in the normal test job.
DESELECT = (
    "tests/test_ports.py::test_import_boundaries",
    "tests/test_ports.py::test_no_vendor_names_in_packages",
    "tests/test_ports.py::test_fakes_satisfy_protocols",  # runtime-protocol check of the trampolined fakes
)
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


def find_function(tree: ast.AST, cls: str | None, func: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    scope: list[ast.stmt] = getattr(tree, "body", [])
    if cls:
        owner = next((n for n in scope if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if owner is None:
            return None
        scope = owner.body
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return node
    return None


def units_touching(source: str, lines: set[int]) -> set[tuple[str | None, str]]:
    """(class or None, function) for each module-level function and class method that contains a changed line.

    A nested function belongs to its enclosing unit: mutmut mutates (and names mutants after) the outer one."""
    units: set[tuple[str | None, str]] = set()

    def visit(body: list[ast.stmt], cls: str | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                if any(first <= n <= (node.end_lineno or node.lineno) for n in lines):
                    units.add((cls, node.name))
            elif isinstance(node, ast.ClassDef) and cls is None:
                visit(node.body, node.name)

    visit(ast.parse(source).body, None)
    return units


def mutant_glob(path: str, cls: str | None, func: str) -> str:
    module = path[:-3].replace("/", ".")
    return f"{module}.xǁ{cls}ǁ{func}__mutmut_*" if cls else f"{module}.x_{func}__mutmut_*"


def mutated_lines(original: str, mutants_source: str, mutant_name: str) -> set[int]:
    """Lines of the real file that a mutant changes.

    mutmut's output file holds, next to each function, a copy of the original (``..__mutmut_orig``) and every mutant
    (``..__mutmut_N``) with the same layout; the two are compared line by line and the differing positions are
    mapped onto the real function's lines."""
    _, cls, func = split_name(mutant_name)
    real = find_function(ast.parse(original), cls, func)
    if real is None:
        return set()
    suffix = mutant_name.rpartition(".")[2].rpartition("__mutmut_")[2]
    wanted = {suffix, "orig"}
    texts: dict[str, list[str]] = {}
    mutants_lines = mutants_source.splitlines()
    for node in ast.walk(ast.parse(mutants_source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "__mutmut_" in node.name:
            tag = node.name.rpartition("__mutmut_")[2]
            if tag in wanted and node.name.startswith(mutant_name.rpartition(".")[2].rpartition("__mutmut_")[0]):
                texts[tag] = [ln.strip() for ln in mutants_lines[node.body[0].lineno - 1 : node.end_lineno]]
    if set(texts) != wanted:
        return {real.lineno}
    first = real.body[0].lineno  # compare bodies: the def lines differ by name
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(a=texts["orig"], b=texts[suffix], autojunk=False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed.update(range(first + i1, first + max(i2, i1 + 1)))  # an insertion marks the line after it
    return changed or {real.lineno}


def tests_for(root: Path, files: list[str]) -> list[str]:
    """Test files that refer to a mutated module: its dotted name, its package (below ``artel``), or the
    ``from artel import <module>`` form. A narrow selection keeps mutmut's baseline run inside the time budget."""
    patterns = []
    for f in files:
        dotted = f[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        parent = dotted.rpartition(".")[0]
        patterns.append(rf"\b{re.escape(dotted)}\b")
        if parent and parent != "artel":
            patterns.append(rf"\b{re.escape(parent)}\b")
        if parent == "artel":
            patterns.append(rf"from artel import[^\n]*\b{re.escape(dotted.rpartition('.')[2])}\b")
    regex = re.compile("|".join(patterns))
    tests = (root / "tests").glob("test_*.py")
    return sorted(f"tests/{p.name}" for p in tests if regex.search(p.read_text(encoding="utf-8")))


def repo_data(root: Path) -> list[str]:
    """Top-level tracked entries besides the package and the tests (which mutmut copies itself): the data the tests
    read from the repo root (docs/, bench/, install.sh, ...) must exist in mutants/ too, or the clean run fails."""
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=False).stdout
    tops = {line.split("/", 1)[0] + ("/" if "/" in line else "") for line in out.splitlines() if line}
    data = sorted(t for t in tops if t not in ("artel/", "tests/", "setup.cfg") and not t.startswith("."))
    return data or ["scripts/"]


def write_config(root: Path, files: list[str], tests: list[str]) -> Path:
    cfg = root / "setup.cfg"
    if cfg.exists():
        raise SystemExit("mutation: setup.cfg exists; the gate writes its own temporary mutmut config there")
    cfg.write_text(
        "[mutmut]\nsource_paths=artel\nalso_copy=\n"
        + "".join(f"    {d}\n" for d in repo_data(root))
        + "mutate_only_covered_lines=true\nonly_mutate=\n"
        + "".join(f"    {f}\n" for f in files)
        + "pytest_add_cli_args=\n    -p\n    no:randomly\n    -q\n"
        + "".join(f"    --deselect\n    {d}\n" for d in DESELECT)
        + ("pytest_add_cli_args_test_selection=\n" + "".join(f"    {t}\n" for t in tests) if tests else ""),
        encoding="utf-8",
    )
    return cfg


def run_mutmut(root: Path, globs: list[str], budget_min: float) -> tuple[bool, int]:
    """Run mutmut on the selected mutants; (timed out, exit code). On timeout it is interrupted so it saves results."""
    with open(root / "mutmut-run.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            ["mutmut", "run", *globs], cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            return False, proc.wait(timeout=budget_min * 60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            return True, proc.returncode


def log_tail(root: Path, lines: int = 25) -> str:
    text = (root / "mutmut-run.log").read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.replace("\r", "\n").splitlines()[-lines:])


def classify(root: Path, changed: dict[str, set[int]], explained: set[tuple[str, int]]):
    """Split mutmut's results into (unexplained survivors, number explained, number not tried), on changed lines."""
    listing = subprocess.run(["mutmut", "results"], cwd=root, capture_output=True, text=True, check=False).stdout
    sources: dict[str, tuple[str, str]] = {}
    unexplained, explained_hits, untried = [], 0, 0
    for m in filter(None, map(SURVIVOR.match, listing.splitlines())):
        name, status = m.group("name"), m.group("status")
        path = split_name(name)[0]
        if path not in changed:
            continue
        if path not in sources:
            sources[path] = ((root / path).read_text("utf-8"), (root / "mutants" / path).read_text("utf-8"))
        hit = sorted(mutated_lines(sources[path][0], sources[path][1], name) & changed[path])
        if not hit:
            continue  # the function has changed lines, but this mutant sits on an unchanged one
        if status == "not checked":
            untried += 1
        elif any((path, n) in explained for n in hit):
            explained_hits += 1
        else:
            unexplained.append((path, hit[0], name, status))
    return unexplained, explained_hits, untried


def run_mutation(root: Path, changed: dict[str, set[int]], *, budget_min: float, require: bool) -> int:
    from changed_gates import parse_explained, skip

    class _Args:  # the part of argparse.Namespace that skip() reads
        pass

    _Args.require = require  # type: ignore[attr-defined]
    files = sorted(changed)
    if not files:
        print("mutation: no changed code under artel/")
        return 0
    if shutil.which("mutmut") is None:
        return skip("mutation", "mutmut is not installed (pip install -e '.[gates]')", _Args)
    globs = [
        mutant_glob(path, cls, func)
        for path in files
        for cls, func in sorted(units_touching((root / path).read_text("utf-8"), changed[path]), key=str)
    ]
    if not globs:
        print("mutation: the changed lines are outside any function")
        return 0
    explained = parse_explained((root / EXPLAINED).read_text() if (root / EXPLAINED).exists() else "")
    tests = tests_for(root, files)
    print(f"mutation: {len(globs)} changed function(s) in {len(files)} file(s); tests: " + (", ".join(tests) or "all"))
    cfg = write_config(root, files, tests)
    shutil.rmtree(root / "mutants", ignore_errors=True)
    start = time.monotonic()
    try:
        timed_out, code = run_mutmut(root, globs, budget_min)
        if not timed_out and code:
            print(f"mutation: mutmut run failed (exit {code}); tail of mutmut-run.log:\n{log_tail(root)}")
            return 1
        unexplained, explained_hits, untried = classify(root, changed, explained)
    finally:
        cfg.unlink(missing_ok=True)
        shutil.rmtree(root / "mutants", ignore_errors=True)

    minutes = (time.monotonic() - start) / 60
    print(f"mutation: {minutes:.1f} min" + (f"; BUDGET of {budget_min:g} min exhausted" if timed_out else ""))
    if untried:
        print(f"mutation: WARNING {untried} mutant(s) on changed lines were not tried within the budget")
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
