import json

import pytest

from artel.manager import human_comment, new_kernel, resume, run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


def kinds(project):
    return [e["kind"] for e in project.log.read()]


def test_goal_to_done(cfg):
    # a rejection plus a low threshold exercise the librarian and refiner on every task
    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1}, "skills": {"review_after_tool_calls": 1}})
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done"
    snap = project.board.snapshot()
    work = [t for t in snap["tasks"] if t["kind"] == "work"]
    assert len(work) == 3 and all(t["status"] == "done" for t in work)
    assert (project.shared / "report.md").read_text().count("VERIFIED") == 1
    # personas with souls and private workspaces
    names = [p.name for p in project.personas()]
    assert names == ["Ada", "Linus"]
    assert "Curious and exact" in project.worker_files("Ada")["SOUL.md"]
    assert "Deliverables live in shared/" in project.worker_files("Linus")["MEMORY.md"]
    # transparency: requests, tool calls, gates, verdicts, audit all logged
    k = kinds(project)
    for needed in ("agent/start", "llm/response", "tool/call", "gate/result", "review/verdict", "audit/verdict",
                   "skills/reviewed", "harness/edited"):
        assert needed in k, needed
    # skills were created then improved by the librarian (Hermes loop)
    skills = new_kernel(cfg).get("skills")
    led = [e["action"] for e in skills.ledger("shared-deliverable-workflow")]
    assert led[0] == "create" and "patch" in led
    assert "Re-run the checks" in skills.view("shared-deliverable-workflow")
    # continual harness note written with a rollback journal
    assert "Run every gate command" in project.prompt_notes()


def test_rejection_stall_and_audit_rounds(cfg):
    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1, "audit_missing": 1, "idle_task": "Build the deliverable"}})
    project, status = run_goal("Ship a tiny CLI", cfg)
    assert status == "done"
    tasks = {t["title"]: t for t in project.board.list()}
    assert tasks["Write a changelog"]["status"] == "done"          # added by the audit, then completed
    assert project.meta["audit_rounds"] == 2
    build_runs = project.board.runs(tasks["Build the deliverable"]["id"])
    assert build_runs[0]["status"] == "stalled" and build_runs[-1]["status"] == "submitted"
    assert "agent/continuation" in kinds(project)
    comments = [c["body"] for t in project.board.list() for c in project.board.comments(t["id"])]
    assert any(c.startswith("REJECTED") for c in comments)


def test_human_unblock_and_resume(cfg):
    from artel.manager import Manager

    kernel = new_kernel(cfg)
    project = Project.create("Needs a human", cfg)
    m = Manager(kernel, project)
    m.plan()
    first = project.board.list(kind="work")[0]
    project.board.update(first["id"], status="blocked", needs_human=1)
    assert m.run() == "waiting_human"
    human_comment(project, first["id"], "Credentials are in place now.")
    _, status = resume(project.slug, cfg)
    assert status == "done"


def test_refine_rollback(cfg):
    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1}})
    project, _ = run_goal("Anything", cfg)
    refine = new_kernel(cfg).get("refine")
    edit = refine.history(project)[0]
    refine.rollback(project, edit["id"])
    assert not project.prompt_notes_path.exists()


def test_portfolio_and_agent_analytics(cfg):
    from artel import analytics

    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1}})
    run_goal("Project one", cfg)
    run_goal("Project two", cfg)
    from artel.manager import Manager

    stuck = Project.create("Project three", cfg)
    Manager(new_kernel(cfg), stuck).plan()
    t = stuck.board.list(kind="work")[0]
    stuck.board.update(t["id"], status="blocked", needs_human=1)

    pf = analytics.portfolio(cfg)
    assert len(pf) == 3
    by_name = {p["name"]: p for p in pf}
    assert by_name["Project one"]["stage"] == "done" and by_name["Project one"]["progress"] == 1.0
    three = by_name["Project three"]
    assert three["attention"] and three["attention"][0]["reason"] == "нужен человек"
    a = analytics.agent_analytics(pf)
    model = a["by_model"][0]
    assert model["key"] == "scripted" and model["done"] == 6 and model["projects"] == 3
    # each run_goal builds its own scripted provider, so each project rejects its first review once
    assert model["rejections"] == 2 and model["first_pass_rate"] == round(4 / 6, 3)
    tl = analytics.task_timelines(analytics._events(Project.open(by_name["Project one"]["slug"], cfg)))
    assert any([s["to"] for s in steps][-1] == "done" for steps in tl.values())


def test_clean_run_skips_refiner_and_librarian(cfg):
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done"
    k = kinds(project)
    assert "harness/refine_skipped" in k and "harness/refined" not in k and "skills/reviewed" not in k
    roles = {e.get("role") for e in project.log.read() if e["kind"] == "agent/start"}
    assert not roles & {"refiner", "librarian"}


def _reviewed(project):
    return [e for e in project.log.read() if e["kind"] == "skills/reviewed"]


def test_failure_run_still_refines(cfg):
    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1}})
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done"
    k = kinds(project)
    assert "harness/refined" in k and "harness/refine_skipped" not in k
    roles = {e.get("role") for e in project.log.read() if e["kind"] == "agent/start"}
    assert "refiner" in roles


@pytest.mark.parametrize("threshold,expect_review", [(4, False), (3, True), (1, True)])
def test_librarian_threshold_gating_on_clean_run(cfg, threshold, expect_review):
    # clean tasks use 6-7 tool calls: below 2x threshold (T=4) is skipped, at/above 2x (T=3) runs
    cfg = deep_merge(cfg, {"skills": {"review_after_tool_calls": threshold}})
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done"
    roles = {e.get("role") for e in project.log.read() if e["kind"] == "agent/start"}
    assert bool(_reviewed(project)) is expect_review
    assert ("librarian" in roles) is expect_review


def test_librarian_runs_below_2x_threshold_after_rejection(cfg):
    cfg = deep_merge(cfg, {"scripted": {"review_failures": 1}, "skills": {"review_after_tool_calls": 4}})
    project, status = run_goal("Write a short guide to SQLite WAL mode", cfg)
    assert status == "done"
    assert _reviewed(project)
