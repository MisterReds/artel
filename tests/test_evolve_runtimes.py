"""Evolution across two subscriptions: a provider limit on one runtime moves the work to the other."""

import json
import time
from pathlib import Path

import pytest

from artel.evolve import bench
from artel.evolve.engine import AllLimited, Evolution, Hypothesis, InfraError, git
from artel.failures import retry_at
from artel.project import DEFAULT_CONFIG, artel_home, deep_merge

RTS = ["claude_code", "codex"]
LIMIT_MSG = {"claude_code": "You've hit your session limit · resets 11:20am (UTC)",
             "codex": "You've hit your usage limit. Try again at 9:05 AM"}
BASE_COST = {"claude_code": 1.0, "codex": 2.0}  # the runtimes differ: mixing them would decide wrongly


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


class Bench:
    """Fake benchmark: the candidate (cheaper.txt present) costs 20% less than the champion on the same runtime;
    ``limited`` runtimes answer with a provider limit (baselines too when ``base_too``)."""

    def __init__(self, limited=(), base_too=False):
        self.limited, self.base_too, self.calls = set(limited), base_too, []

    def __call__(self, code, label, tasks=None, runtime=None):
        self.calls.append((label, runtime))
        if runtime in self.limited and (self.base_too or not label.startswith("base-")):
            rows = [{"id": "a", "pass": False, "partial": 0.0, "error": LIMIT_MSG[runtime]}]
        else:
            cost = BASE_COST[runtime] * (0.8 if (Path(code) / "cheaper.txt").exists() else 1.0)
            rows = [{"id": "a", "pass": True, "partial": 1.0, "cost_usd": cost}]
        return {"results": rows, "summary": bench.summarize(rows)}


def _impl(h, wt, runtime=None):
    (wt / "cheaper.txt").write_text("1")
    return {"status": "done", "cost_usd": 0.0}


def _evo(repo, home, bench_fn, impl=_impl):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "evolve": {"runtimes": RTS}})
    return Evolution(repo, home, cfg, cfg, bench_fn=bench_fn, implement_fn=impl)


def test_limit_moves_the_benchmark_to_the_other_runtime_with_its_own_baseline(repo, tmp_path, capsys):
    b = Bench(limited={"claude_code"})
    home = artel_home() / "evolution" / repo.name
    ev = _evo(repo, home, b)
    champ = git(repo, "rev-parse", "HEAD")
    s = ev.generation(hypotheses=[Hypothesis("cheaper", "r", "p")])
    # compared with the codex baseline (2.0 -> 1.6, -20%): accepted; against claude_code's (1.0) it would be +60%
    assert s["accepted"] == ["evolve/g1-1-cheaper"]
    cand = next(e for e in ev.ledger() if e["kind"] == "candidate")
    assert cand["runtime"] == "codex" and cand["summary"]["cost_usd"] == pytest.approx(1.6)
    assert (home / "baseline" / f"{champ}@claude_code.json").exists()
    assert (home / "baseline" / f"{champ}@codex.json").exists()
    assert {e["runtime"] for e in ev.ledger() if e["kind"] == "baseline"} == {"claude_code", "codex"}
    # the limit is remembered until the provider's reset, in state.json
    lim = next(e for e in ev.ledger() if e["kind"] == "limited")
    assert lim["runtime"] == "claude_code" and lim["step"] == "bench"
    saved = json.loads((home / "state.json").read_text())
    assert saved["limits"]["claude_code"] == pytest.approx(retry_at(LIMIT_MSG["claude_code"]), abs=120)
    assert saved["runtime"]["bench"] == "codex"
    # every comparison paired a candidate run with the baseline of the same runtime
    cand_runs = [rt for label, rt in b.calls if label.startswith("g1-")]
    assert cand_runs == ["claude_code", "codex"]
    # `artel evolution` shows the runtime
    (artel_home() / "artel.yaml").write_text("evolve: {runtimes: [claude_code, codex]}\n")
    from artel.cli import main

    assert main(["evolution", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "[codex]" in out and "[claude_code]" in out and "limit    claude_code" in out
    assert "codex" in out.splitlines()[-1] and "claude_code (limited until" in out.splitlines()[-1]


def test_baselines_are_cached_per_runtime(repo, tmp_path):
    b = Bench()
    ev = _evo(repo, tmp_path / "evo", b)
    champ = git(repo, "rev-parse", "HEAD")
    ev.baseline(champ, "claude_code")
    ev.baseline(champ, "codex")
    ev.baseline(champ, "codex")
    assert [c for c in b.calls if c[0].startswith("base-")] == [(f"base-{champ[:8]}-claude_code", "claude_code"),
                                                                (f"base-{champ[:8]}-codex", "codex")]
    # a docs-only commit reuses the baseline of its own runtime only
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "docs")
    new = git(repo, "rev-parse", "HEAD")
    assert ev.baseline(new, "codex")["summary"]["cost_usd"] == pytest.approx(2.0)
    assert len([c for c in b.calls if c[0].startswith("base-")]) == 2
    assert ev._equivalent_baseline(new, "native") is None


def test_all_runtimes_limited_pauses_until_the_earliest_reset_and_survives_a_restart(repo, tmp_path):
    b = Bench(limited=set(RTS), base_too=True)
    ev = _evo(repo, tmp_path / "evo", b)
    with pytest.raises(AllLimited) as exc:
        ev.generation(hypotheses=[Hypothesis("cheaper", "r", "p")])
    earliest = min(retry_at(m) for m in LIMIT_MSG.values())
    assert exc.value.retry_at == pytest.approx(earliest, abs=120)
    paused = ev.ledger()[-1]
    assert paused["kind"] == "paused" and paused["retry_at"] == pytest.approx(earliest, abs=120)
    assert ev.state["running"]["phase"].startswith("paused") and set(ev.state["limits"]) == set(RTS)
    # a restart before the reset: the limits come back from state.json, so it pauses again at once
    b2 = Bench()
    ev2 = _evo(repo, tmp_path / "evo", b2)
    assert set(ev2.limits()) == set(RTS)
    with pytest.raises(AllLimited):
        ev2.resume()
    assert b2.calls == []
    # the limits reset: claude_code (first preference) is used again
    state = json.loads((tmp_path / "evo" / "state.json").read_text())
    state["limits"] = {rt: time.time() - 1 for rt in RTS}
    (tmp_path / "evo" / "state.json").write_text(json.dumps(state))
    ev3 = _evo(repo, tmp_path / "evo", b2)
    s = ev3.resume()
    assert s["accepted"] == ["evolve/g1-1-cheaper"] and ev3.limits() == {}
    assert {rt for _, rt in b2.calls} == {"claude_code"}
    assert next(e for e in ev3.ledger() if e["kind"] == "candidate")["runtime"] == "claude_code"


def test_expired_limit_returns_to_the_preferred_runtime(repo, tmp_path):
    b = Bench()
    ev = _evo(repo, tmp_path / "evo", b)
    ev.state["limits"] = {"claude_code": time.time() + 3600}
    assert ev._pick("bench") == "codex"
    ev.state["limits"]["claude_code"] = time.time() - 1
    assert ev._pick("bench") == "claude_code" and "claude_code" not in ev.state["limits"]


def test_implementation_continues_on_the_other_runtime_in_the_same_worktree(repo, tmp_path):
    seen = []

    def impl(h, wt, runtime=None):
        seen.append(runtime)
        if runtime == "claude_code":
            (wt / "part1.txt").write_text("half")
            raise InfraError("You've hit your session limit", None)
        assert (wt / "part1.txt").exists()  # the other subscription picks up the same worktree
        (wt / "cheaper.txt").write_text("1")
        return {"status": "done", "cost_usd": 0.0}

    ev = _evo(repo, tmp_path / "evo", Bench(), impl)
    s = ev.generation(hypotheses=[Hypothesis("cheaper", "r", "p")])
    assert seen == ["claude_code", "codex"] and s["accepted"] == ["evolve/g1-1-cheaper"]
    cand = next(e for e in ev.ledger() if e["kind"] == "candidate")
    assert cand["implementation"]["runtime"] == "codex"
    files = git(repo, "show", "--stat", "--format=", "evolve/g1-1-cheaper")
    assert "part1.txt" in files and "cheaper.txt" in files
    # a limit without a stated reset waits the default interval
    assert ev.state["limits"]["claude_code"] > time.time() + 600
    # the benchmark also moved: claude_code is limited for every step
    assert cand["runtime"] == "codex"


def test_resumed_implementation_project_runs_on_the_given_runtime(repo, tmp_path, monkeypatch):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "evolve": {"runtimes": RTS},
                                      "runtime_by_role": {"reviewer": "claude_code", "librarian": "native"}})
    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=Bench())
    wt = tmp_path / "wt"
    pdir = artel_home() / "projects" / "half"
    pdir.mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({"workdir": str(wt), "status": "error"}))
    got = {}

    class FakeProject:
        root = pdir
        meta = {"usage": {"cost_usd": 0.0}}

    def fake_resume(slug, config):
        got.update(slug=slug, config=config)
        return FakeProject(), "done"

    import artel.manager

    monkeypatch.setattr(artel.manager, "resume", fake_resume)
    out = ev._continue_implementation(Hypothesis("x", "r", "p"), wt, "codex")
    assert out["resumed"] and got["slug"] == "half"
    assert got["config"]["runtime"] == "codex"
    assert got["config"]["runtime_by_role"] == {"reviewer": "codex", "librarian": "native"}


def test_adopt_mode_switches_and_records_the_runtime(repo, tmp_path):
    ev = _evo(repo, tmp_path / "evo", Bench(limited={"claude_code"}))
    s = ev.generation(hypotheses=[Hypothesis("cheaper", "r", "p")], plan_extra={"mode": "adopt"})
    assert s["accepted"] == ["evolve/g1-1-cheaper"]
    stack = next(e for e in ev.ledger() if e["kind"] == "stack")
    assert stack["runtime"] == "codex" and stack["accepted"]
    assert next(e for e in ev.ledger() if e["kind"] == "candidate")["runtime"] == "codex"


def test_without_runtimes_a_limit_still_pauses(repo, tmp_path):
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    calls = []

    def bench_fn(code, label, tasks=None):
        calls.append(label)
        rows = [{"id": "a", "pass": False, "partial": 0.0, "error": "You've hit your session limit"}]
        return {"results": rows, "summary": bench.summarize(rows)}

    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=_impl)
    with pytest.raises(InfraError) as exc:
        ev.generation(hypotheses=[Hypothesis("cheaper", "r", "p")])
    assert not isinstance(exc.value, AllLimited) and not ev.state.get("limits")
    assert not (tmp_path / "evo" / "baseline").exists() and len(calls) == 1
    assert ev.ledger()[-1]["kind"] == "paused"
