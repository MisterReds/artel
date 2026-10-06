import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from artel.decide import HttpSystemOne, choice, noul, score
from artel.manager import Manager, new_kernel, run_goal
from artel.project import DEFAULT_CONFIG, Project, deep_merge


class FakeSystemOne:
    """A System One over HTTP with the documented contract; answers are configurable per question name."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"auth": self.headers.get("Authorization"), "body": body})
                out = {"id": "gen-dec-1", "model": "team-s1-test",
                       "answers": {q: fake.answers[q] for q in body["questions"]},
                       "usage": {"input_tokens": 100, "output_tokens": 10, "cost": 0.0000042}}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/decide"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("S1_KEY", "s1-test")


def cfg_with(url, **points):
    # One rejected review makes the run "troubled", so the rule-based gate (evolved in gen 1:
    # no failures -> no librarian/refine) lets learning through and the System One gate decides.
    return deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05,
                                       "skills": {"review_after_tool_calls": 1}, "scripted": {"review_failures": 1},
                                       "decide": {"backend": "http", "url": url, "api_key_env": "S1_KEY",
                                                  "points": points}})


def test_request_shape_and_parsing(env):
    s1 = FakeSystemOne({"is_bug": {"type": "noul", "noul": 0.96},
                   "team": {"type": "choice", "choice": "payments", "confidence": 0.67,
                            "probabilities": {"payments": 0.78, "frontend": 0.22}},
                   "urgency": {"type": "score", "score": 1.99, "confidence": 0.99}})
    d = HttpSystemOne(cfg_with(s1.url)["decide"])
    ans = d.ask({"ticket": "blank checkout"}, {
        "is_bug": noul("Is it a defect?", "broken behavior", "a question"),
        "team": choice("Which team?", {"payments": "checkout", "frontend": "rendering"}),
        "urgency": score("How urgent?", ["later", "this week", "now"])})
    req = s1.requests[0]
    assert req["auth"] == "Bearer s1-test" and "model" not in req["body"]
    assert req["body"]["questions"]["is_bug"] == {"type": "noul", "instructions": "Is it a defect?",
                                                  "criteria": {"true": "broken behavior", "false": "a question"}}
    assert req["body"]["questions"]["urgency"]["criteria"] == ["later", "this week", "now"]
    assert ans["team"]["choice"] == "payments" and ans["is_bug"]["noul"] == 0.96


def test_low_probability_skips_skill_review_and_refine(env):
    s1 = FakeSystemOne({"go": {"type": "noul", "noul": 0.05}})
    project, status = run_goal("Gate me", cfg_with(s1.url, skill_review=True, refine=True))
    assert status == "done"
    kinds = [e["kind"] for e in project.log.read()]
    assert "decide/answer" in kinds
    assert "skills/reviewed" not in kinds and "harness/refined" not in kinds
    assert project.meta["usage"]["cost_usd"] > 0  # decision cost is accounted


def test_high_probability_keeps_learning(env):
    s1 = FakeSystemOne({"go": {"type": "noul", "noul": 0.9}})
    project, _ = run_goal("Learn", cfg_with(s1.url, skill_review=True, refine=True))
    kinds = [e["kind"] for e in project.log.read()]
    assert "skills/reviewed" in kinds and "harness/refined" in kinds


def test_replan_shortcut_escalates_without_llm_manager(env):
    s1 = FakeSystemOne({"action": {"type": "choice", "choice": "human", "confidence": 0.9,
                              "probabilities": {"human": 0.92, "retry": 0.08}}})
    cfg = cfg_with(s1.url, replan_shortcut=True)
    project = Project.create("Shortcut", cfg)
    m = Manager(new_kernel(cfg), project)
    m.plan()
    t = project.board.list(kind="work")[0]
    project.board.update(t["id"], status="blocked")
    assert m.run() == "waiting_human"
    assert project.board.get(t["id"])["needs_human"] == 1
    starts = [e for e in project.log.read() if e["kind"] == "agent/start" and e["role"] == "manager"]
    assert starts == []  # the expensive manager LLM was never called


def test_backend_failure_falls_back_to_default(env, monkeypatch):
    project, status = run_goal("No key", cfg_with("http://127.0.0.1:9/none", skill_review=True))
    kinds = [e["kind"] for e in project.log.read()]
    assert status == "done" and "decide/error" in kinds and "skills/reviewed" in kinds


def test_plugin_system_one_and_no_system_one(env):
    """A team's System One plugs in as a kernel service; without one, decision points use their defaults."""
    import types

    asked = []

    class TeamS1:
        name = "team-s1"

        def ask(self, state, questions):
            asked.append(sorted(questions))
            return {q: {"type": "noul", "noul": 0.02} for q in questions}

    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05,
                                      "skills": {"review_after_tool_calls": 1}, "scripted": {"review_failures": 1},
                                      "decide": {"points": {"skill_review": True}}})
    k = new_kernel(cfg)
    mod = types.ModuleType("team_s1")
    mod.setup = lambda kk: kk.provide("system_one", TeamS1())
    k.load_plugin("team_s1", mod)
    project, status = run_goal("Plugin S1", cfg, kernel=k)
    ev = project.log.read()
    assert status == "done" and asked and any(e["kind"] == "decide/answer" and e["backend"] == "team-s1" for e in ev)
    assert not any(e["kind"] == "skills/reviewed" for e in ev)  # the team's answer (0.02) skipped the librarian
    project, status = run_goal("No S1", cfg)  # nothing connected: defaults, no decide events
    ev = project.log.read()
    assert status == "done" and not any(e["kind"].startswith("decide/") for e in ev)
    assert any(e["kind"] == "skills/reviewed" for e in ev)
