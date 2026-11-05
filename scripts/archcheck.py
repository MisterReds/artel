#!/usr/bin/env python3
"""archcheck: the architecture rules of docs/TARGET_ARCHITECTURE.md §10 as a CI gate (stdlib ``ast`` only).

Checks, over every ``*.py`` of the package:

* cycles     import cycles between the package's modules (module-level AND function-level imports); every import edge
             that lies on a cycle is a violation, reported with the edge list of its strongly connected component;
* globals    module-level mutable containers (dict/list/set literals, comprehensions or constructors) that are mutated
             somewhere in the module, and every ``global`` statement;
* vendor     vendor/model names (identifiers and string literals, docstrings excluded) outside VENDOR_ALLOWED;
* purity     ``contracts`` and ``ports`` import only the standard library and the packages PURE_IMPORTS allows.

Existing debt lives in a committed baseline (scripts/archcheck_baseline.json), a ratchet (see scripts/ratchet.py):
a new violation fails, and so does a baseline entry that no longer occurs (delete it).

    python3 scripts/archcheck.py                       # check
    python3 scripts/archcheck.py --update-baseline     # only removes entries; adding needs --allow-add (reviewed)

Known limits: a global mutated only from another module is not seen; shadowing is resolved per function by
parameters and assignments only; vendor names in comments and docstrings are not checked.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ratchet

VENDOR_NAMES = frozenset(
    [
        "anthropic",
        "openai",
        "claude",
        "codex",
        "gpt",
        "gemini",
        "deepseek",
        "sonnet",
        "opus",
        "haiku",
        "llama",
        "mistral",
        "qwen",
    ]
)

# Package-relative globs where vendor/model names are allowed (TARGET_ARCHITECTURE §10: adapters and config).
VENDOR_ALLOWED = (
    "adapters/*",  # the adapters are the one place that talks to vendors
    "plugins/llm_providers.py",  # the provider catalogue
    "project.py",  # config defaults
    "data/*",  # packaged data (harness and tool catalogues)
    "runtimes/*",  # TEMPORARY: vendor runtimes until П4 moves them under adapters/
)

# Core purity: subpackage -> subpackages of the same package it may import (besides the standard library).
PURE_IMPORTS = {"contracts": {"contracts"}, "ports": {"contracts", "ports"}}

MUTATORS = frozenset(
    [
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "popitem",
        "clear",
        "update",
        "add",
        "discard",
        "setdefault",
        "sort",
        "reverse",
        "appendleft",
        "extendleft",
        "popleft",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
    ]
)
CONTAINER_CALLS = frozenset(
    [
        "dict",
        "list",
        "set",
        "defaultdict",
        "OrderedDict",
        "deque",
        "Counter",
        "bytearray",
        "WeakValueDictionary",
        "WeakKeyDictionary",
    ]
)
DEFAULT_BASELINE = "scripts/archcheck_baseline.json"


# ---------------------------------------------------------------- loading


class Module:
    def __init__(self, name: str, relpath: str, is_pkg: bool, tree: ast.Module) -> None:
        self.name, self.relpath, self.is_pkg, self.tree = name, relpath, is_pkg, tree


def load_modules(root: Path, package: str) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    base = root / package
    for path in sorted(base.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        is_pkg = parts[-1] == "__init__"
        name = ".".join(parts[:-1] if is_pkg else parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules[name] = Module(name, "/".join(path.relative_to(root).parts), is_pkg, tree)
    return modules


def resolve_imports(mod: Module, package: str) -> list[tuple[str, list[str]]]:
    """Every import in the module (any depth) as (absolute base module, imported names)."""
    out: list[tuple[str, list[str]]] = []
    current_pkg = mod.name if mod.is_pkg else mod.name.rpartition(".")[0]
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = current_pkg.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join(anchor + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            out.append((base, [a.name for a in node.names]))
    return out


def import_edges(modules: dict[str, Module], package: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for mod in modules.values():
        for base, names in resolve_imports(mod, package):
            targets = [f"{base}.{n}" for n in names if f"{base}.{n}" in modules]
            if not targets:
                probe = base
                while probe and probe not in modules:
                    probe = probe.rpartition(".")[0]
                targets = [probe] if probe else []
            edges.update((mod.name, t) for t in targets if t != mod.name)
    return edges


# ---------------------------------------------------------------- cycles


def strongly_connected(nodes: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    graph: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in sorted(edges):
        graph[a].append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(sorted(comp))

    sys.setrecursionlimit(max(1000, 4 * len(nodes)))
    for n in nodes:
        if n not in index:
            visit(n)
    return [c for c in result if len(c) > 1]


def cycle_groups(modules: dict[str, Module], package: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    edges = import_edges(modules, package)
    groups = []
    for comp in strongly_connected(sorted(modules), edges):
        members = set(comp)
        groups.append((comp, sorted(e for e in edges if e[0] in members and e[1] in members)))
    return groups


def check_cycles(modules: dict[str, Module], package: str) -> dict[str, int]:
    return {f"cycles:{a} -> {b}": 1 for _, edges in cycle_groups(modules, package) for a, b in edges}


# ---------------------------------------------------------------- mutable module state


def _is_container(value: ast.expr | None) -> bool:
    if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        return name in CONTAINER_CALLS
    return False


def _module_level(stmts: list[ast.stmt]):
    """Statements executed at import time: the body plus the bodies of module-level if/try/with/for."""
    for st in stmts:
        yield st
        if isinstance(st, (ast.If, ast.For, ast.While)):
            yield from _module_level(st.body + st.orelse)
        elif isinstance(st, ast.Try):
            yield from _module_level(st.body + st.orelse + st.finalbody + [s for h in st.handlers for s in h.body])
        elif isinstance(st, ast.With):
            yield from _module_level(st.body)


def _local_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = fn.args
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    names.update(a.arg for a in (args.vararg, args.kwarg) if a)
    declared_global: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names - declared_global


class _Mutations(ast.NodeVisitor):
    def __init__(self, candidates: set[str]) -> None:
        self.candidates = candidates
        self.mutated: set[str] = set()
        self.scopes: list[set[str]] = []

    def _live(self, name: str) -> bool:
        return name in self.candidates and not any(name in s for s in self.scopes)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(_local_names(node))
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def _target(self, target: ast.expr) -> None:
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and self._live(target.value.id):
            self.mutated.add(target.value.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._target(elt)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            self._target(t)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._target(node.target)
        if isinstance(node.target, ast.Name) and self._live(node.target.id):
            self.mutated.add(node.target.id)  # ``X += [...]`` extends a list in place
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for t in node.targets:
            self._target(t)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr in MUTATORS
            and isinstance(f.value, ast.Name)
            and self._live(f.value.id)
        ):
            self.mutated.add(f.value.id)
        self.generic_visit(node)


def check_globals(modules: dict[str, Module]) -> dict[str, int]:
    found: dict[str, int] = {}
    for mod in modules.values():
        candidates: set[str] = set()
        for st in _module_level(mod.tree.body):
            targets: list[ast.expr] = []
            value = None
            if isinstance(st, ast.Assign):
                targets, value = st.targets, st.value
            elif isinstance(st, ast.AnnAssign):
                targets, value = [st.target], st.value
            if _is_container(value):
                candidates.update(t.id for t in targets if isinstance(t, ast.Name) and not t.id.startswith("__"))
        if candidates:
            visitor = _Mutations(candidates)
            visitor.visit(mod.tree)
            for name in visitor.mutated:
                found[f"globals:{mod.name}:{name}"] = 1
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Global):
                for name in node.names:
                    key = f"globals:{mod.name}:global {name}"
                    found[key] = found.get(key, 0) + 1
    return found


# ---------------------------------------------------------------- vendor names

_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def vendor_tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text) if t.lower() in VENDOR_NAMES]


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def check_vendor(modules: dict[str, Module], package: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for mod in modules.values():
        rel_in_package = mod.relpath.split("/", 1)[1] if "/" in mod.relpath else mod.relpath
        if any(fnmatch.fnmatchcase(rel_in_package, pat) for pat in VENDOR_ALLOWED):
            continue
        docstrings = _docstring_nodes(mod.tree)
        for node in ast.walk(mod.tree):
            texts: list[str] = []
            if isinstance(node, ast.Name):
                texts.append(node.id)
            elif isinstance(node, ast.Attribute):
                texts.append(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                texts.append(node.name)
            elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
                texts.append(node.arg)
            elif isinstance(node, ast.alias):
                texts.append(node.name.replace(".", " "))
            elif isinstance(node, ast.ImportFrom) and node.module:
                texts.append(node.module.replace(".", " "))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                texts.append(node.value)
            for text in texts:
                for tok in vendor_tokens(text):
                    key = f"vendor:{mod.relpath}:{tok}"
                    found[key] = found.get(key, 0) + 1
    return found


# ---------------------------------------------------------------- core purity


def check_purity(modules: dict[str, Module], package: str) -> dict[str, int]:
    stdlib = sys.stdlib_module_names
    found: dict[str, int] = {}
    for mod in modules.values():
        parts = mod.name.split(".")
        sub = parts[1] if len(parts) > 1 else ""
        if sub not in PURE_IMPORTS:
            continue
        allowed = {f"{package}.{s}" for s in PURE_IMPORTS[sub]}
        for base, names in resolve_imports(mod, package):
            top = base.split(".")[0]
            if top in stdlib:
                continue
            if any(base == a or base.startswith(a + ".") for a in allowed):
                continue
            if base == package and names and all(f"{package}.{n}" in allowed for n in names):
                continue
            key = f"purity:{mod.name} imports {base}"
            found[key] = found.get(key, 0) + 1
    return found


# ---------------------------------------------------------------- driver


def scan(root: Path, package: str = "artel") -> dict[str, int]:
    modules = load_modules(root, package)
    current: dict[str, int] = {}
    for part in (
        check_cycles(modules, package),
        check_globals(modules),
        check_vendor(modules, package),
        check_purity(modules, package),
    ):
        current.update(part)
    return current


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".", help="repository root (default: current directory)")
    ap.add_argument("--package", default="artel")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--update-baseline", action="store_true", help="rewrite the baseline (only removes entries)")
    ap.add_argument("--allow-add", action="store_true", help="with --update-baseline: allow adding entries (reviewed)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    baseline_path = (root / args.baseline) if not Path(args.baseline).is_absolute() else Path(args.baseline)
    current = scan(root, args.package)
    if args.update_baseline:
        return ratchet.update("archcheck", baseline_path, current, allow_add=args.allow_add)
    baseline = ratchet.load(baseline_path)
    code = ratchet.report("archcheck", current, baseline, args.baseline)
    new_cycles = {k for k in ratchet.diff(current, baseline)[0] if k.startswith("cycles:")}
    for comp, edges in cycle_groups(load_modules(root, args.package), args.package) if new_cycles else []:
        if any(f"cycles:{a} -> {b}" in new_cycles for a, b in edges):  # only the groups a new edge belongs to
            print("Import cycle among: " + ", ".join(comp))
            for a, b in edges:
                print(f"    {a} -> {b}" + ("   <- new" if f"cycles:{a} -> {b}" in new_cycles else ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
