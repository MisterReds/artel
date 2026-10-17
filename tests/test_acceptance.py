import json
import threading
import time

import pytest

from artel import acceptance
from artel.cli import main
from artel.manager import run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge

H = 3600


def ev(kind, ts, **f):
    return {"seq": 0, "ts": ts, "kind": kind, **f}


def synthetic(ended="done"):
    """Two run segments (0-1 h and 10-13 h), a verification run at 20 h that must not count."""
    return [
        ev("project/status", 0, status="running"),
        ev("agent/start", 10, role="worker", purpose="work"),
        ev("agent/end", 20, role="worker", runtime="claude_code", status="terminal",
           usage={"input_tokens": 100, "output_tokens": 50}),
        ev("agent/start", 30, role="worker", purpose="work"),
        ev("agent/end", 40, role="worker", status="terminal", usage={"input_tokens": 10, "output_tokens": 5}),
        ev("agent/start", 50, role="manager", purpose="replan"),
        # a task escalated to the owner: the update and the escalation are one ask; a goal-level escalation is another
        ev("board/task_updated", 60, fields={"needs_human": 1}),
        ev("manager/escalated", 61, task_id="t1", rationale="?"),
        ev("manager/escalated", 62, rationale="goal audit"),
        ev("policy/blocked", 63, decision="prompt"),
        ev("board/task_updated", 64, fields={"status": "ready", "needs_human": 0}),
        ev("board/comment", 65, author="human"),
        ev("board/comment", 66, author="manager"),
        ev("owner/resume", 67, via="telegram", after_answer=True),
        ev("owner/confirm_intent", 68),
        ev("owner/stop", 69, via="web"),
        ev("provider/limited", 70, runtime="codex", until=1, reason="limit"),
        ev("manager/provider_limit", 71, task_id="t1", until=1, reason="limit"),
        ev("agent/start", 72, role="worker", purpose="work"),
        ev("project/status", H, status="stopped"),
        ev("project/status", 10 * H, status="running"),
        ev("agent/end", 12 * H, role="worker", status="terminal", usage={"input_tokens": 1, "output_tokens": 1},
           runtime="codex"),
        ev("project/status", 13 * H, status=ended),
        ev("agent/start", 20 * H, role="auditor", purpose="verify"),
        ev("agent/end", 20 * H + 5, role="auditor", purpose="verify", runtime="codex",
           usage={"input_tokens": 1000, "output_tokens": 1000}),
        ev("acceptance/verdict", 20 * H + 6, runtime="codex", independent=True, complete=True, summary="ok",
           missing=[], usage={"input_tokens": 1000, "output_tokens": 1000}),
    ]


TASKS = [{"kind": "goal", "status": "done"}, {"kind": "task", "status": "done"}, {"kind": "task", "status": "done"},
         {"kind": "task", "status": "blocked"}, {"kind": "task", "status": "cancelled"},
         {"kind": "task", "status": "ready"}]
META = {"tiers": {"expensive_tokens": 5, "medium_tokens": 95, "expensive_calls": 1, "medium_calls": 9,
                  "cheap_calls": 10},
        "usage": {"input_tokens": 9000, "output_tokens": 9000, "cost_usd": 1.5}}


def test_measure_synthetic():
    m = acceptance.measure(synthetic(), TASKS, META)
    assert m["duration_h"] == 4.0 and m["ended"] == "done"  # 1 h + 3 h; the idle gap and the verification do not count
    assert m["stopped_by_limit"] is False and m["limit_waits"] == 1  # provider/limited only
    assert m["expensive_token_share"] == 0.05 and m["expensive_call_share"] == 0.05
    assert m["tokens_total"] == 167 and m["tokens_by_runtime"] == {"claude_code": 150, "native": 15, "codex": 2}
    assert m["token_note"] and m["tasks_done"] == 2 and m["tokens_per_accepted"] == 84
    assert m["success_rate"] == 0.5
    assert m["steps"] == 4 and m["owner_asks"] == 2 and m["autonomy_share"] == 0.5
    assert m["owner_interventions"] == 2  # the human comment and owner/stop; not the after-answer resume or confirm
    assert m["cost_usd"] == 1.5 and m["cost_note"] == "includes verification"
    assert m["decision_log_coverage"] is None
    assert m["verdict"]["runtime"] == "codex" and m["verdict"]["usage"]["input_tokens"] == 1000


def test_owner_ask_counts_one_per_transition():
    pair = [ev("board/task_updated", 1, fields={"needs_human": True}), ev("manager/escalated", 2, task_id="t")]
    assert acceptance.measure(pair, [], {})["owner_asks"] == 1
    assert acceptance.measure([ev("policy/blocked", 1, decision="prompt")], [], {})["owner_asks"] == 0
    assert acceptance.measure([ev("manager/escalated", 1, rationale="x")], [], {})["owner_asks"] == 1


def test_no_verification_no_cost_note():
    m = acceptance.measure(synthetic()[:-3], TASKS, META)
    assert m["cost_note"] is None and m["verdict"] is None and m["tokens_total"] == 167


def test_stopped_by_limit():
    def run(*tail, ended="stopped"):
        return acceptance.measure([ev("project/status", 0, status="running"), *tail,
                                   *([ev("project/status", 9, status=ended)] if ended else [])], [], {})

    limited = ev("provider/limited", 5, runtime="codex")
    assert run(limited)["stopped_by_limit"] is True
    assert run(limited, ended="waiting_human")["stopped_by_limit"] is True
    assert run(limited, ended="error")["stopped_by_limit"] is True
    assert run(ev("agent/end", 1, status="terminal"), limited)["stopped_by_limit"] is True
    assert run(limited, ev("agent/end", 6, status="terminal"))["stopped_by_limit"] is False  # work went on after it
    assert run(limited, ev("agent/end", 6, status="error"))["stopped_by_limit"] is True
    assert run(limited, ev("owner/stop", 6, via="web"))["stopped_by_limit"] is False
    assert run(ev("owner/stop", 1, via="web"), limited)["stopped_by_limit"] is True
    assert run(limited, ended="done")["stopped_by_limit"] is False
    assert run(limited, ended="needs_review")["stopped_by_limit"] is False
    assert run(ended="stopped")["stopped_by_limit"] is False
    assert run(limited, ended=None)["stopped_by_limit"] is None
    assert run(limited, ended="running")["stopped_by_limit"] is None


def test_duration_open_segment_and_verify():
    open_run = [ev("project/status", 0, status="running"), ev("agent/start", H, purpose="work"),
                ev("agent/start", 5 * H, purpose="verify")]
    assert acceptance.measure(open_run, [], {})["duration_h"] == 1.0
    assert acceptance.measure([ev("agent/start", 1)], [], {})["duration_h"] is None


def test_measure_empty():
    m = acceptance.measure([], [], {})
    assert m["duration_h"] is None and m["ended"] is None and m["steps"] == 0 and m["stopped_by_limit"] is None
    assert m["autonomy_share"] is None and m["tokens_per_accepted"] is None and m["success_rate"] is None
    assert m["expensive_token_share"] is None and m["expensive_call_share"] is None
    assert m["tokens_total"] == 0 and m["tokens_by_runtime"] == {} and m["cost_usd"] is None and m["verdict"] is None


def test_check():
    m = acceptance.measure(synthetic(), TASKS, META)
    c = acceptance.check(m, {"expensive_token_share_max": 0.05, "expensive_call_share_max": 0.04, "autonomy_min": 0.95,
                             "owner_interventions_max": 2, "hours_min": 6, "no_limit_stop": True})
    assert c["expensive_token_share_max"] == {"value": 0.05, "limit": 0.05, "ok": True}
    assert c["expensive_call_share_max"]["ok"] is False
    assert c["autonomy_min"]["ok"] is False and c["owner_interventions_max"]["ok"] is True
    assert c["hours_min"] == {"value": 4.0, "limit": 6, "ok": False}
    assert c["no_limit_stop"]["ok"] is True
    stopped = acceptance.measure([ev("project/status", 0, status="running"), ev("provider/limited", 1),
                                  ev("project/status", 2, status="stopped")], [], {})
    assert acceptance.check(stopped, {"no_limit_stop": True})["no_limit_stop"]["ok"] is False
    empty = acceptance.check(acceptance.measure([], [], {}), {"autonomy_min": 0.95, "hours_min": 6,
                                                              "no_limit_stop": True})
    assert all(v["ok"] is None for v in empty.values())


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path))
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})


@pytest.fixture
def project(scripted):
    return run_goal("write a hello world script", scripted)[0]


def test_measure_project(project):
    m = acceptance.measure_project(project)
    events = project.log.read()
    assert m["ended"] == project.meta["status"]
    assert m["steps"] == sum(1 for e in events if e["kind"] == "agent/start") > 0
    assert m["tasks_done"] == sum(1 for t in project.board.list() if t["kind"] != "goal" and t["status"] == "done")
    spent = sum(e["usage"]["input_tokens"] + e["usage"]["output_tokens"] for e in events if e["kind"] == "agent/end")
    assert m["tokens_total"] == spent > 0
    assert set(m["tokens_by_runtime"]) == {"native"}


def test_owner_events_logged(scripted, project):
    from artel.web.server import Runner

    runner = Runner(scripted)
    runner.resume(project.slug)
    runner.resume(project.slug, via="telegram", after_answer=True)  # already running: nothing is logged
    for _ in range(100):  # let the resumed run end
        if not runner.active:
            break
        time.sleep(0.1)
    runner.resume(project.slug, via="telegram", after_answer=True)
    for _ in range(100):
        if not runner.active:
            break
        time.sleep(0.1)
    log = Project.open(project.slug, scripted).log
    resumes = [e for e in log.read() if e["kind"] == "owner/resume"]
    assert [(e["via"], e.get("after_answer")) for e in resumes] == [("web", None), ("telegram", True)]
    runner.active[project.slug] = threading.Event()
    assert runner.stop(project.slug, via="telegram") and runner.stop(project.slug, via="web")
    stops = [e for e in log.read() if e["kind"] == "owner/stop"]
    assert [e["via"] for e in stops] == ["telegram"]  # one stop, one event
    assert acceptance.measure_project(Project.open(project.slug, scripted))["owner_interventions"] == 2


def test_cli_ctrl_c_logs_owner_stop(project):
    from artel.cli import _note_owner_stop

    _note_owner_stop(None)
    _note_owner_stop((project, "stopped"))
    assert [e["via"] for e in project.log.read() if e["kind"] == "owner/stop"] == ["cli"]


@pytest.mark.parametrize("name,hours", [("S", None), ("L", 6)])
def test_scenarios(name, hours):
    s = acceptance.load_scenario(name)
    assert s["id"] == name and s["idea"] and s["expected_hours"]
    t = s["thresholds"]
    assert t["expensive_token_share_max"] == 0.05 and t["expensive_call_share_max"] == 0.05
    assert t["autonomy_min"] == 0.95 and t["owner_interventions_max"] == 1 and t["no_limit_stop"] is True
    assert t.get("hours_min") == hours
    with pytest.raises(FileNotFoundError):
        acceptance.load_scenario("nope")


def test_cli_metrics_json(project, capsys):
    assert main(["metrics", project.slug, "--json", "--scenario", "S"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["metrics"]["steps"] > 0 and "autonomy_min" in data["check"]
    assert main(["metrics", project.slug]) == 0
    assert "Автономность" in capsys.readouterr().out


def test_cli_scenario_errors(project, capsys):
    with pytest.raises(SystemExit):
        main(["acceptance", "run", "X"])
    project.set_meta(acceptance="Z")
    assert main(["metrics", project.slug]) == 2
    assert "сценарий приёмки" in capsys.readouterr().err


def worker_project(config, *runtimes):
    p = Project.create("write a hello world script", config)
    for rt in runtimes:
        p.log.append("agent/end", actor="w", role="worker", purpose="work", status="terminal", runtime=rt,
                     usage={"input_tokens": 10, "output_tokens": 5})
    return p


def test_verify_needs_independent_runtime(scripted, monkeypatch):
    monkeypatch.setattr("artel.runtimes.connected_runtimes", lambda c: ["claude_code", "codex"])
    p = worker_project(scripted, "claude_code", "codex")
    res = acceptance.verify(p, scripted)
    assert res == {"error": "нет независимого рантайма: подключите второй провайдер", "independent": False}
    assert not [e for e in p.log.read() if e["kind"] in ("acceptance/verdict", "agent/start")]
    # the testers' runtime counts as the workers' runtime
    p2 = worker_project(scripted, "claude_code")
    p2.log.append("agent/end", actor="t", role="tester", purpose="work", status="terminal", runtime="codex")
    assert "error" in acceptance.verify(p2, scripted)


def test_verify_independent_and_isolated(scripted, monkeypatch):
    monkeypatch.setattr("artel.runtimes.connected_runtimes", lambda c: ["claude_code", "native"])
    seen = []
    real = acceptance.new_kernel
    monkeypatch.setattr(acceptance, "new_kernel", lambda cfg: seen.append(cfg) or real(cfg))
    p = worker_project(scripted, "claude_code")
    before = (p.meta.get("tiers"), [(t["id"], t["status"]) for t in p.board.list()], p.meta["status"])
    seq = p.log.seq
    base = acceptance.measure_project(p)
    res = acceptance.verify(p, scripted)
    assert res["runtime"] == "native" and res["independent"] is True and res["complete"] is True
    assert seen[0]["failover"] is False and seen[0]["runtime_by_role"]["auditor"] == "native"
    after = p.log.read(after=seq)
    assert [e for e in after if e["kind"] == "acceptance/verdict"][0]["usage"]["input_tokens"] > 0
    assert not [e for e in after if e["kind"] == "tier/usage"]
    assert (p.meta.get("tiers"), [(t["id"], t["status"]) for t in p.board.list()], p.meta["status"]) == before
    m = acceptance.measure_project(p)
    assert m["tokens_total"] == base["tokens_total"] == 15 and m["steps"] == base["steps"]
    assert m["tokens_by_runtime"] == {"claude_code": 15} and m["cost_note"] == "includes verification"
    assert m["verdict"]["independent"] is True


def test_verify_configured_order_and_no_fallback(scripted, monkeypatch):
    from unittest import mock

    from artel.runtimes import runtime_for

    monkeypatch.setattr("artel.runtimes.connected_runtimes", lambda c: ["claude_code", "codex"])
    cfg = {**scripted, "acceptance": {"verifier_runtimes": ["native"]}}
    p = worker_project(cfg, "claude_code", "codex")
    assert acceptance.verify(p, cfg)["runtime"] == "native"
    # the verifier on a limit waits: with failover off runtime_for keeps it even though another runtime is free
    live = {"provider": "anthropic", "runtime_by_role": {"auditor": "codex"}, "failover": False}
    with mock.patch("artel.limits.load", return_value={"codex": 9e12}), \
            mock.patch("artel.runtimes.detect_runtime", return_value="claude_code"):
        assert runtime_for(live, "auditor") == "codex"
