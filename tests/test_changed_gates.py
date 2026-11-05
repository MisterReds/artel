"""scripts/changed_gates.py, scripts/mutation.py, scripts/coverage_gate.py: the gates on changed code."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import changed_gates as cg  # noqa: E402
import coverage_gate  # noqa: E402
import mutation  # noqa: E402

SRC = textwrap.dedent("""\
    def simple(x):
        return x + 1


    def branchy(a, b, c):
        if a:
            return 1
        if b:
            return 2
        if c and a:
            return 3
        return 0


    class K:
        def method(self, v):
            for i in v:
                if i:
                    return i
            return None
""")


def test_crap_formula_matches_the_definition():
    assert cg.crap_score(1, 1.0) == 1
    assert cg.crap_score(5, 0.0) == 30  # 25 * 1 + 5
    assert cg.crap_score(10, 0.5) == pytest.approx(10 * 10 * 0.125 + 10)


def test_function_coverage_counts_statements_inside_the_function_only():
    assert cg.function_coverage(5, 9, {5, 6, 20}, {7, 8, 30}) == 0.5
    assert cg.function_coverage(1, 3, set(), set()) == 1.0  # nothing to execute


def test_functions_of_lists_functions_and_methods_with_complexity():
    pytest.importorskip("radon", reason="radon comes with the gates extra: pip install -e '.[gates]'")
    got = {name: (first, comp) for name, first, last, comp in cg.functions_of(SRC)}
    assert got["simple"] == (1, 1)
    assert got["branchy"][1] == 5  # three ifs, one boolean operator, plus one
    assert got["K.method"][1] == 3


def test_crap_report_flags_only_changed_functions_and_uses_coverage():
    pytest.importorskip("radon", reason="radon comes with the gates extra: pip install -e '.[gates]'")
    changed = {"artel/m.py": {2, 20}}  # a line in `simple` and a line in K.method
    coverage = {
        "files": {"artel/m.py": {"executed_lines": [2], "missing_lines": [6, 7, 9, 10, 11, 12, 17, 18, 19, 20]}}
    }
    rows = {r[1].split(":")[0]: r for r in cg.crap_report(changed, coverage, {"artel/m.py": SRC})}
    assert set(rows) == {"simple", "K.method"}
    assert rows["simple"][2] == 1  # fully covered, complexity 1
    assert rows["K.method"][2] >= cg.CRAP_LIMIT - 1 and rows["K.method"][4] == 0  # uncovered
    # an uncovered file counts as zero coverage
    unseen = cg.crap_report({"artel/m.py": {6}}, {"files": {}}, {"artel/m.py": SRC})
    assert unseen[0][4] == 0.0 and unseen[0][2] == 5**2 + 5


def test_duplicates_are_parsed_and_filtered_to_changed_lines():
    out = textwrap.dedent("""\
        ************* Module artel.a
        artel/a.py:1:0: R0801: Similar lines in 2 files
        ==artel.a:[10:20]
        ==artel.b:[40:50]
            x = 1
        ************* Module artel.c
        artel/c.py:1:0: R0801: Similar lines in 2 files
        ==artel.c:[1:9]
        ==artel.d:[1:9]
    """)
    blocks = cg.parse_duplicates(out)
    assert blocks == [[("artel/a.py", 10, 20), ("artel/b.py", 40, 50)], [("artel/c.py", 1, 9), ("artel/d.py", 1, 9)]]
    assert cg.duplicates_touching(blocks, {"artel/b.py": {45}}) == [blocks[0]]
    assert cg.duplicates_touching(blocks, {"artel/a.py": {5}, "artel/z.py": {1}}) == []


def test_explained_file_parses_and_rejects_malformed_lines():
    assert cg.parse_explained("# c\n\nartel/a.py:12: equivalent mutant\n") == {("artel/a.py", 12)}
    with pytest.raises(SystemExit):
        cg.parse_explained("artel/a.py 12 no colon\n")


def test_mutant_names_split_into_file_class_and_function():
    assert mutation.split_name("artel.contracts._base.x_freeze__mutmut_46") == (
        "artel/contracts/_base.py",
        None,
        "freeze",
    )
    assert mutation.split_name("artel.contracts._base.xǁContractErrorǁ__init____mutmut_2") == (
        "artel/contracts/_base.py",
        "ContractError",
        "__init__",
    )


MUTANTS = textwrap.dedent("""\
    def x_simple__mutmut_orig(x):
        return x + 1


    def x_simple__mutmut_1(x):
        return x - 1


    def x_simple__mutmut_2(x):
        y = None
        return x + 1
""")


def test_units_touching_names_the_enclosing_function_or_method():
    assert mutation.units_touching(SRC, {2}) == {(None, "simple")}
    assert mutation.units_touching(SRC, {18}) == {("K", "method")}
    assert mutation.units_touching(SRC, {3, 4}) == set()  # blank lines between functions
    assert mutation.mutant_glob("artel/m.py", None, "simple") == "artel.m.x_simple__mutmut_*"
    assert mutation.mutant_glob("artel/m.py", "K", "method") == "artel.m.xǁKǁmethod__mutmut_*"


def test_mutated_lines_come_from_comparing_the_mutant_with_the_original_copy():
    name = "artel.m.x_simple__mutmut_1"
    assert mutation.mutated_lines(SRC, MUTANTS, name) == {2}  # `return x + 1` is line 2 of the real file
    # an inserted line is attributed to the line it precedes
    assert mutation.mutated_lines(SRC, MUTANTS, "artel.m.x_simple__mutmut_2") == {2}
    # unknown mutant: fall back to the function's first line
    assert mutation.mutated_lines(SRC, MUTANTS, "artel.m.x_simple__mutmut_9") == {1}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_changed_lines_skip_mechanical_commits(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "master")
    (tmp_path / "artel").mkdir()
    (tmp_path / "scripts").mkdir()
    f = tmp_path / "artel" / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD").strip()
    f.write_text("a = 1\nb = 22\nc = 3\n")  # a real change
    git(tmp_path, "commit", "-qam", "real")
    f.write_text("a = 1\nb = 22\nc = 33\n")  # a mechanical rewrite
    git(tmp_path, "commit", "-qam", "mechanical")
    mech = git(tmp_path, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(cg, "ROOT", tmp_path)
    assert cg.changed_lines(base) == {"artel/m.py": {2, 3}}
    (tmp_path / "scripts" / "gates_ignore_revs.txt").write_text(f"{mech}  # mechanical\n")
    assert cg.changed_lines(base) == {"artel/m.py": {2}}


def test_coverage_threshold_only_goes_up(tmp_path, capsys, monkeypatch):
    assert coverage_gate.check(80.4, 79) == 0
    assert coverage_gate.check(77.0, 79) == 1
    assert coverage_gate.monotonic(80, 79) == 0
    assert coverage_gate.monotonic(78, 79) == 1
    f = tmp_path / "t.txt"
    f.write_text("# comment 5\n70\n")
    cov = tmp_path / "c.json"
    cov.write_text(json.dumps({"totals": {"percent_covered": 83.7}}))
    monkeypatch.setattr(coverage_gate, "THRESHOLD_FILE", f)
    assert coverage_gate.main(["raise", str(cov)]) == 0
    assert f.read_text() == "# comment 5\n82\n"
    cov.write_text(json.dumps({"totals": {"percent_covered": 60.0}}))
    coverage_gate.main(["raise", str(cov)])
    assert f.read_text() == "# comment 5\n82\n"  # never lowered


def test_mutation_copy_holds_the_repo_data_the_tests_read(tmp_path):
    """mutmut runs the tests inside mutants/; data the code reads from the repo root (acceptance scenarios in bench/)
    must be copied there, or the clean run fails before any mutant is tried."""
    import configparser

    for path in ("artel/a.py", "tests/test_a.py", "bench/acceptance/S.yaml", "docs/agents_md.md", "scripts/x.py",
                 "install.sh", ".github/workflows/ci.yml"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x", encoding="utf-8")
    (tmp_path / "coverage.json").write_text("{}", encoding="utf-8")  # untracked output, not repo data
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "artel", "tests", "bench", "docs", "scripts", "install.sh", ".github"], cwd=tmp_path,
                   check=True)
    mutation.write_config(tmp_path, ["artel/a.py"], ["tests/test_a.py"])
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / "setup.cfg", encoding="utf-8")
    assert cfg["mutmut"]["also_copy"].split() == ["bench/", "docs/", "install.sh", "scripts/"]
