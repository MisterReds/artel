"""A dead proxy or provider network is an environment failure: the project waits, probes, and resumes by itself."""

import socket
import threading
import time

import pytest

from artel.analytics import stage_of
from artel.failures import classify
from artel.manager import Manager, new_kernel
from artel.plugins.llm_providers import LLMError
from artel.project import DEFAULT_CONFIG, Project, deep_merge


@pytest.mark.parametrize("text", [
    "error: openai_compat failed: ConnectError: [Errno 111] Connection refused",
    "error: ProxyError('Cannot connect to proxy.', NewConnectionError('Connection refused'))",
    "error: APIConnectionError: Connection error.",
    "error: [Errno -2] Name or service not known",
    "error: ReadTimeout: timed out",
    "error: Network is unreachable",
])
def test_network_failures_are_classified(text):
    assert classify(text) == "network"


def test_other_failures_keep_their_class():
    assert classify("HTTP 429: rate limit") == "limit"
    assert classify("not logged in") == "setup"
    assert classify("stalled: 3 consecutive turns without tool use") == "work"


class ProxyDownProvider:
    """The real scripted provider behind a proxy: while nothing listens on the proxy port every call fails."""

    def __init__(self, real, port):
        self.real, self.port, self.refused = real, port, 0

    def chat(self, **kw):
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError as exc:
            self.refused += 1
            raise LLMError(f"openai_compat failed: ConnectError: {exc}") from exc
        return self.real.chat(**kw)


def test_dead_proxy_waits_and_resumes_without_replanning(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                          # the "proxy" is down: connection refused
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05, "network_probe_s": 0.05,
                                      "network_probe_max_s": 0.2})
    k = new_kernel(cfg)
    fake = ProxyDownProvider(k.get("llm") or k.get("llm_factory")(), port)
    k.provide("llm:scripted", fake)
    project = Project.create("write hello.txt containing hello", cfg, name="net", workdir=str(tmp_path / "wd"))
    proxy = socket.socket()
    stages, up = [], threading.Event()

    def bring_proxy_back():
        time.sleep(1.0)
        proxy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        proxy.bind(("127.0.0.1", port))
        proxy.listen(50)
        up.set()

    def watch():
        while not up.is_set():
            stages.append(stage_of(project, project.board.list()))
            time.sleep(0.05)

    threading.Thread(target=bring_proxy_back, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    try:
        status = Manager(k, project).run()
    finally:
        proxy.close()
    kinds = [e["kind"] for e in project.log.read()]
    assert status == "done" and fake.refused >= 1
    assert "manager/network_wait" in kinds and "manager/network_back" in kinds
    assert "network_wait" in stages
    assert not {"manager/escalated", "manager/replan", "manager/doctor_asked"} & set(kinds)
    assert project.meta.get("network_wait") in (None, {})
    assert all(t["attempts"] == 0 for t in project.board.list())      # the outage is nobody's attempt


def test_dashboard_names_the_wait():
    from pathlib import Path

    import artel.web
    from artel.analytics import PROJECT_STATUS

    html = (Path(artel.web.__file__).parent / "index.html").read_text(encoding="utf-8")
    assert "network_wait:'нет сети / прокси недоступен, жду'" in html
    assert PROJECT_STATUS["network_wait"] == "нет сети / прокси недоступен, жду"


def test_stop_during_planning_network_wait_ends_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                          # nothing listens: the wait never ends by itself
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05, "network_probe_s": 0.05,
                                      "network_probe_max_s": 0.2})
    k = new_kernel(cfg)
    k.provide("llm:scripted", ProxyDownProvider(k.get("llm") or k.get("llm_factory")(), port))
    project = Project.create("write hello.txt containing hello", cfg, name="net", workdir=str(tmp_path / "wd"))
    m = Manager(k, project)
    threading.Timer(0.6, m.stop.set).start()
    assert m.run() == "stopped"
    assert project.meta["status"] == "stopped"
