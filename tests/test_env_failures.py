"""Stage B items 3 and 8: `python` resolves in agents' shells; a broken environment is not bad work."""
import os
import subprocess
from pathlib import Path
import sys

import pytest

from artel.manager import Manager, new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


@pytest.fixture
def only_python3(tmp_path, monkeypatch):
    """A machine where `python3` exists and `python` does not (the owner's)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _gates(cfg, tmp_path, checks):
    project = Project.create("g", cfg, name="envp", workdir=str(tmp_path / "wd"))
    mgr = Manager.__new__(Manager)
    mgr.project, mgr.cfg = project, cfg
    return mgr.run_gates({"id": "t1", "checks": checks})


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture")
def test_check_with_python_passes_when_only_python3_exists(cfg, tmp_path, only_python3):
    res = _gates(cfg, tmp_path, ["python -c 'print(1)'"])
    assert res[0]["exit"] == 0 and res[0]["output"].strip() == "1"


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture")
def test_agent_shell_env_resolves_python(cfg, only_python3):
    from artel.agent_env import agent_env

    for runtime in ("native", "claude_code", "codex"):
        env = agent_env(runtime)
        out = subprocess.run(["/bin/sh", "-c", "python -c 'print(2)'"], env=env, capture_output=True, text=True)
        assert out.stdout.strip() == "2", runtime


def test_real_python_is_not_shadowed(cfg, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text("#!/bin/sh\necho mine\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    from artel.agent_env import agent_env

    assert agent_env("native")["PATH"] == str(bin_dir)


# --- item 8 -----------------------------------------------------------------

def test_text_alone_never_makes_a_failure_environmental():
    """Forgery: the work (or a test it runs) can print any text; only structure of the machine counts."""
    from artel.failures import gate_failure_class

    def g(code, out, **kw):
        return [{"command": "x", "exit": code, "output": out, **kw}]

    assert gate_failure_class(g(1, "AssertionError: user: not found")) == "work"
    assert gate_failure_class(g(127, "sh: 1: rg: not found")) == "work"
    assert gate_failure_class(g(1, "bash: jq: command not found")) == "work"
    assert gate_failure_class(g(1, "bwrap: setting up uid map: Permission denied")) == "work"
    assert gate_failure_class(g(1, "ProxyError: cannot connect to proxy at 127.0.0.1:3128")) == "work"
    assert gate_failure_class(g(1, "AssertionError: 2 != 3")) == "work"
    assert gate_failure_class(g(0, "")) is None
    assert gate_failure_class(g(1, "x", env="missing_tool")) == "environment"
    mixed = g(1, "rg: not found", env="missing_tool") + g(1, "AssertionError")
    assert gate_failure_class(mixed) == "work"      # one real failure among environment ones: the work is to blame


def test_gates_classify_by_what_the_machine_lacks(cfg, tmp_path):
    from artel.failures import gate_failure_class

    def cls(*checks):
        return gate_failure_class(_gates(cfg, tmp_path, list(checks)))

    assert cls("nonexistent_tool_xyz --version") == "environment"
    assert cls("echo 'AssertionError: user: not found' >&2; exit 1") == "work"
    assert cls("sh -c 'exit 127'") == "work"            # the program exists: 127 is the script's own business
    assert cls("echo done") is None


def test_environment_only_when_the_first_program_of_the_first_segment_is_absent(cfg, tmp_path):
    from artel.failures import gate_failure_class

    def cls(*checks):
        return gate_failure_class(_gates(cfg, tmp_path, list(checks)))

    assert cls("pytest -q --no-such-option-xyz && ruff check .") == "work"       # pytest fails; ruff never matters
    assert cls("for f in tests/*.py; do python3 $f; done") == "work"             # `for`, `f` are not programs
    assert cls("for f in a; do nonexistent_tool_xyz $f; done") == "work"          # not the first segment's program
    assert cls("python3 -c 'exit(1)' && nonexistent_tool_xyz") == "work"
    assert cls("ok=1; nonexistent_tool_xyz") == "environment"                     # assignments are skipped
    assert cls("cd . && nonexistent_tool_xyz") == "environment"                   # so is cd
    assert cls("nonexistent_tool_xyz; python3 -c 'exit(1)'") == "work"            # the exit status is python's
    assert cls("nonexistent_tool_xyz") == "environment"
    assert cls("X=1 nonexistent_tool_xyz --version && echo hi") == "environment"


def test_first_programs_skips_loop_headers_and_variables():
    from artel.provision import first_programs, leading_program

    assert first_programs(["for f in tests/*.py; do python3 $f; done"]) == {"python3"}
    assert first_programs(["while read l; do grep x $l; done < f"]) == {"grep"}   # `read` is a builtin
    assert first_programs(["case $x in a) ;; esac"]) == set()
    assert leading_program("for f in a; do python3 $f; done") is None
    assert leading_program("FOO=1 BAR=2 rg x") == "rg"
    assert leading_program("cd d && rg x") == "rg"
    assert leading_program("cd d; echo hi; rg x | wc") is None


def test_a_dead_loopback_proxy_is_never_an_environment_verdict(cfg, tmp_path, monkeypatch):
    """The proxy is not consulted when classifying: an ordinary failing check stays 'work' (the gate is held
    elsewhere, see test_network_wait)."""
    import socket

    from artel.failures import gate_failure_class

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    assert gate_failure_class(_gates(cfg, tmp_path, ["exit 1"])) == "work"


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture")
def test_tool_probe_lists_what_exists(only_python3):
    from artel.agent_env import tool_inventory

    have, missing = tool_inventory()
    assert "python3" in have and "python" in have       # python comes from the shim
    assert "git" in missing and "rg" in missing


def test_planner_is_told_the_available_tools(cfg, tmp_path, monkeypatch):
    from artel.plugins import scripted

    seen = []
    orig = scripted.ScriptedProvider._plan

    def spy(self, messages, system):
        seen.append(scripted._first_text(messages))
        return orig(self, messages, system)

    monkeypatch.setattr(scripted.ScriptedProvider, "_plan", spy)
    run_goal("Write a guide", cfg)
    assert "<tools_available>" in seen[0] and "python3" in seen[0]


def test_unavailable_check_tool_sends_plan_back_once(cfg, tmp_path, monkeypatch):
    from artel.plugins import scripted

    seen = []
    orig = scripted.ScriptedProvider._plan

    def spy(self, messages, system):
        seen.append(scripted._first_text(messages))
        return orig(self, messages, system)

    monkeypatch.setattr(scripted.ScriptedProvider, "_plan", spy)
    cfg = deep_merge(cfg, {"scripted": {"bad_check_tool": "nonexistent_tool_xyz"}})
    project, status = run_goal("Write a guide", cfg)
    assert status == "done"
    assert len(seen) == 2 and "nonexistent_tool_xyz" in seen[1] and "<missing_tools>" in seen[1]
    assert all("nonexistent_tool_xyz" not in c for t in project.board.list() for c in t["checks"])
    assert "manager/escalated" not in [e["kind"] for e in project.log.read()]


def test_broken_check_is_not_the_workers_fault(cfg):
    """Incident (b): the planner keeps a check with a tool the machine lacks; the work is done; the project must
    finish without the owner and the worker must not be blamed."""
    cfg = deep_merge(cfg, {"scripted": {"bad_check_tool": "nonexistent_tool_xyz", "bad_check_persists": True}})
    project, status = run_goal("Write a guide", cfg)
    assert status == "done"
    events = project.log.read()
    kinds = [e["kind"] for e in events]
    assert "manager/escalated" not in kinds and project.meta["status"] != "waiting_human"
    tasks = {t["title"]: t for t in project.board.list()}
    build = tasks["Build the deliverable"]
    assert all("nonexistent_tool_xyz" not in c for c in build["checks"])       # rewritten by the replanner
    assert build["attempts"] == 0 and build["needs_human"] == 0                # not counted against the worker
    verdicts = [e for e in events if e["kind"] == "review/verdict" and e["task_id"] == build["id"]]
    assert verdicts[0]["passed"] is False and verdicts[0]["failure_class"] == "environment"
    comments = [c["body"] for c in project.board.comments(build["id"])]
    assert not any(c.startswith("REJECTED") for c in comments)
    assert any("ENVIRONMENT" in c for c in comments)
    assert kinds.count("manager/decision") >= 1


def test_replanner_sees_environment_class_and_premise(cfg, tmp_path):
    project = Project.create("g", cfg, name="brief", workdir=str(tmp_path / "wd"))
    mgr = Manager(new_kernel(cfg), project)
    tid = mgr.board.add_task("T", "b", assignee="x", criteria=["c"], checks=["rg foo"], created_by="planner")
    gate = {"command": "rg foo", "exit": 127, "output": "sh: rg: not found"}
    for _ in range(2):
        project.log.append("review/verdict", task_id=tid, passed=False, feedback="f", failure_class="environment",
                           gates=[{"command": gate["command"], "exit": 127}])
    brief = mgr.manager_brief(mgr.board.get(tid))
    assert 'failure_class="environment"' in brief
    assert "premise" in brief.lower() and "environment" in brief.split("premise", 1)[1].lower()
    assert "<tools_available>" in brief


def test_replan_can_change_checks_only_for_environment_failures(cfg, tmp_path):
    project = Project.create("g", cfg, name="chk", workdir=str(tmp_path / "wd"))
    mgr = Manager(new_kernel(cfg), project)
    tid = mgr.board.add_task("T", "b", assignee="x", criteria=["c"], checks=["rg foo"], created_by="planner")
    mgr.note("snapshot", task_id=tid, tree=mgr.snapper.track())          # claimed: the baseline has no foo
    (project.shared / "foo.txt").write_text("foo\n")                    # the submission
    mgr.board.update(tid, status="blocked")
    from artel.agent import Outcome

    out = Outcome(status="terminal", terminal=("replan_decision", {
        "action": "retry", "rationale": "r", "guidance": "", "assignee": "", "new_worker": {}, "subtasks": [],
        "checks": ["grep -rq foo ."]}))
    mgr.on_replan(tid, out)                      # no environment failure on record: checks stay
    assert mgr.board.get(tid)["checks"] == ["rg foo"]
    mgr.board.update(tid, status="blocked")
    project.log.append("review/verdict", task_id=tid, passed=False, feedback="f", failure_class="environment",
                       gates=[{"command": "rg foo", "exit": 127}])
    _replan(mgr, tid, ["grep -rq foo ."])
    assert mgr.board.get(tid)["checks"] == ["grep -rq foo ."]


# --- rewritten checks are vetted: they must discriminate (fail on the baseline, pass on the result) -------------

def _blocked_env_task(cfg, tmp_path, checks, baseline=None, result=None, isolated=False, claimed=True):
    """A task whose checks hit an environment failure. ``baseline`` files exist when the task is claimed (a snapshot
    of shared/, or the worktree's base commit under isolation), ``result`` files are the worker's submission."""
    if isolated:
        cfg = deep_merge(cfg, {"isolation": {"worktrees": True}})
    project = Project.create("g", cfg, name="vet", workdir=str(tmp_path / "wd"))
    mgr = Manager(new_kernel(cfg), project)
    tid = mgr.board.add_task("T", "b", assignee="x", criteria=["c"], checks=checks, created_by="planner")
    for name, text in (baseline or {}).items():
        (project.shared / name).write_text(text)
    if isolated:
        where = mgr._ensure_worktree(tid)       # records the base commit, as job_work does at claim time
    else:
        if claimed:
            mgr.note("snapshot", task_id=tid, tree=mgr.snapper.track())      # what job_work records at claim time
        where = project.shared
    for name, text in (result or {}).items():
        (where / name).write_text(text)
    mgr.board.update(tid, status="blocked")
    project.log.append("review/verdict", task_id=tid, passed=False, feedback="f", failure_class="environment",
                       gates=[{"command": checks[0], "exit": 127}])
    return project, mgr, tid


def _replan(mgr, tid, checks):
    """The replanner decides to rewrite the checks: vetted in the job (pool) the way the manager schedules it."""
    from unittest import mock

    from artel import manager as manager_mod

    with mock.patch.object(manager_mod, "run_agent", lambda *a, **k: _decision(checks)):
        out = mgr.job_replan(mgr.board.get(tid))
    mgr.on_replan(tid, out)


def _decision(checks):
    from artel.agent import Outcome

    return Outcome(status="terminal", terminal=("replan_decision", {
        "action": "retry", "rationale": "r", "guidance": "", "assignee": "", "new_worker": {}, "subtasks": [],
        "checks": checks}))


@pytest.mark.parametrize("bad", [["true"], [":"], ["exit 0"], ["echo ok"], ["test 1"], ["pytest || true"],
                                 ["nonexistent_tool_xyz foo"], ["printf done"]])
def test_trivial_or_unrunnable_rewrites_are_rejected(cfg, tmp_path, bad):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"])
    _replan(mgr, tid, bad)
    assert mgr.board.get(tid)["checks"] == ["rg foo"]
    assert project.log.read(kinds={"check/rejected"}) and not project.log.read(kinds={"check/rewritten"})


@pytest.mark.parametrize("bad", ["python3 -c pass", "bash -c true", "test -e .", "cat f.txt"])
@pytest.mark.parametrize("isolated", [False, True])
def test_a_check_that_passes_on_the_baseline_is_only_a_reviewer_confirmed_guard(cfg, tmp_path, bad, isolated):
    """No syntax decides this: a check that already passes before the task says nothing about it, so it is accepted
    only as a guard that the reviewer must confirm; the result is not even run."""
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "old\n"},
                                          result={"f.txt": "old\n", "foo.txt": "foo\n"}, isolated=isolated)
    _replan(mgr, tid, [bad])
    ev = project.log.read(kinds={"check/rewritten"})
    assert ev and any("passes on the baseline" in r for r in ev[-1]["confirm"])
    assert ev[-1]["evidence"][0]["baseline"]["exit"] == 0 and ev[-1]["evidence"][0]["result"] is None


def test_the_baseline_pass_is_a_guard_when_the_result_passes_too(cfg, tmp_path):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "x"}, result={"f.txt": "x"})
    _replan(mgr, tid, ["test -e f.txt"])
    assert any("passes on the baseline" in r for r in project.log.read(kinds={"check/rewritten"})[-1]["confirm"])


def test_a_check_that_fails_on_the_submitted_result_is_rejected(cfg, tmp_path):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "x"}, result={"f.txt": "x"})
    _replan(mgr, tid, ["grep -rq foo ."])
    assert "fails on the submitted result" in project.log.read(kinds={"check/rejected"})[-1]["reason"]


def test_a_task_without_a_baseline_cannot_have_its_checks_rewritten(cfg, tmp_path):
    """A task nobody ever claimed has no snapshot event and no worktree: nothing to compare against."""
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], result={"foo.txt": "foo\n"},
                                          claimed=False)
    _replan(mgr, tid, ["grep -rq foo ."])
    assert mgr.board.get(tid)["checks"] == ["rg foo"]
    assert "baseline" in project.log.read(kinds={"check/rejected"})[-1]["reason"]


def test_rewrite_must_not_drop_checks(cfg, tmp_path):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo", "rg bar"], result={"foo.txt": "foo bar\n"})
    _replan(mgr, tid, ["grep -rq foo ."])
    assert mgr.board.get(tid)["checks"] == ["rg foo", "rg bar"]


@pytest.mark.parametrize("isolated", [False, True])
def test_rg_to_grep_is_accepted_journaled_and_reviewed_against_the_old_checks(cfg, tmp_path, monkeypatch, isolated):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "old\n"},
                                          result={"f.txt": "old\n", "foo.txt": "foo\n"}, isolated=isolated)
    _replan(mgr, tid, ["grep -rq foo ."])
    assert mgr.board.get(tid)["checks"] == ["grep -rq foo ."]
    ev = project.log.read(kinds={"check/rewritten"})
    assert ev and ev[-1]["old"] == ["rg foo"] and ev[-1]["new"] == ["grep -rq foo ."]
    assert mgr.board.get(tid)["status"] == "review"
    assert not project.log.read(kinds={"gate/result"})          # the vetting runs are not the task's gate results
    seen = []
    from artel import manager as manager_mod
    from artel.agent import Outcome

    monkeypatch.setattr(manager_mod, "run_agent", lambda *a, **k: seen.append(k["first_user"]) or Outcome(status="error"))
    mgr.job_review(mgr.board.get(tid))
    brief = seen[0]
    assert "<checks_rewritten>" in brief and "rg foo" in brief and "grep -rq foo ." in brief
    assert "same criteria" in brief


def test_a_worker_moving_main_does_not_move_the_baseline(cfg, tmp_path, monkeypatch):
    """The baseline is the commit recorded at claim time. The old merge-base(main, branch) followed `git branch -f`."""
    import subprocess

    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "old\n"}, isolated=True)
    wt = mgr.iso.path(tid)
    (wt / "foo.txt").write_text("foo\n")
    mgr.iso.commit(tid, "work")
    subprocess.run(["git", f"--git-dir={mgr.iso.git_dir}", "branch", "-f", "main", mgr.iso.branch(tid)], check=True)
    _replan(mgr, tid, ["grep -rq foo ."])
    assert mgr.board.get(tid)["checks"] == ["grep -rq foo ."]


def test_a_branch_that_lost_the_recorded_base_rejects_the_rewrite(cfg, tmp_path):
    import subprocess

    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "old\n"}, isolated=True)
    wt = mgr.iso.path(tid)
    subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--orphan", "x"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", "r", "--allow-empty"],
                   check=True)
    subprocess.run(["git", f"--git-dir={mgr.iso.git_dir}", "branch", "-f", mgr.iso.branch(tid), "x"], check=True)
    _replan(mgr, tid, ["grep -rq foo ."])
    assert mgr.board.get(tid)["checks"] == ["rg foo"]
    assert "база задачи изменена" in project.log.read(kinds={"check/rejected"})[-1]["reason"]


def test_existence_check_replacing_a_test_run_needs_the_reviewer_and_the_review_tool_gates_acceptance(cfg, tmp_path, monkeypatch):
    from artel import manager as manager_mod
    from artel.agent import Outcome

    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["pytest tests/test_x.py"], baseline={"f.txt": "old\n"},
                                          result={"f.txt": "old\n", "out.txt": "x\n"})
    _replan(mgr, tid, ["test -e out.txt"])
    ev = project.log.read(kinds={"check/rewritten"})[-1]
    assert len(ev["confirm"]) >= 2 and any("existence" in r for r in ev["confirm"])
    assert any("pytest" in r for r in ev["confirm"]) and any("artefacts" in r for r in ev["confirm"])
    seen = []

    def verdict(confirmed):
        def fake(*a, **k):
            seen.append(k["first_user"])
            return Outcome(status="terminal", terminal=("review_verdict", {
                "passed": True, "feedback": "ok", "failed_criteria": [], "checks_cover_criteria": confirmed}))
        monkeypatch.setattr(manager_mod, "run_agent", fake)
        mgr.board.update(tid, status="review")
        mgr.on_review(tid, mgr.job_review(mgr.board.get(tid)))
        return mgr.board.get(tid)["status"]

    assert verdict(False) != "done"
    assert "test -e out.txt" in seen[0] and "baseline: exit 1" in seen[0] and "result: exit 0" in seen[0]
    assert "checks_cover_criteria" in seen[0]
    assert verdict(True) == "done"


def test_rg_to_grep_needs_no_extra_confirmation(cfg, tmp_path):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "old\n"},
                                          result={"f.txt": "old\n", "foo.txt": "foo\n"})
    _replan(mgr, tid, ["grep -rq foo ."])
    assert project.log.read(kinds={"check/rewritten"})[-1]["confirm"] == []


def test_vetting_runs_in_the_job_pool_baseline_first_and_without_provisioning(cfg, tmp_path, monkeypatch):
    import threading

    from artel import provision

    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "x"}, result={"f.txt": "x"})
    calls = []
    orig = mgr.run_gates
    monkeypatch.setattr(mgr, "run_gates", lambda task, **kw: calls.append((threading.current_thread().name, kw.get("vet"),
                                                                           kw["checks"], str(kw["cwd"]))) or orig(task, **kw))
    monkeypatch.setattr(provision, "prepare_checks", lambda *a, **k: calls.append("provisioned") or [])
    from unittest import mock

    from artel import manager as manager_mod

    with mock.patch.object(manager_mod, "run_agent", lambda *a, **k: _decision(["test -e f.txt"])):
        fut = mgr.pool.submit(mgr.job_replan, mgr.board.get(tid))
        fut.result(timeout=60)
    assert "provisioned" not in calls
    assert len(calls) == 1 and calls[0][0].startswith("artel") and calls[0][1] is True   # baseline only: it passed
    assert "artel-baseline-" in calls[0][3]


def test_vetting_is_offline_and_writes_only_its_temporary_tree(cfg, tmp_path, request):
    from artel import sandbox

    ok, why = sandbox.bwrap_status(False)
    if not ok:
        pytest.skip(f"no sandbox backend here: {why}")
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "x"})
    task = mgr.board.get(tid)
    out = Path.home() / f".artel-vet-probe-{tmp_path.name}"       # outside the tree and the temp directories
    request.addfinalizer(lambda: out.unlink(missing_ok=True))
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    try:
        res = mgr.run_gates(task, cwd=project.shared, record=False, vet=True, checks=[
            f"touch {out}", f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2)\""])
        online = mgr.run_gates(task, cwd=project.shared, record=False, vet=False, checks=[
            f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2)\""])
    finally:
        srv.close()
    assert online[0]["exit"] == 0                                  # the listener is reachable from a normal gate
    assert res[0]["exit"] != 0 and not out.exists()                # only the temporary tree is writable
    assert res[1]["exit"] != 0                                     # and the network is off


def test_a_check_that_fails_on_the_result_too_is_rejected(cfg, tmp_path):
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["rg foo"], baseline={"f.txt": "x"}, result={"f.txt": "x"})
    _replan(mgr, tid, ["pytest -k nonexistent"])
    assert "fails on the submitted result" in project.log.read(kinds={"check/rejected"})[-1]["reason"]


def test_a_forged_journal_event_does_not_remove_the_confirmation_requirement(cfg, tmp_path):
    """The requirement lives on the board row the manager wrote; appending a check/rewritten event with an empty
    `confirm` (what a worker with access to events.jsonl can do) changes nothing."""
    project, mgr, tid = _blocked_env_task(cfg, tmp_path, ["pytest tests/test_x.py"], baseline={"f.txt": "old\n"},
                                          result={"f.txt": "old\n", "out.txt": "x\n"})
    _replan(mgr, tid, ["test -e out.txt"])
    assert mgr._needs_checks_confirmation(tid)
    new = mgr.board.get(tid)["checks"]
    project.log.append("check/rewritten", task_id=tid, old=["x"], new=new, confirm=[], evidence=[])
    project.log.append("check/rejected", task_id=tid, old=new, new=["x"], reason="r")
    assert mgr._needs_checks_confirmation(tid)
    assert "existence" in mgr._rewritten_brief(tid)           # the reasons shown come from the row too


def test_the_journal_and_the_board_are_protected_from_agent_writes(tmp_path, monkeypatch):
    from artel import sandbox

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    prot = sandbox.protected_paths(root)
    for n in ("events.jsonl", "board.db"):
        assert os.path.realpath(str(root / n)) in prot
