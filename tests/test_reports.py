"""Failure reports: secrets never leave the machine, the same failure is one issue with comments, the owner hears
about it, and the diagnostician's answers become reports."""
import json
import os
import sys
from pathlib import Path

import pytest

from artel import reports
from artel.project import DEFAULT_CONFIG, deep_merge


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return tmp_path


def test_secrets_and_personal_paths_are_removed():
    text = (f"ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop12345 Authorization: Bearer abcdef1234567890xyz "
            f"ghp_{'a' * 36} 123456789:{'b' * 35} owner@example.com {Path.home()}/.artel/x")
    red = reports.redact(text)
    for secret in ("sk-ant-abc", "abcdef1234567890xyz", "ghp_aaa", "bbbbbbbbbb", "owner@example.com"):
        assert secret not in red, secret
    if str(Path.home()) not in ("/", "/root"):
        assert str(Path.home()) not in red


def test_the_same_failure_has_the_same_fingerprint():
    a = reports.fingerprint("failure", "bwrap: setting up uid map: Permission denied at /home/a/x line 12")
    b = reports.fingerprint("failure", "bwrap: setting up uid map: Permission denied at /home/b/y line 99")
    c = reports.fingerprint("failure", "No module named pytest")
    assert a == b != c


def test_reports_are_kept_and_listeners_hear_them(home):
    heard = []
    reports._listeners.append(heard.append)
    try:
        rep = reports.record("failure", "сервер не ответил", "token=supersecretvalue123",
                             diagnosis={"cause": "прокси", "who": "owner", "steps": ["no_proxy"]})
    finally:
        reports._listeners.remove(heard.append)
    assert heard and heard[0]["id"] == rep["id"] and "supersecretvalue123" not in rep["text"]
    assert reports.load()[0]["diagnosis"]["cause"] == "прокси"
    assert "Причина: прокси" in reports.telegram_text(rep)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gh is a shebang script")
def test_sending_to_the_developers_is_one_issue_per_failure(home, monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_bytes((Path(__file__).parent / "fakes" / "gh").read_bytes())
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh.json"))
    monkeypatch.setattr(reports, "github_repo", lambda: "o/r")
    first = reports.record("failure", "No module named pytest", "trace 1")
    again = reports.record("failure", "No module named pytest", "trace 2")
    r1, r2 = reports.send_github(first["id"]), reports.send_github(again["id"])
    state = json.loads((tmp_path / "gh.json").read_text())
    assert r1["ok"] and not r1["comment"] and r2["ok"] and r2["comment"]
    assert len(state["issues"]) == 1 and len(state["comments"]) == 1
    assert first["fingerprint"] in state["issues"][0]["body"] and "Диагност" not in state["issues"][0]["title"]
    assert reports.load()[0]["sent"]["url"] == "https://github.com/o/r/issues/1"


def test_without_github_cli_sending_explains_why(home, monkeypatch):
    monkeypatch.setattr(reports.shutil, "which", lambda name: None)
    rep = reports.record("failure", "x", "y")
    res = reports.send_github(rep["id"])
    assert not res["ok"] and "GitHub CLI" in res["error"]


def test_a_diagnosis_becomes_a_report(home):
    from artel import doctor

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    doctor.diagnose(cfg, "No module named pytest", use_model=False)
    rows = reports.load()
    assert rows and rows[0]["title"] == "No module named pytest" and "pytest" in rows[0]["diagnosis"]["cause"]


def test_redaction_is_linear_on_long_runs():
    import base64
    import os
    import time

    from artel.reports import redact_deep

    n = 1_000_000
    cases = ["a" * n, "x" * n + "@", base64.b64encode(os.urandom(n * 3 // 4)).decode(), os.urandom(n // 2).hex(),
             "a." * (n // 2), "key=\"" * (n // 5), "bearer " * (n // 7)]
    for text in cases:
        t0 = time.time()
        redact_deep({"a": text})
        assert time.time() - t0 < 1.0, text[:12]
    assert redact_deep({"a": "mail me: bob@example.com"})["a"] == "mail me: <e-mail>"


def test_journal_redacts_outside_its_lock(tmp_path, monkeypatch):
    import artel.reports as reports
    from artel.kernel import EventLog

    log = EventLog(tmp_path / "events.jsonl")
    held = []
    real = reports.redact_deep
    monkeypatch.setattr(reports, "redact_deep", lambda v: (held.append(log._lock.locked()), real(v))[1])
    log.append("x", note="hi")
    assert held and not any(held)
