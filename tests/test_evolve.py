import json
import subprocess
from pathlib import Path

import pytest

from artel.evolve import bench
from artel.evolve.diagnose import doctor, render_markdown, trajectory_profile
from artel.evolve.engine import Evolution, Hypothesis, Policy, git, slug
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
    (r / "artel" / "__init__.py").write_text('"""toy artel"""\n', encoding="utf-8")
    (r / "tests").mkdir()
    (r / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
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
        (wt / "tests" / "test_broken.py").write_text("def test_no():\n    assert False\n", encoding="utf-8")
        return {"status": "done"}

    ev.implement_fn = breaking_impl
    s = ev.generation(hypotheses=[Hypothesis("break it", "r", "p")])
    assert s["accepted"] == []
    e = [x for x in ev.ledger() if x["kind"] == "candidate"][0]
    assert e["reason"].startswith("tests fail") and "1 failed" in e["reason"]


def test_proposals_and_absorb_via_agents(repo, tmp_path):
    src = tmp_path / "otherharness"
    src.mkdir()
    (src / "README.md").write_text("Other harness with a marker feature", encoding="utf-8")
    (src / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, ...", encoding="utf-8")
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
    (tmp_path / "task" / "hidden" / "c.py").write_text("import os; assert os.path.exists('x.txt')", encoding="utf-8")
    wd = tmp_path / "wd"
    wd.mkdir()
    assert bench.run_hidden(task, wd)["pass"] is False
    (wd / "x.txt").write_text("1", encoding="utf-8")
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


# --- population generations: parallel, screening, finalists, stacking, budget -------------------

POP = [("fix a", {"fix_a.txt": "1"}), ("fix b", {"fix_b.txt": "1"}), ("fix c one", {"artel/__init__.py": "C = 1\n"}),
       ("fix c two", {"artel/__init__.py": "C = 2\n"}), ("break d", {"break_d.txt": "1"}),
       ("break tests", {"tests/test_broken.py": "def test_no():\n    assert False\n"})]


def _pop_suite(code, tasks=None):
    code = Path(code)
    ok = {"a": (code / "fix_a.txt").exists(), "b": (code / "fix_b.txt").exists(),
          "c": "C =" in (code / "artel" / "__init__.py").read_text(encoding="utf-8"), "d": not (code / "break_d.txt").exists()}
    rows = [{"id": t, "pass": v, "partial": 1.0 if v else 0.0, "cost_usd": 0.5} for t, v in ok.items()
            if not tasks or t in tasks]
    return {"results": rows, "summary": bench.summarize(rows)}


def _population(repo, tmp_path):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    calls = []

    def bench_fn(code, label, tasks=None):
        calls.append((label, tuple(tasks or ())))
        return _pop_suite(code, tasks)

    def impl(h, wt):
        for rel, text in dict(POP)[h.title].items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text, encoding="utf-8")
        return {"cost_usd": 0.1}

    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    return ev, calls, [Hypothesis(t, "r", "p") for t, _ in POP]


def test_population_screens_and_stacks_compatible_winners(repo, tmp_path):
    ev, calls, hyps = _population(repo, tmp_path)
    s = ev.generation(hypotheses=hyps, parallel=3, screen_tasks=["a", "d"])
    led = {e["branch"].split("-", 2)[-1]: e for e in ev.ledger() if e["kind"] == "candidate"}
    assert led["break-tests"]["stage"] == "tests"
    assert led["break-d"]["stage"] == "screen" and "regressed" in led["break-d"]["reason"]
    stack = next(e for e in ev.ledger() if e["kind"] == "stack")
    assert set(stack["included"]) == {"evolve/g1-1-fix-a", "evolve/g1-2-fix-b", "evolve/g1-3-fix-c-one"}
    assert stack["excluded"] == {"evolve/g1-4-fix-c-two": "merge conflict"}
    assert s["accepted"] == stack["included"] and stack["merged"]
    # the merged champion carries all three independent improvements
    assert _pop_suite(repo)["summary"]["pass_rate"] == 1.0
    assert "evolve/gen-1" in git(repo, "branch", "--list", "evolve/gen-1")
    # screening ran on the subset only; full benchmarks only for survivors (+ baseline + stack)
    screens = [c for c in calls if "screen" in c[0]]
    assert len(screens) == 5 and all(c[1] == ("a", "d") for c in screens)  # all that passed tests
    assert len([c for c in calls if c[0].startswith("g1-") and "screen" not in c[0] and "stack" not in c[0]]) == 4


def test_population_finalists_cutoff_and_budget(repo, tmp_path):
    ev, calls, hyps = _population(repo, tmp_path)
    ev.generation(hypotheses=hyps, parallel=2, screen_tasks=["a", "d"], finalists=1)
    led = [e for e in ev.ledger() if e["kind"] == "candidate"]
    assert sum(1 for e in led if e.get("stage") == "benchmarked") == 1
    assert any("screened out" in e["reason"] for e in led)
    ev2, _, hyps2 = _population(repo, tmp_path / "second")
    s = ev2.generation(hypotheses=hyps2, parallel=1, budget_usd=0.15)
    stages = s["stages"]
    assert stages["skipped"] >= 4  # budget stopped new implementations after the first one or two


def test_everything_reaches_the_remote_and_a_fresh_home_restores(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    ev = make(repo, tmp_path, [_suite(0.5, 1.0), _suite(0.0, 1.0), _suite(1.0, 1.0)])
    ev.generation(hypotheses=[Hypothesis("worse idea", "r", "p"), Hypothesis("better idea", "r", "p")])
    heads = git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert {"main", "evolve/g1-1-worse-idea", "evolve/g1-2-better-idea", "evolve/gen-1", "evolve/journal"} <= set(heads.split())
    assert git(remote, "rev-parse", "main") == git(repo, "rev-parse", "main")
    files = git(remote, "ls-tree", "-r", "--name-only", "evolve/journal").split()
    assert "ledger.jsonl" in files and "state.json" in files and any(f.startswith("baseline/") for f in files)
    # the main working tree was never touched by the journal commits
    assert not git(repo, "status", "--porcelain")
    # a reclaimed container: new clone, empty evolution home -> the ledger comes back from the remote
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    ev2 = make(clone, tmp_path / "fresh", [])
    kinds = [e["kind"] for e in ev2.ledger()]
    assert kinds.count("candidate") == 2 and "generation" in kinds and ev2.state["generation"] == 1


class _Crash(BaseException):
    """Simulates the process dying (not an ordinary candidate error)."""


def test_interrupted_generation_resumes_without_redoing_finished_candidates(repo, tmp_path):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    calls = []

    def impl(h, wt):
        calls.append(h.title)
        (wt / f"{slug(h.title)}.txt").write_text("partial" if h.title == "two" else "done", encoding="utf-8")
        if h.title == "two" and calls.count("two") == 1:
            raise _Crash()
        return {"cost_usd": 0.0}

    def bench_fn(code, label, tasks=None):
        ok = (Path(code) / "two.txt").exists()
        rows = [{"id": "a", "pass": ok, "partial": float(ok), "cost_usd": 0.1}]
        return {"results": rows, "summary": bench.summarize(rows)}

    hyps = [Hypothesis("one", "r", "p"), Hypothesis("two", "r", "p")]
    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    with pytest.raises(_Crash):
        ev.generation(hypotheses=hyps)
    assert ev.state["running"]["plan"]["branches"] == {"1": "evolve/g1-1-one", "2": "evolve/g1-2-two"}
    # a new process (container restart) picks the generation up
    ev2 = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    s = ev2.resume()
    assert calls == ["one", "two", "two"]  # "one" was committed before the crash and is not redone
    assert s["generation"] == 1 and s["accepted"] and "running" not in ev2.state
    assert ev2.state["generation"] == 1 and any(e["kind"] == "resume" for e in ev2.ledger())
    assert ev2.resume() is None


def test_provider_limit_pauses_instead_of_rejecting(repo, tmp_path):
    from artel.evolve.engine import InfraError, infra_problem

    e = infra_problem("planning failed: error claude code failed: You've hit your session limit · resets 11:20am (UTC)")
    assert isinstance(e, InfraError) and e.retry_at and "session limit" in str(e)
    assert infra_problem("AssertionError: expected 3 got 4") is None
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    limited = {"on": True}

    def bench_fn(code, label, tasks=None):
        if limited["on"] and not label.startswith("base-"):
            rows = [{"id": "a", "pass": False, "partial": 0.0, "error": "You've hit your session limit"}]
        else:
            rows = [{"id": "a", "pass": True, "partial": 1.0, "cost_usd": 0.1 if label.startswith("base-") else 0.05}]
        return {"results": rows, "summary": bench.summarize(rows)}

    impls = []
    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn,
                   implement_fn=lambda h, wt: impls.append(h.title) or (wt / "x.txt").write_text("1", encoding="utf-8"))
    with pytest.raises(InfraError):
        ev.generation(hypotheses=[Hypothesis("cheaper", "r", "p")])
    assert not [x for x in ev.ledger() if x["kind"] == "candidate"]  # nobody was judged on a provider failure
    assert ev.state["running"]["phase"].startswith("paused") and ev.ledger()[-1]["kind"] == "paused"
    limited["on"] = False
    s = ev.resume()
    assert impls == ["cheaper"] and s["accepted"] == ["evolve/g1-1-cheaper"]


def test_reopen_voids_decisions_and_remeasures_without_reimplementing(repo, tmp_path):
    scores = iter([_suite(0.5, 1.0), _suite(0.0, 1.0), _suite(1.0, 1.0)])
    impls = []
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=lambda code, label: next(scores),
                   implement_fn=lambda h, wt: impls.append(h.title) or (wt / "y.txt").write_text("1", encoding="utf-8") and {})
    assert ev.generation(hypotheses=[Hypothesis("good idea", "r", "p")])["accepted"] == []
    ev.reopen(1, "provider outage")
    s = ev.resume()
    assert impls == ["good idea"] and s["generation"] == 1 and s["accepted"] == ["evolve/g1-1-good-idea"]
    assert any(e["kind"] == "voided" for e in ev.ledger())


def test_adopt_policy_takes_neutral_features_and_rejects_only_damage():
    p = Policy()
    same = bench.compare(_suite(1.0, 1.0), _suite(1.0, 1.1))
    assert p.adopt(same, {"ok": True}, []).accepted  # no gain needed for a planned feature
    assert not p.decide(same, {"ok": True}).accepted  # ... while a free hypothesis must show a gain
    assert not p.adopt(same, {"ok": True}, ["b"]).accepted
    assert not p.adopt(bench.compare(_suite(1.0, 1.0), _suite(1.0, 1.4)), {"ok": True}, []).accepted
    assert not p.adopt(same, {"ok": False, "summary": "1 failed"}, []).accepted


def test_adopt_combination_failure_is_incremental_not_all_or_nothing(repo, tmp_path):
    """Each item is fine alone but together they cost too much: adopt the cheapest, defer the other."""
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})

    def bench_fn(code, label, tasks=None):
        code = Path(code)
        n = sum((code / f).exists() for f in ("x.txt", "y.txt"))
        solo_cost = 1.05 if (code / "x.txt").exists() else 1.10
        cost = 1.0 if n == 0 else (solo_cost if n == 1 else 2.0)
        rows = [{"id": "a", "pass": True, "partial": 1.0, "cost_usd": cost}]
        return {"results": rows, "summary": bench.summarize(rows)}

    def impl(h, wt):
        (wt / ("x.txt" if h.title == "x" else "y.txt")).write_text("1", encoding="utf-8")
        return {}

    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    s = ev.generation(hypotheses=[Hypothesis("x", "r", "p"), Hypothesis("y", "r", "p")],
                      plan_extra={"mode": "adopt"})
    led = {e["hypothesis"]["title"]: e for e in ev.ledger() if e["kind"] == "candidate"}
    assert s["accepted"] == ["evolve/g1-1-x"] and led["x"]["merged"]
    assert led["y"]["stage"] == "deferred" and "combination" in led["y"]["reason"]


def test_gates_leave_no_python_debris(tmp_path, monkeypatch):
    from artel.manager import Manager, new_kernel, run_goal

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("gates", cfg)
    (project.shared / "m.py").write_text("X = 1\n", encoding="utf-8")
    m = Manager(new_kernel(cfg), project)
    res = m.run_gates({"id": "t", "checks": ["python3 -c 'import m'"]})
    assert res[0]["exit"] == 0 and not (project.shared / "__pycache__").exists()


def test_unfinished_implementation_is_never_merged(repo, tmp_path):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})

    def impl(h, wt):
        (wt / "half.txt").write_text("half a feature", encoding="utf-8")
        return {"status": "waiting_human", "cost_usd": 0.0}

    def bench_fn(code, label, tasks=None):
        rows = [{"id": "a", "pass": True, "partial": 1.0, "cost_usd": 0.1}]
        return {"results": rows, "summary": bench.summarize(rows)}

    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    s = ev.generation(hypotheses=[Hypothesis("half", "r", "p")], plan_extra={"mode": "adopt"})
    e = next(x for x in ev.ledger() if x["kind"] == "candidate")
    assert s["accepted"] == [] and e["stage"] == "unfinished" and "waiting_human" in e["reason"]
    assert not (repo / "half.txt").exists() and git(repo, "branch", "--list", "evolve/g1-1-half")


def test_final_merge_conflict_with_a_moved_main_never_breaks_main(repo, tmp_path):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})

    def impl(h, wt):
        (wt / "artel" / "__init__.py").write_text('"""from the candidate"""\n', encoding="utf-8")
        # a person changes the same file on main while the generation runs
        (repo / "artel" / "__init__.py").write_text('"""by hand on main"""\n', encoding="utf-8")
        git(repo, "commit", "-qam", "hand change")
        return {}

    def bench_fn(code, label, tasks=None):
        rows = [{"id": "a", "pass": True, "partial": 1.0, "cost_usd": 0.1}]
        return {"results": rows, "summary": bench.summarize(rows)}

    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    s = ev.generation(hypotheses=[Hypothesis("clash", "r", "p")], plan_extra={"mode": "adopt"})
    assert s["accepted"] == [] and not git(repo, "status", "--porcelain", "--untracked-files=no")
    assert "by hand on main" in (repo / "artel" / "__init__.py").read_text(encoding="utf-8")
    e = next(x for x in ev.ledger() if x["kind"] == "candidate")
    assert e["stage"] == "deferred" and any(x["kind"] == "merge_conflict" for x in ev.ledger())
