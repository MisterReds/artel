import shutil
import subprocess

import pytest

from artel.snapshot import Snapshot

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git missing")


def _setup(tmp_path):
    wt = tmp_path / "work"
    (wt / "sub").mkdir(parents=True)
    (wt / "a.txt").write_text("A", encoding="utf-8")
    (wt / "sub" / "b.txt").write_text("B", encoding="utf-8")
    return wt, Snapshot(tmp_path / "snapshot.git", wt)


def test_restore_reverts_modified_deleted_and_added(tmp_path):
    wt, snap = _setup(tmp_path)
    tree = snap.track()
    assert tree
    (wt / "a.txt").write_text("changed", encoding="utf-8")
    (wt / "sub" / "b.txt").unlink()
    (wt / "new.txt").write_text("N", encoding="utf-8")
    (wt / "newdir").mkdir()
    (wt / "newdir" / "c.txt").write_text("C", encoding="utf-8")
    assert sorted(snap.diff_files(tree)) == ["a.txt", "new.txt", "newdir/c.txt", "sub/b.txt"]
    assert snap.restore(tree) is True
    assert (wt / "a.txt").read_text(encoding="utf-8") == "A"
    assert (wt / "sub" / "b.txt").read_text(encoding="utf-8") == "B"
    assert not (wt / "new.txt").exists()
    assert not (wt / "newdir").exists()
    assert snap.diff_files(tree) == []


def test_workdir_git_untouched_and_caches_excluded(tmp_path):
    wt, snap = _setup(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    before = sorted(p.relative_to(wt / ".git").as_posix() for p in (wt / ".git").rglob("*"))
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
    tree = snap.track()
    (wt / "a.txt").write_text("zzz", encoding="utf-8")
    snap.restore(tree)
    after = sorted(p.relative_to(wt / ".git").as_posix() for p in (wt / ".git").rglob("*"))
    assert before == after
    assert (wt / "__pycache__" / "x.pyc").exists()
    assert "x.pyc" not in subprocess.run(
        ["git", f"--git-dir={snap.git_dir}", "ls-tree", "-r", "--name-only", tree],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def test_failure_degrades(tmp_path):
    snap = Snapshot(tmp_path / "s.git", tmp_path / "missing")
    assert snap.track() is None
    assert snap.restore("deadbeef") is False
    assert snap.diff_files("deadbeef") is None
    wt, snap = _setup(tmp_path)
    assert snap.restore("0" * 40) is False


# --- manager wiring ---------------------------------------------------------
from artel.agent import Outcome  # noqa: E402
from artel.manager import Manager, new_kernel, wants_start_over  # noqa: E402
from artel.project import DEFAULT_CONFIG, Project, deep_merge  # noqa: E402


@pytest.mark.parametrize("text", ["START OVER", "Please start over.", "start from scratch with a new approach",
                                  "revert the changes", "Discard this attempt"])
def test_wants_start_over_positive(text):
    assert wants_start_over(text)


@pytest.mark.parametrize("text", ["Add more detail to the deliverable.", "", "The overview section is overdue",
                                  "restart the server and rerun tests"])
def test_wants_start_over_negative(text):
    assert not wants_start_over(text)


def _manager(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("g", cfg, name="snap", workdir=str(tmp_path / "wd"))
    (project.shared / "a.txt").write_text("good", encoding="utf-8")
    m = Manager(new_kernel(cfg), project)
    tid = project.board.add_task("t", "b", criteria=["c"], assignee="Ada")
    return m, project, tid


def _reject(m, tid, feedback):
    # simulate: snapshot taken before the run, worker damaged files, reviewer rejects
    m.snapshots[tid] = m.snapper.track()
    (m.project.shared / "a.txt").write_text("broken", encoding="utf-8")
    (m.project.shared / "junk.txt").write_text("x", encoding="utf-8")
    m.project.board.update(tid, status="review", actor="test")
    out = Outcome(status="terminal", terminal=("review_verdict", {"passed": False, "feedback": feedback}))
    m.on_review(tid, ([], out))


def test_start_over_rejection_restores_files(tmp_path, monkeypatch):
    m, project, tid = _manager(tmp_path, monkeypatch)
    _reject(m, tid, "Wrong approach. START OVER.")
    assert (project.shared / "a.txt").read_text(encoding="utf-8") == "good"
    assert not (project.shared / "junk.txt").exists()
    assert any("restored deliverables" in c["body"] for c in project.board.comments(tid))
    assert "manager/snapshot_restore" in [e["kind"] for e in project.log.read()]
    assert tid not in m.snapshots


def test_ordinary_rejection_leaves_files(tmp_path, monkeypatch):
    m, project, tid = _manager(tmp_path, monkeypatch)
    _reject(m, tid, "Add more detail to the deliverable.")
    assert (project.shared / "a.txt").read_text(encoding="utf-8") == "broken"
    assert (project.shared / "junk.txt").exists()
    assert "manager/snapshot_restore" not in [e["kind"] for e in project.log.read()]
