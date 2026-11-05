#!/usr/bin/env python3
"""Gates on CHANGED code only (CLAUDE.md Engineering rules, rule 9; docs/SPEC.md §11).

    changed_gates.py lines                       # print the changed lines per file
    changed_gates.py crap --coverage coverage.json
    changed_gates.py dup
    changed_gates.py mutation [--budget-min 10]

"Changed" = lines of ``artel/**/*.py`` that differ from the merge base with master (``--base`` overrides). Commits
listed in scripts/gates_ignore_revs.txt (purely mechanical rewrites such as a formatter or ``ruff --fix`` run) are
skipped through ``git blame --ignore-revs-file`` so a mechanical commit does not drag every touched function in.
A tool that is not installed is skipped with a clear message and exit code 0 (``--require`` makes it an error).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "artel/"
IGNORE_REVS = "scripts/gates_ignore_revs.txt"
CRAP_LIMIT = 10
MIN_SIMILARITY = 8


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if check and proc.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def merge_base(explicit: str | None = None) -> str:
    if explicit:
        return git("rev-parse", explicit).strip()
    head = git("rev-parse", "HEAD").strip()
    for ref in ("origin/master", "master", "origin/main", "main"):
        if subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=ROOT, capture_output=True).returncode:
            continue
        base = git("merge-base", "HEAD", ref).strip()
        if base != head:
            return base
        # HEAD is the tip of the default branch itself (a push to it): check the last commit only
        parent = git("rev-parse", "-q", "--verify", "HEAD~1", check=False).strip()
        return parent or head
    raise SystemExit("no master/main ref to take the merge base from (fetch it: git fetch origin master)")


def changed_lines(base: str) -> dict[str, set[int]]:
    """{path: line numbers} of in-scope Python files changed since ``base`` (mechanical commits excluded)."""
    names = git("diff", "--name-only", "--diff-filter=AM", f"{base}..HEAD", "--", SCOPE).split()
    ignore = ROOT / IGNORE_REVS
    result: dict[str, set[int]] = {}
    for name in (n for n in names if n.endswith(".py")):
        args = ["blame", "-s", "--abbrev=40"]
        if ignore.exists():
            args += ["--ignore-revs-file", str(ignore)]
        out = git(*args, f"{base}..HEAD", "--", name, check=False)
        lines = set()
        for raw in out.splitlines():
            m = re.match(r"^(\^?)([0-9a-f]{39,40})(?:\s+\S+)?\s+(\d+)\)", raw)
            if m and not m.group(1):
                lines.add(int(m.group(3)))
        if lines:
            result[name] = lines
    return result


# ---------------------------------------------------------------- CRAP


def functions_of(source: str):
    """(qualified name, first line, last line, cyclomatic complexity) for each function, method and closure."""
    from radon.complexity import cc_visit

    out = []

    def add(block, prefix=""):
        name = f"{prefix}{block.name}"
        out.append((name, block.lineno, block.endline, block.complexity))
        for inner in getattr(block, "closures", []):
            add(inner, name + ".")

    for block in cc_visit(source):
        if hasattr(block, "methods"):
            continue  # a class: radon lists its methods as functions of their own, with ``classname`` set
        add(block, f"{block.classname}." if getattr(block, "classname", None) else "")
    return out


def crap_score(complexity: int, coverage: float) -> float:
    return complexity**2 * (1 - coverage) ** 3 + complexity


def function_coverage(first: int, last: int, executed: set[int], missing: set[int]) -> float:
    statements = {n for n in executed | missing if first <= n <= last}
    return 1.0 if not statements else len(statements & executed) / len(statements)


def crap_report(
    changed: dict[str, set[int]], coverage: dict, sources: dict[str, str]
) -> list[tuple[str, str, float, int, float]]:
    rows = []
    for path, lines in changed.items():
        data = coverage.get("files", {}).get(path)
        executed, missing = (
            set(data["executed_lines"]) if data else set(),
            set(data["missing_lines"]) if data else set(),
        )
        for name, first, last, comp in functions_of(sources[path]):
            if not any(first <= n <= last for n in lines):
                continue
            cov = function_coverage(first, last, executed, missing)
            if data is None:
                cov = 0.0
            rows.append((path, f"{name}:{first}", crap_score(comp, cov), comp, cov))
    return rows


def cmd_crap(args) -> int:
    try:
        import radon  # noqa: F401
    except ImportError:
        return skip("crap", "radon is not installed (pip install -e '.[gates]')", args)
    changed = changed_lines(merge_base(args.base))
    if not changed:
        print("crap: no changed code under artel/")
        return 0
    cov_path = Path(args.coverage)
    if not cov_path.exists():
        return skip("crap", f"{cov_path} is missing (run: coverage run --branch -m pytest && coverage json)", args)
    coverage = json.loads(cov_path.read_text())
    sources = {p: (ROOT / p).read_text(encoding="utf-8") for p in changed}
    rows = crap_report(changed, coverage, sources)
    bad = [r for r in rows if r[2] >= CRAP_LIMIT]
    print(f"crap: {len(rows)} changed function(s), limit {CRAP_LIMIT}")
    for path, name, score, comp, cov in sorted(bad, key=lambda r: -r[2]):
        print(f"  CRAP {score:7.1f}  {path}:{name}  complexity {comp}, coverage {cov:.0%}")
    if bad:
        print("crap: lower the complexity (split the function) or cover it with behaviour tests.")
        return 1
    print("crap: ok")
    return 0


# ---------------------------------------------------------------- duplicate code


def parse_duplicates(output: str) -> list[list[tuple[str, int, int]]]:
    """Blocks of similar lines: each block is a list of (path, first line, last line)."""
    blocks, current = [], []
    for line in output.splitlines():
        if "R0801" in line:
            if current:
                blocks.append(current)
            current = []
        m = re.match(r"^==([\w.]+):\[(\d+):(\d+)\]", line)
        if m:
            current.append((m.group(1).replace(".", "/") + ".py", int(m.group(2)), int(m.group(3))))
    if current:
        blocks.append(current)
    return blocks


def duplicates_touching(blocks, changed: dict[str, set[int]]):
    return [
        block
        for block in blocks
        if any(any(first <= n < last for n in changed.get(path, ())) for path, first, last in block)
    ]


def cmd_dup(args) -> int:
    changed = changed_lines(merge_base(args.base))
    if not changed:
        print("dup: no changed code under artel/")
        return 0
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pylint",
            "--disable=all",
            "--enable=duplicate-code",
            f"--min-similarity-lines={MIN_SIMILARITY}",
            "--ignore-imports=y",
            "--score=n",
            "artel",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if "No module named pylint" in proc.stderr:
        return skip("dup", "pylint is not installed (pip install -e '.[gates]')", args)
    hits = duplicates_touching(parse_duplicates(proc.stdout), changed)
    print(f"dup: {len(hits)} duplicated block(s) of >= {MIN_SIMILARITY} lines touching changed code")
    for block in hits:
        print("  " + "  <->  ".join(f"{p}:{a}-{b}" for p, a, b in block))
    if hits:
        print("dup: extract the shared code instead of copying it.")
        return 1
    print("dup: ok")
    return 0


# ---------------------------------------------------------------- mutation (mutmut)


def parse_explained(text: str) -> set[tuple[str, int]]:
    out = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+?):(\d+):\s*(\S.*)$", line)
        if not m:
            raise SystemExit(f"scripts/mutation_explained.txt: malformed line (want 'file:line: reason'): {raw!r}")
        out.add((m.group(1), int(m.group(2))))
    return out


def cmd_mutation(args) -> int:
    from mutation import run_mutation  # sibling module

    return run_mutation(ROOT, changed_lines(merge_base(args.base)), budget_min=args.budget_min, require=args.require)


def skip(gate: str, why: str, args) -> int:
    print(f"{gate}: SKIPPED - {why}")
    return 1 if args.require else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gate", choices=["lines", "crap", "dup", "mutation"])
    ap.add_argument("--base", help="commit to diff against (default: merge base with master)")
    ap.add_argument("--coverage", default="coverage.json")
    ap.add_argument("--budget-min", type=float, default=10.0)
    ap.add_argument("--require", action="store_true", help="a missing tool is an error, not a skip")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if args.gate == "lines":
        for path, lines in sorted(changed_lines(merge_base(args.base)).items()):
            print(f"{path}: {len(lines)} changed line(s)")
        return 0
    return {"crap": cmd_crap, "dup": cmd_dup, "mutation": cmd_mutation}[args.gate](args)


if __name__ == "__main__":
    sys.exit(main())
