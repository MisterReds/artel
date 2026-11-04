"""scripts/ratchet.py, scripts/lint_ratchet.py: the baseline only shrinks; ruff and mypy are counted per file."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lint_ratchet  # noqa: E402
import ratchet  # noqa: E402


def test_diff_separates_new_from_stale():
    new, stale = ratchet.diff({"a": 2, "b": 1, "c": 1}, {"a": 1, "b": 1, "d": 3, "c": 4})
    assert new == {"a": 1}
    assert stale == {"d": 3, "c": 3}


def test_report_prints_the_delete_hint_and_returns_one(capsys):
    assert ratchet.report("x", {"k": 1}, {"k": 3, "gone": 1}, "base.json") == 1
    out = capsys.readouterr().out
    assert "gone  (delete" in out and "k  (lower to 1" in out and "base.json" in out
    assert ratchet.report("x", {"k": 1}, {"k": 1}, "base.json") == 0


def test_update_refuses_to_add_or_raise_without_allow_add(tmp_path, capsys):
    path = tmp_path / "b.json"
    ratchet.dump(path, {"k": 2})
    assert ratchet.update("x", path, {"k": 3}, allow_add=False) == 1
    assert ratchet.update("x", path, {"k": 1, "other": 1}, allow_add=False) == 1
    assert ratchet.load(path) == {"k": 2}
    assert ratchet.update("x", path, {"k": 1}, allow_add=False) == 0
    assert ratchet.load(path) == {"k": 1}
    assert ratchet.update("x", path, {"k": 1, "other": 1}, allow_add=True) == 0
    assert ratchet.load(path) == {"k": 1, "other": 1}


def test_ruff_counts_are_per_file_and_rule(tmp_path):
    out = json.dumps(
        [
            {"filename": str(tmp_path / "artel" / "a.py"), "code": "F401"},
            {"filename": str(tmp_path / "artel" / "a.py"), "code": "F401"},
            {"filename": str(tmp_path / "artel" / "a.py"), "code": "E501"},
        ]
    )
    assert lint_ratchet.ruff_counts(out, tmp_path) == {"artel/a.py:F401": 2, "artel/a.py:E501": 1}


def test_mypy_counts_ignore_notes():
    text = (
        "artel/a.py:3: error: bad  [arg-type]\nartel/a.py:9:4: error: worse  [index]\n"
        "artel/a.py:9: note: hint\nartel/b.py:1: error: x\n"
    )
    assert lint_ratchet.mypy_counts(text, Path("/nowhere")) == {"artel/a.py": 2, "artel/b.py": 1}


def test_ruff_gate_end_to_end_on_a_temp_repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "a.py").write_text("import os\n")

    def gate(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "lint_ratchet.py"), "ruff", "--root", str(tmp_path), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    assert gate().returncode == 1  # a new violation
    assert gate("--update-baseline").returncode == 1  # refuses to add
    assert gate("--update-baseline", "--allow-add").returncode == 0
    assert gate().returncode == 0
    (tmp_path / "a.py").write_text("")
    stale = gate()
    assert stale.returncode == 1 and "a.py:F401" in stale.stdout and "delete" in stale.stdout


def test_mypy_strict_zone_errors_fail_even_when_baselined(tmp_path, monkeypatch, capsys):
    (tmp_path / "scripts").mkdir()
    out = "artel/contracts/x.py:1: error: nope  [misc]\n"
    monkeypatch.setattr(lint_ratchet, "run_tool", lambda cmd, root: out)
    ratchet.dump(tmp_path / "scripts" / "mypy_baseline.json", {"artel/contracts/x.py": 1})
    assert lint_ratchet.main(["mypy", "--root", str(tmp_path)]) == 1
    assert "strict zone" in capsys.readouterr().out
