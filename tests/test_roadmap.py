from pathlib import Path

from artel.evolve import bench
from artel.evolve.engine import Evolution, git
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.roadmap import Roadmap

ROOT = Path(__file__).resolve().parents[1]

TOY = """# toy roadmap — this comment must survive status updates
pillars:
  - id: P1-x
    title: Pillar
    source: {harness: H, repo: a/b}
    status: done

items:
  # ---- wave 1 ----
  - id: W1-a
    wave: 1
    mode: evolve
    status: planned
    title: Feature A
    source: {harness: Donor, repo: DONOR, paths: [README.md]}
    target: [artel/a.py]
    plan: >-
      Port feature A.
    acceptance: [tests]
  - id: W1-b
    wave: 1
    mode: evolve
    status: planned
    title: Feature B breaks things
    source: {harness: Donor, repo: DONOR}
  - id: W1-d
    wave: 1
    mode: design
    status: planned
    title: Structural
  - id: W2-c
    wave: 2
    mode: evolve
    status: planned
    after: [W1-a]
    title: Feature C needs A
"""


def _toy(tmp_path, donor="o/donor"):
    p = tmp_path / "roadmap.yaml"
    p.write_text(TOY.replace("DONOR", donor))
    return p


def test_real_roadmap_is_consistent_and_rendered():
    rm = Roadmap.of_repo(ROOT)
    ids = [it["id"] for it in rm.pillars + rm.items]
    assert len(ids) == len(set(ids))
    for it in rm.items:
        assert it["mode"] in ("evolve", "design") and it["status"] in ("planned", "partial", "in_progress", "done")
        assert (it.get("source") or {}).get("repo"), it["id"]
        for dep in it.get("after", []):
            rm.get(dep)
    assert {p["id"].split("-", 1)[1] for p in rm.pillars} >= {"modularity", "skills", "goal-loop"}
    assert all(p["status"] == "done" for p in rm.pillars)
    assert (ROOT / "docs" / "ROADMAP.md").read_text() == rm.render_markdown(), "run `artel roadmap render`"


def test_next_items_order_modes_and_dependencies(tmp_path):
    rm = Roadmap(_toy(tmp_path))
    assert [it["id"] for it in rm.next_items(5)] == ["W1-a", "W1-b"]  # design skipped, W2-c blocked by W1-a
    assert [it["id"] for it in rm.next_items(5, mode="design")] == ["W1-d"]
    rm.set_status("W1-a", "done", "merged in gen 1")
    assert [it["id"] for it in rm.next_items(5)] == ["W1-b", "W2-c"]
    text = rm.path.read_text()
    assert "# toy roadmap — this comment must survive" in text and "# ---- wave 1 ----" in text
    assert rm.get("W1-a")["result"] == "merged in gen 1" and rm.get("W1-b")["status"] == "planned"
    rm.set_status("W1-a", "done", "again")  # idempotent: one result line
    assert text.count("result:") == 1 and rm.path.read_text().count("result:") == 1
    assert "Волна 1 — 1/3 готово" in rm.render_markdown()


def test_hypothesis_carries_donor_sources_and_license(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "README.md").write_text("donor")
    (donor / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge")
    rm = Roadmap(_toy(tmp_path, str(donor)))
    h = rm.to_hypothesis(rm.get("W1-a"), tmp_path / "sources")
    assert h.source == "roadmap:W1-a" and h.refs == [str(donor)] and h.files == ["artel/a.py"]
    assert "MIT" in h.license_note and str(donor / "README.md") in h.license_note
    (tmp_path / "m").mkdir()
    missing = Roadmap(_toy(tmp_path / "m", "/nonexistent/x"))
    h2 = missing.to_hypothesis(missing.get("W1-b"), tmp_path / "sources")
    assert h2.refs == [] and "could not be fetched" in h2.license_note and "Feature B" in h2.plan


def test_roadmap_generation_merges_marks_done_and_reuses_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "README.md").write_text("donor")
    repo = tmp_path / "selfrepo"
    (repo / "artel").mkdir(parents=True)
    (repo / "artel" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    _toy(repo, str(donor))
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        git(repo, *args)

    calls = []

    def bench_fn(code, label, tasks=None):
        calls.append(label)
        ok = {"a": (Path(code) / "artel" / "a.py").exists(), "b": not (Path(code) / "broken").exists()}
        rows = [{"id": k, "pass": v, "partial": float(v), "cost_usd": 0.1} for k, v in ok.items()]
        return {"results": rows, "summary": bench.summarize(rows)}

    repairs = []

    def impl(h, wt):
        assert h.refs == [str(donor)]
        if "REPAIR" in h.plan:  # the team gets the failing checks back and tries again (here: fails to fix it)
            repairs.append(h.title)
            (wt / "broken").write_text(f"attempt {len(repairs)}\n")
        else:
            (wt / ("artel/a.py" if "W1-a" in h.title else "broken")).write_text("x = 1\n")
        return {"cost_usd": 0.0}

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    s = ev.roadmap_generation(2)
    # planned items are adopted, not ranked: W1-a goes in; W1-b breaks a task, goes back to its team twice with
    # the evidence, still breaks it, and is flagged for a human decision instead of silently dropped
    assert s["roadmap"] == {"W1-a": "done", "W1-b": "blocked"}
    assert len(repairs) == 2
    rm = Roadmap.of_repo(repo)
    assert rm.get("W1-a")["status"] == "done" and "evolve/gen-1" in rm.get("W1-a")["result"]
    assert "breaks tasks: b" in rm.get("W1-b")["result"] and "needs a design decision" in rm.get("W1-b")["result"]
    assert "нужна помощь" in (repo / "docs" / "ROADMAP.md").read_text()
    assert "roadmap: gen 1" in git(repo, "log", "-1", "--format=%s")
    assert (repo / "docs" / "ROADMAP.md").exists() and (repo / "artel" / "a.py").exists()
    assert git(repo, "rev-parse", "evolve/gen-1") == git(repo, "rev-parse", "HEAD")
    # next pick follows dependencies; the roadmap-only commit reuses the merged code's baseline
    n_base = sum(1 for c in calls if c.startswith("base-"))
    s2 = ev.roadmap_generation(5)
    assert set(s2["roadmap"]) == {"W2-c"}  # blocked items wait for a decision; W2-c's dependency is done
    assert sum(1 for c in calls if c.startswith("base-")) == n_base + 1  # merged code is new; measured once
    rm = Roadmap.of_repo(repo)
    rm.set_status("W1-b", "planned", "redesigned by hand")
    ev.roadmap_generation(5)  # gen 2 merged W2-c ... its roadmap-only commit must not trigger an extra baseline
    assert sum(1 for c in calls if c.startswith("base-")) <= n_base + 2


def _selfrepo(tmp_path, donor):
    repo = tmp_path / "selfrepo"
    (repo / "artel").mkdir(parents=True)
    (repo / "artel" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    _toy(repo, str(donor))
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        git(repo, *args)
    return repo


def test_absorb_turns_findings_into_roadmap_items_and_cycle_follows_the_plan(tmp_path, monkeypatch):
    from artel.roadmap import ABSORB_WAVE, load_studied, next_to_study

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "README.md").write_text("donor")
    other = tmp_path / "otherharness"
    other.mkdir()
    (other / "README.md").write_text("Other harness with a marker feature")
    (other / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, ...")
    repo = _selfrepo(tmp_path, donor)
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    impls = []

    def impl(h, wt):
        impls.append(h.source)
        (wt / "artel" / "a.py").write_text("x = 1\n")
        return {"cost_usd": 0.0}

    def bench_fn(code, label, tasks=None):
        ok = (Path(code) / "artel" / "a.py").exists()
        rows = [{"id": "a", "pass": ok, "partial": float(ok), "cost_usd": 0.1}]
        return {"results": rows, "summary": bench.summarize(rows)}

    ev = Evolution(repo, tmp_path / "evo", cfg, cfg, bench_fn=bench_fn, implement_fn=impl)
    out = ev.absorb(str(other))  # the scripted absorber reports one feature, "marker", fit 0.9
    assert out["roadmap_added"] == ["A-otherharness-marker"] and "generation" not in out
    rm = Roadmap.of_repo(repo)
    item = rm.get("A-otherharness-marker")
    assert item["wave"] == ABSORB_WAVE and item["mode"] == "evolve" and item["source"]["license"] == "MIT"
    assert item["source"]["paths"] == ["README.md"] and item["plan"] == "write ABSORBED.md"
    assert "# toy roadmap — this comment must survive" in rm.path.read_text()
    assert load_studied(repo)[0]["added"] == ["A-otherharness-marker"]
    assert "roadmap: absorbed" in git(repo, "log", "-1", "--format=%s")
    assert "Изученные проекты — 1" in (repo / "docs" / "ROADMAP.md").read_text()
    assert ev.absorb(str(other))["roadmap_added"] == []  # studying again adds no duplicates

    # registry: studied projects and roadmap donors are skipped; most relevant live harness first
    reg = [{"repo": str(other), "status": "alive", "category": "harness", "relevance": 30},
           {"repo": str(donor), "status": "alive", "category": "harness", "relevance": 29},
           {"repo": "x/list", "status": "alive", "category": "list", "relevance": 28},
           {"repo": "x/dead", "status": "dead", "category": "harness", "relevance": 27},
           {"repo": "x/low", "status": "alive", "category": "orchestrator", "relevance": 3},
           {"repo": "x/high", "status": "alive", "category": "orchestrator", "relevance": 20}]
    assert [h["repo"] for h in next_to_study(repo, reg, n=5)] == ["x/high", "x/low"]

    # a cycle studies nothing new here (registry patched to the already studied source) and implements the
    # next items in plan order: wave 1 first, the absorbed wave-4 item waits its turn
    monkeypatch.setattr("artel.roadmap.next_to_study", lambda repo, n=1: [])
    s = ev.cycle(items=1)
    assert impls == ["roadmap:W1-a"] and s["generation"]["roadmap"] == {"W1-a": "done"}
    # --run jumps the queue explicitly for the freshly absorbed items
    out = ev.absorb(str(other), run=True)
    assert out["roadmap_added"] == []
    s = ev.roadmap_generation(1, ids=["A-otherharness-marker"])
    assert impls[-1] == "roadmap:A-otherharness-marker"
