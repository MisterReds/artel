"""W2-worktree-isolation: per-task worktrees, merge on acceptance, clean discard, conflicts never overwrite."""

import shutil

import pytest

from artel.agent import Outcome
from artel.manager import Manager, new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.tooling import ToolContext, ToolError
from artel.worktree import Isolation

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git missing")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05,
                                       "isolation": {"worktrees": True}})


def _project(cfg, tmp_path):
    project = Project.create("g", cfg, name="iso", workdir=str(tmp_path / "wd"))
    (project.shared / "base.txt").write_text("line1\nline2\nline3\n")
    return project


def test_parallel_tasks_work_apart_and_merge_on_acceptance(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    iso = Isolation(project)
    a, b = iso.ensure("A"), iso.ensure("B")
    assert a != b and (a / "base.txt").read_text() == (b / "base.txt").read_text()
    (a / "a.txt").write_text("from A")
    (b / "b.txt").write_text("from B")
    (b / "base.txt").write_text("line1\nline2 by B\nline3\n")
    assert not (project.shared / "a.txt").exists() and not (b / "a.txt").exists()  # nothing leaks before review
    assert iso.accept("A") == (True, [])
    assert (project.shared / "a.txt").read_text() == "from A" and not (project.shared / "b.txt").exists()
    assert iso.accept("B") == (True, [])  # main moved: B is merged onto the updated base, A's file kept
    assert (project.shared / "a.txt").read_text() == "from A"
    assert (project.shared / "b.txt").read_text() == "from B"
    assert "line2 by B" in (project.shared / "base.txt").read_text()
    assert not a.exists() and not b.exists()  # merged worktrees are removed
    assert not (project.shared / ".git").exists()  # the deliverables get no git dir of ours


def test_rejected_attempt_is_discarded_cleanly(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    iso = Isolation(project)
    wt = iso.ensure("T")
    (wt / "base.txt").write_text("broken")
    (wt / "junk").mkdir()
    (wt / "junk" / "x.txt").write_text("x")
    iso.commit("T")
    assert iso.discard("T")
    assert (wt / "base.txt").read_text() == "line1\nline2\nline3\n" and not (wt / "junk").exists()
    assert (project.shared / "base.txt").read_text() == "line1\nline2\nline3\n"
    iso.drop("T")
    assert not wt.exists()


def test_conflict_is_reported_and_resolved_by_retry_on_updated_base(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    iso = Isolation(project)
    a, b = iso.ensure("A"), iso.ensure("B")
    (a / "base.txt").write_text("line1\nA wins\nline3\n")
    (b / "base.txt").write_text("line1\nB wins\nline3\n")
    assert iso.accept("A") == (True, [])
    merged, conflicts = iso.accept("B")
    assert not merged and conflicts == ["base.txt"]
    assert "A wins" in (project.shared / "base.txt").read_text()  # never silently overwritten
    text = (b / "base.txt").read_text()
    assert "<<<<<<< " in text and "A wins" in text and "B wins" in text  # updated base + markers in B's worktree
    assert iso.integrate("B") == ["base.txt"]  # unresolved markers keep it from merging
    (b / "base.txt").write_text("line1\nA wins and B wins\nline3\n")
    assert iso.accept("B") == (True, [])
    assert (project.shared / "base.txt").read_text() == "line1\nA wins and B wins\nline3\n"


def test_outside_changes_to_shared_become_the_base(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    iso = Isolation(project)
    wt = iso.ensure("A")
    (wt / "a.txt").write_text("A")
    (project.shared / "human.txt").write_text("edited by a person")
    assert iso.accept("A") == (True, [])
    assert (project.shared / "human.txt").read_text() == "edited by a person"
    assert (project.shared / "a.txt").read_text() == "A"


def test_tools_write_into_the_task_worktree_only(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    k = new_kernel(cfg)
    iso = Isolation(project)
    wt = iso.ensure("A")
    ctx = ToolContext(k, project, role="worker", workspace=tmp_path / "ws", deliverables=wt)
    (tmp_path / "ws").mkdir()
    k.tools["write_file"].fn(ctx, path="shared/new.md", content="x")
    assert (wt / "new.md").exists() and not (project.shared / "new.md").exists()
    with pytest.raises(ToolError, match="merged deliverables"):
        k.tools["write_file"].fn(ctx, path=str(project.shared / "evil.md"), content="x")
    with pytest.raises(ToolError):
        k.tools["write_file"].fn(ctx, path=str(iso.path("OTHER") / "evil.md"), content="x")
    out = k.tools["bash"].fn(ctx, command='echo "$ARTEL_SHARED"')
    assert str(wt) in out


def test_manager_review_conflict_goes_back_to_worker_with_comment(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    m = Manager(new_kernel(cfg), project)
    t1 = project.board.add_task("t1", "b", criteria=["c"], assignee="Ada")
    t2 = project.board.add_task("t2", "b", criteria=["c"], assignee="Bob")
    for tid, word in ((t1, "one"), (t2, "two")):
        (m.iso.ensure(tid) / "base.txt").write_text(f"line1\n{word}\nline3\n")
        project.board.update(tid, status="review", actor="test")
    ok = Outcome(status="terminal", terminal=("review_verdict", {"passed": True, "feedback": "good"}))
    m.on_review(t1, ([], ok))
    assert project.board.get(t1)["status"] == "done"
    assert "one" in (project.shared / "base.txt").read_text()
    m.on_review(t2, ([], ok))  # accepted, but the base moved under it in the same lines
    t = project.board.get(t2)
    assert t["status"] == "ready" and t["attempts"] == 1
    body = project.board.comments(t2)[-1]["body"]
    assert "MERGE CONFLICT" in body and "base.txt" in body
    assert "one" in (project.shared / "base.txt").read_text()
    assert "manager/merge_conflict" in [e["kind"] for e in project.log.read()]
    # a later review before resolving reports the conflict instead of running the reviewer
    project.board.update(t2, status="review", actor="test")
    gates, out = m.job_review(project.board.get(t2))
    assert out.name == "merge_conflict" and out.payload["conflicts"] == ["base.txt"]


def test_manager_start_over_discards_the_worktree(cfg, tmp_path):
    project = _project(cfg, tmp_path)
    m = Manager(new_kernel(cfg), project)
    tid = project.board.add_task("t", "b", criteria=["c"], assignee="Ada")
    wt = m.iso.ensure(tid)
    (wt / "junk.txt").write_text("x")
    project.board.update(tid, status="review", actor="test")
    m.on_review(tid, ([], Outcome(status="terminal", terminal=("review_verdict",
                                                                {"passed": False, "feedback": "START OVER"}))))
    assert not (wt / "junk.txt").exists() and not (project.shared / "junk.txt").exists()
    assert "manager/worktree_discard" in [e["kind"] for e in project.log.read()]


def test_full_run_with_isolation_delivers_into_shared(cfg):
    project, status = run_goal("Isolated delivery", cfg)
    assert status == "done"
    for f in ("notes.md", "deliverable.md", "report.md"):
        assert (project.shared / f).exists(), f
    kinds = [e["kind"] for e in project.log.read()]
    assert "manager/worktree" in kinds and "manager/merged" in kinds
    assert not any((project.root / "worktrees").iterdir())  # every accepted worktree was merged and removed
