import json
import subprocess
from pathlib import Path

import pytest

from artel.evolve import bench
from artel.evolve.diagnose import doctor, render_markdown, trajectory_profile
from artel.evolve.engine import Evolution, Hypothesis, Policy, git
from artel.project import DEFAULT_CONFIG, deep_merge


def _suite(pass_rate, cost, partial=None, results=None):
    return {"summary": {"tasks": 2, "pass_rate": pass_rate, "partial_mean": partial if partial is not None else pass_rate,
                        "cost_usd": cost, "tokens": 0, "duration_mean_s": 10, "attempts": 0, "rejections": 0,
                        "stalls": 0, "audit_rounds": 0},
            "results": results or [{"id": "a", "pass": pass_rate >= 0.5, "partial": pass_rate},
                                   {"id": "b", "pass": pass_rate >= 1, "partial": pass_rate}]}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    r = tmp_path / "selfrepo"
    (r / "artel").mkdir(parents=True)
    (r / "artel" / "__init__.py").write_text('"""toy artel"""\n')
    (r / "tests").mkdir()
    (r / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        git(r, *args)
    return r


def make(repo, tmp_path, scores, monkeypatch=None):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    calls = iter(scores)
    return Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=lambda code, label: next(calls))


def test_policy():
    p = Policy()
    ok = {"ok": True}
    better = bench.compare(_suite(0.5, 1.0), _suite(1.0, 1.1))
    assert p.decide(better, ok).accepted
    same_cheaper = bench.compare(_suite(1.0, 1.0), _suite(1.0, 0.7))
    assert p.decide(same_cheaper, ok).accepted
    worse = bench.compare(_suite(1.0, 1.0), _suite(0.5, 0.5))
    assert not p.decide(worse, ok).accepted and "regressed" in p.decide(worse, ok).reason
    assert not p.decide(better, {"ok": False, "summary": "1 failed"}).accepted


def test_generation_merges_winner_and_keeps_loser_branch(repo, tmp_path):
    # baseline, candidate 1 (worse), candidate 2 (better)
    ev = make(repo, tmp_path, [_suite(0.5, 1.0), _suite(0.0, 1.0), _suite(1.0, 1.0)])
    hyps = [Hypothesis("worse idea", "r", "p"), Hypothesis("better idea", "r", "p")]
    before = git(repo, "rev-parse", "HEAD")
    s = ev.generation(hypotheses=hyps)
    assert s["accepted"] == ["evolve/g1-2-better-idea"]
    branches = git(repo, "branch", "--list", "evolve/*")
    assert "evolve/g1-1-worse-idea" in branches and "evolve/g1-2-better-idea" in branches
    assert git(repo, "rev-parse", "HEAD") != before
    assert "evolve-gen-1" in git(repo, "tag")
    # Artel (scripted team) actually wrote files into the candidate worktree, committed on its branch
    files = git(repo, "show", "--stat", "--format=", "evolve/g1-2-better-idea")
    assert "report.md" in files
    led = [e for e in ev.ledger() if e["kind"] == "candidate"]
    assert {e["branch"]: e["accepted"] for e in led} == {"evolve/g1-1-worse-idea": False,
                                                         "evolve/g1-2-better-idea": True}
    loser = next(e for e in led if not e["accepted"])
    assert "regressed" in loser["reason"] and loser["delta"]["pass_rate"] == -0.5
    assert not list((tmp_path / "evo" / "worktrees").iterdir())  # worktrees cleaned up


def test_failing_tests_reject_candidate(repo, tmp_path):
    ev = make(repo, tmp_path, [_suite(0.5, 1.0), _suite(1.0, 1.0)])

    def breaking_impl(h, wt):
        (wt / "tests" / "test_broken.py").write_text("def test_no():\n    assert False\n")
        return {"status": "done"}

    ev.implement_fn = breaking_impl
    s = ev.generation(hypotheses=[Hypothesis("break it", "r", "p")])
    assert s["accepted"] == []
    e = [x for x in ev.ledger() if x["kind"] == "candidate"][0]
    assert e["reason"].startswith("tests fail") and "1 failed" in e["reason"]


def test_proposals_and_absorb_via_agents(repo, tmp_path):
    src = tmp_path / "otherharness"
    src.mkdir()
    (src / "README.md").write_text("Other harness with a marker feature")
    (src / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, ...")
    ev = make(repo, tmp_path, [_suite(0.5, 1.0), _suite(1.0, 1.0), _suite(1.0, 1.0)])
    out = ev.absorb(str(src), run=True, top=1)
    assert out["license"] == "MIT" and out["features"][0]["name"] == "marker"
    assert out["generation"]["accepted"] == ["evolve/g1-1-absorb-from-otherharness-marker"]
    cand = [e for e in ev.ledger() if e["kind"] == "absorb_candidate"][0]
    assert cand["hypothesis"]["refs"] == [str(src)] and "allows copying" in cand["hypothesis"]["license_note"]
    # evolver proposes from diagnosis (scripted evolver returns one hypothesis)
    s = ev.generation(candidates=1)
    assert s["candidates"] == 1


def test_bench_hidden_checks_and_doctor(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "h"))
    task = {"id": "t", "dir": str(tmp_path / "task"), "hidden": ["python3 .artel_eval/c.py"]}
    (tmp_path / "task" / "hidden").mkdir(parents=True)
    (tmp_path / "task" / "hidden" / "c.py").write_text("import os; assert os.path.exists('x.txt')")
    wd = tmp_path / "wd"
    wd.mkdir()
    assert bench.run_hidden(task, wd)["pass"] is False
    (wd / "x.txt").write_text("1")
    res = bench.run_hidden(task, wd)
    assert res["pass"] is True and not (wd / ".artel_eval").exists()
    # doctor over a real scripted project with a rejection
    from artel.manager import run_goal

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05, "scripted": {"review_failures": 1}})
    project, _ = run_goal("doctor me", cfg)
    prof = trajectory_profile([project.root])
    assert prof["rejections"] == 1 and prof["projects"] == 1
    rep = doctor(None, [project.root], run_tests=False)
    assert any(f["area"] == "quality" for f in rep["findings"])
    assert "Находки" in render_markdown(rep)


@pytest.mark.skipif(not (Path(__file__).parents[1] / "evals" / "tasks").exists(), reason="no eval tasks")
def test_eval_tasks_are_well_formed():
    tasks = bench.load_tasks()
    assert {t["id"] for t in tasks} >= {"wordfreq-cli", "fix-inventory", "csv-report", "adr-doc"}
    for t in tasks:
        assert t["goal"] and t["hidden"]
        assert (Path(t["dir"]) / "hidden").exists()
