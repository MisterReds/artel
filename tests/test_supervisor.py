"""An evolution generation outlives its process: a dead one is resumed with the same options, a live one is left
alone, a finished one is never restarted, and a crash loop stops at a person instead of restarting forever."""
import json
import os
import subprocess
import sys
import time

import pytest

from artel.evolve import supervisor as sv


def _home(tmp_path, running=True, hb=None):
    d = tmp_path / "evolution" / "repo"
    d.mkdir(parents=True)
    state = {"main": "master", "generation": 5, "repo": str(tmp_path)}
    if running:
        state["running"] = {"generation": 5, "phase": "implementing", "plan": {}}
    (d / "state.json").write_text(json.dumps(state))
    if hb is not None:
        (d / "heartbeat.json").write_text(json.dumps(hb))
    return d


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_resume_argv_keeps_what_matters():
    argv = ["evolve", "--roadmap", "2", "--wave", "2", "--parallel-candidates", "2", "--parallel-bench=3",
            "--budget", "30", "--no-stack", "--runtimes", "claude_code,codex", "--generations", "4"]
    assert sv.resume_argv(argv) == ["evolve", "--resume", "--parallel-candidates", "2", "--parallel-bench=3",
                                    "--budget", "30", "--no-stack", "--runtimes", "claude_code,codex"]


def test_only_dead_running_generations_are_orphaned(tmp_path):
    root = tmp_path / "evolution"
    _home(tmp_path, hb={"pid": os.getpid(), "at": time.time() - 3600})  # our own pid: alive
    assert sv.orphaned(root) == []
    (root / "repo" / "heartbeat.json").write_text(json.dumps({"pid": _dead_pid(), "at": time.time()}))
    assert sv.orphaned(root) == []  # fresh heartbeat: not yet
    (root / "repo" / "heartbeat.json").write_text(json.dumps({"pid": _dead_pid(), "at": time.time() - 600}))
    assert [o["home"].name for o in sv.orphaned(root)] == ["repo"]
    st = json.loads((root / "repo" / "state.json").read_text())
    st.pop("running")
    (root / "repo" / "state.json").write_text(json.dumps(st))
    assert sv.orphaned(root) == []  # a finished generation is never restarted


def test_restart_starts_resume_and_records_it(tmp_path, monkeypatch):
    calls = []

    class P:
        pid = 4242

    monkeypatch.setattr(sv, "_spawn", lambda args, **kw: calls.append((args, kw)) or P())
    d = _home(tmp_path, hb={"pid": _dead_pid(), "at": time.time() - 600, "started": time.time() - 7200,
                            "argv": ["evolve", "--resume", "--budget", "30"], "cwd": str(tmp_path / "planted"),
                            "artel_home": str(tmp_path / "elsewhere")})
    (tmp_path / "planted" / "artel").mkdir(parents=True)  # a package that would shadow artel from the cwd
    assert sv.check(tmp_path / "evolution", log=lambda m: None) == [4242]
    args, kw = calls[0]
    assert args[args.index("-m"):args.index("-m") + 3] == ["-m", "artel.cli", "evolve"]
    assert "--resume" in args and "--budget" in args
    # heartbeat.json never chooses where or as what the resume runs
    assert "--repo" in args and kw["env"]["ARTEL_HOME"] == str(tmp_path)
    assert kw["cwd"] != str(tmp_path / "planted")
    assert sys.version_info < (3, 11) or args[1] == "-P"
    st = json.loads((d / "state.json").read_text())
    assert st["running"]["supervisor"]["restarts"] == 1
    assert any(json.loads(ln)["kind"] == "auto_resume" for ln in (d / "ledger.jsonl").read_text().splitlines())
    assert sv.orphaned(tmp_path / "evolution") == []  # the new process counts as alive at once: no double start


def test_a_crash_loop_stops_at_a_person(tmp_path, monkeypatch):
    class P:
        pid = 1

    monkeypatch.setattr(sv, "_spawn", lambda args, **kw: P())
    d = _home(tmp_path)
    started = []
    for _ in range(6):
        now = time.time()
        (d / "heartbeat.json").write_text(json.dumps({"pid": _dead_pid(), "started": now - 700, "at": now - 600,
                                                      "argv": ["evolve"]}))
        started += sv.check(tmp_path / "evolution", log=lambda m: None)
    st = json.loads((d / "state.json").read_text())
    assert len(started) == 1 + sv.MAX_QUICK_DEATHS - 1 and "died within" in st["running"]["auto_resume_blocked"]
    assert sv.orphaned(tmp_path / "evolution") == []


def test_heartbeat_lifecycle(tmp_path):
    hb = sv.Heartbeat(tmp_path, ["evolve", "--resume"]).start()
    data = json.loads((tmp_path / "heartbeat.json").read_text())
    assert data["pid"] == os.getpid() and data["argv"] == ["evolve", "--resume"]
    hb.stop()
    assert not (tmp_path / "heartbeat.json").exists()


def test_without_a_heartbeat_a_running_evolve_process_is_trusted(tmp_path, monkeypatch):
    _home(tmp_path)  # running, no heartbeat (started by a version that did not write one)
    monkeypatch.setattr(sv, "evolve_process_running", lambda **kw: True)
    assert sv.orphaned(tmp_path / "evolution") == []
    monkeypatch.setattr(sv, "evolve_process_running", lambda **kw: None)  # cannot tell: leave it alone
    assert sv.orphaned(tmp_path / "evolution") == []
    monkeypatch.setattr(sv, "evolve_process_running", lambda **kw: False)
    assert len(sv.orphaned(tmp_path / "evolution")) == 1


def test_one_evolution_per_repository(tmp_path, monkeypatch):
    from artel.evolve.engine import git

    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    monkeypatch.setattr(sv, "evolve_process_running", lambda **kw: False)
    lock = sv.RepoLock(repo).acquire()
    assert lock.path.exists() and lock.holder() is None  # our own lock is not "someone else"
    other = sv.RepoLock(repo)
    lock.path.write_text(json.dumps({"pid": 1 if sv.pid_alive(1) else os.getppid()}))
    with pytest.raises(sv.AlreadyRunning):
        other.acquire()
    lock.path.write_text(json.dumps({"pid": _dead_pid()}))  # a stale lock from a dead process is taken over
    other.acquire()
    other.release()
    assert not lock.path.exists()
    monkeypatch.setattr(sv, "evolve_process_running", lambda **kw: True)
    with pytest.raises(sv.AlreadyRunning, match="without locks"):
        sv.RepoLock(repo).acquire()
