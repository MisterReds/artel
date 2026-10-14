"""The diagnostician: known causes, a model-made report on an available runtime, rules without a model, and an
automatic explanation in a task that stopped for a person."""
import json
import types

import pytest

from artel import doctor
from artel.manager import new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, deep_merge


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


def test_known_causes_cover_what_owners_actually_hit():
    for text, needle in (("bwrap: setting up uid map: Permission denied", "bubblewrap"),
                         ("No module named pytest", "pytest"),
                         ("llama-server не ответил за 90 с", "эмбеддингов"),
                         ("You've hit your usage limit", "лимит")):
        hits = doctor.known_causes(text)
        assert hits and needle in hits[0]["cause"] and hits[0]["steps"], text
    assert doctor.known_causes("all good") == []


def test_a_model_explains_the_failure_and_the_report_is_kept(cfg, tmp_path):
    rep = doctor.diagnose(cfg, "Codex: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted")
    assert rep["by"] == "native" and "bubblewrap" in rep["cause"] and rep["who"] == "owner" and rep["steps"]
    kept = [json.loads(ln) for ln in (tmp_path / "home" / "doctor" / "reports.jsonl").read_text().splitlines()]
    assert kept[-1]["id"] == rep["id"]
    assert not any(p["name"].startswith("why-") for p in __import__("artel.project", fromlist=["x"]).list_projects())


def test_without_a_model_the_rules_still_answer(cfg):
    rep = doctor.diagnose(cfg, "No module named pytest", use_model=False)
    assert rep["by"] == "rules" and "pytest" in rep["cause"] and rep["steps"][0].startswith("~/.artel/venv/bin/pip")
    unknown = doctor.diagnose(cfg, "something strange", use_model=False)
    assert unknown["who"] == "unknown" and unknown["steps"]


def test_a_task_that_stops_for_a_person_gets_an_explanation(cfg):
    from artel.agent import Outcome

    def no_cli(kernel, project, *, role, ctx, **kw):
        return Outcome(status="error", error="Codex CLI не установлен: «Подключения» → «Установить»")

    mod = types.ModuleType("nocli_rt")
    mod.setup = lambda k: k.register_runtime("nocli", no_cli)
    c = deep_merge(cfg, {"runtime_by_role": {"worker": "nocli"}})
    k = new_kernel(c)
    k.load_plugin("nocli_rt", mod)
    project, status = run_goal("setup problem", c, kernel=k)
    assert status == "waiting_human"
    notes = [cm for t in project.board.list() for cm in project.board.comments(t["id"]) if cm["author"] == "Диагност"]
    assert notes and "Причина:" in notes[0]["body"] and "не установлена" in notes[0]["body"]
    assert any(e["kind"] == "doctor/report" for e in project.log.read())


def test_proxy_is_suspected_only_for_local_connection_failures():
    assert doctor.known_causes("urlopen error [Errno 111] Connection refused", proxies=True)[0] is doctor.PROXY_HINT
    assert doctor.known_causes("urlopen error [Errno 111] Connection refused", proxies=False) == []
    assert doctor.PROXY_HINT not in doctor.known_causes("No module named pytest", proxies=True)
