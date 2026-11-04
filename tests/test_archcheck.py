"""scripts/archcheck.py: planted violations of each kind fail, a clean package passes, the baseline is a ratchet."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import archcheck  # noqa: E402


def make_package(root: Path, files: dict[str, str], name: str = "pkg") -> Path:
    base = root / name
    base.mkdir(parents=True, exist_ok=True)
    (base / "__init__.py").write_text("")
    for rel, body in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent != base and not (path.parent / "__init__.py").exists():
            (path.parent / "__init__.py").write_text("")
        path.write_text(textwrap.dedent(body))
    return root


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "archcheck.py"), "--root", str(root), "--package", "pkg", *args],
        capture_output=True, text=True, check=False,
    )


CLEAN = {
    "contracts/__init__.py": "",
    "contracts/model.py": "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass Task:\n    id: str\n",
    "ports/__init__.py": "",
    "ports/store.py": "from typing import Protocol\nfrom pkg.contracts.model import Task\n\nclass Store(Protocol):\n    def get(self) -> Task: ...\n",
    "core.py": "from pkg import util\n\nTABLE = {'a': 1}\n\ndef f():\n    return util.g(TABLE['a'])\n",
    "util.py": "def g(x):\n    return x\n",
    "adapters/__init__.py": "",
    "adapters/llm.py": "import anthropic\nMODEL = 'claude-3-opus'\n",
}


def test_clean_package_passes(tmp_path):
    root = make_package(tmp_path, CLEAN)
    result = run(root)
    assert result.returncode == 0, result.stdout
    assert archcheck.scan(root, "pkg") == {}


def test_import_cycle_is_reported_with_its_edges(tmp_path):
    root = make_package(tmp_path, {"a.py": "import pkg.b\n", "b.py": "def f():\n    from pkg import a\n    return a\n"})
    assert archcheck.scan(root, "pkg") == {"cycles:pkg.a -> pkg.b": 1, "cycles:pkg.b -> pkg.a": 1}
    result = run(root)
    assert result.returncode == 1
    assert "pkg.a -> pkg.b" in result.stdout and "pkg.b -> pkg.a" in result.stdout


def test_relative_import_cycle_and_three_module_cycle(tmp_path):
    root = make_package(tmp_path, {"a.py": "from . import b\n", "b.py": "from .c import x\n", "c.py": "from pkg.a import y\nx = 1\n"})
    keys = set(archcheck.scan(root, "pkg"))
    assert keys == {"cycles:pkg.a -> pkg.b", "cycles:pkg.b -> pkg.c", "cycles:pkg.c -> pkg.a"}


def test_one_way_imports_are_not_cycles(tmp_path):
    root = make_package(tmp_path, {"a.py": "import pkg.b\n", "b.py": "import pkg.c\n", "c.py": ""})
    assert archcheck.scan(root, "pkg") == {}


@pytest.mark.parametrize(
    "body, expected",
    [
        ("CACHE = {}\ndef put(k):\n    CACHE[k] = 1\n", {"globals:pkg.m:CACHE": 1}),
        ("ITEMS = []\ndef put(k):\n    ITEMS.append(k)\n", {"globals:pkg.m:ITEMS": 1}),
        ("from collections import defaultdict\nD = defaultdict(list)\ndef put(k):\n    D.update({k: 1})\n", {"globals:pkg.m:D": 1}),
        ("SEEN = set()\ndef put(k):\n    SEEN.add(k)\n", {"globals:pkg.m:SEEN": 1}),
        ("N = 0\ndef bump():\n    global N\n    N += 1\n", {"globals:pkg.m:global N": 1}),
    ],
)
def test_mutable_module_state_is_reported(tmp_path, body, expected):
    root = make_package(tmp_path, {"m.py": body})
    assert archcheck.scan(root, "pkg") == expected


@pytest.mark.parametrize(
    "body",
    [
        "TABLE = {'a': 1}\ndef get(k):\n    return TABLE[k]\n",  # a constant table that is only read
        "ITEMS = []\ndef f():\n    ITEMS = []\n    ITEMS.append(1)\n    return ITEMS\n",  # a shadowing local
        "ITEMS = []\ndef f(ITEMS):\n    ITEMS.append(1)\n",  # a parameter
        "__all__ = ['f']\n__all__.append('g')\n",
    ],
)
def test_read_only_or_shadowed_state_is_not_reported(tmp_path, body):
    root = make_package(tmp_path, {"m.py": body})
    assert archcheck.scan(root, "pkg") == {}


def test_vendor_names_outside_the_allow_list_are_reported(tmp_path):
    root = make_package(tmp_path, {
        "logic.py": "import anthropic\nclass ClaudeClient:\n    model = 'gpt-4o'\n    other = 'Sonnet'\n",
        "adapters/ok.py": "import openai\n",
    })
    found = archcheck.scan(root, "pkg")
    assert found == {
        "vendor:pkg/logic.py:anthropic": 1,
        "vendor:pkg/logic.py:claude": 1,
        "vendor:pkg/logic.py:gpt": 1,
        "vendor:pkg/logic.py:sonnet": 1,
    }


def test_vendor_check_ignores_docstrings_and_lookalikes(tmp_path):
    root = make_package(tmp_path, {"m.py": '"""Talks to Claude."""\ndef f():\n    """Uses openai."""\n    return "operate"\n'})
    assert archcheck.scan(root, "pkg") == {}


@pytest.mark.parametrize(
    "files, key",
    [
        ({"contracts/__init__.py": "", "contracts/a.py": "import yaml\n"}, "purity:pkg.contracts.a imports yaml"),
        ({"contracts/__init__.py": "", "contracts/a.py": "from pkg.ports import x\n", "ports/__init__.py": ""},
         "purity:pkg.contracts.a imports pkg.ports"),
        ({"ports/__init__.py": "", "ports/a.py": "from pkg import core\n", "core.py": ""}, "purity:pkg.ports.a imports pkg"),
        ({"ports/__init__.py": "", "ports/a.py": "from ..adapters import llm\n", "adapters/__init__.py": "", "adapters/llm.py": ""},
         "purity:pkg.ports.a imports pkg.adapters"),
    ],
)
def test_core_purity_violations(tmp_path, files, key):
    root = make_package(tmp_path, files)
    assert key in archcheck.scan(root, "pkg")
    assert run(root).returncode == 1


def test_ports_may_import_contracts_and_stdlib(tmp_path):
    root = make_package(tmp_path, {
        "contracts/__init__.py": "", "contracts/m.py": "import json\n",
        "ports/__init__.py": "", "ports/p.py": "from __future__ import annotations\nfrom pkg.contracts import m\nimport os\n",
    })
    assert archcheck.scan(root, "pkg") == {}


def test_ratchet_new_violation_fails_and_known_debt_passes(tmp_path):
    root = make_package(tmp_path, {"m.py": "CACHE = {}\ndef f():\n    CACHE['a'] = 1\n"})
    assert run(root).returncode == 1
    assert run(root, "--update-baseline", "--allow-add").returncode == 0
    assert run(root).returncode == 0
    (root / "pkg" / "n.py").write_text("OTHER = []\ndef g():\n    OTHER.append(1)\n")
    result = run(root)
    assert result.returncode == 1
    assert "NEW" in result.stdout and "pkg.n:OTHER" in result.stdout


def test_ratchet_fixed_violation_with_stale_entry_fails_with_delete_hint(tmp_path):
    root = make_package(tmp_path, {"m.py": "CACHE = {}\ndef f():\n    CACHE['a'] = 1\n"})
    run(root, "--update-baseline", "--allow-add")
    (root / "pkg" / "m.py").write_text("def f():\n    return {}\n")
    result = run(root)
    assert result.returncode == 1
    assert "globals:pkg.m:CACHE" in result.stdout and "delete" in result.stdout
    assert run(root, "--update-baseline").returncode == 0  # removing is always allowed
    assert json.loads((root / "scripts" / "archcheck_baseline.json").read_text()) == {}
    assert run(root).returncode == 0


def test_update_baseline_refuses_to_add_without_allow_add(tmp_path):
    root = make_package(tmp_path, {"m.py": "CACHE = {}\ndef f():\n    CACHE['a'] = 1\n"})
    result = run(root, "--update-baseline")
    assert result.returncode == 1 and "--allow-add" in result.stdout
    assert not (root / "scripts" / "archcheck_baseline.json").exists()


def test_a_repeated_known_violation_counts_as_new(tmp_path):
    root = make_package(tmp_path, {"m.py": "import anthropic\n"})
    run(root, "--update-baseline", "--allow-add")
    (root / "pkg" / "m.py").write_text("import anthropic\nx = anthropic.Client\n")
    result = run(root)
    assert result.returncode == 1 and "more than recorded" in result.stdout


def test_repository_passes_its_own_gate():
    repo = SCRIPTS.parent
    result = subprocess.run([sys.executable, str(SCRIPTS / "archcheck.py"), "--root", str(repo)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout
