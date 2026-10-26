import os

import pytest


@pytest.fixture(autouse=True)
def fast_manager_tick(monkeypatch):
    """The manager loop polls every TICK_S; shrink it so scripted runs don't spend the suite sleeping."""
    import artel.manager
    monkeypatch.setattr(artel.manager, "TICK_S", 0.02)


@pytest.fixture(autouse=True)
def git_identity_and_line_endings(monkeypatch):
    """Tests commit into throwaway repositories: give git an identity when the machine has none (CI runners), and
    keep files byte-for-byte (Git for Windows defaults to core.autocrlf=true)."""
    for var, value in (("GIT_AUTHOR_NAME", "artel-tests"), ("GIT_AUTHOR_EMAIL", "tests@artel.invalid"),
                       ("GIT_COMMITTER_NAME", "artel-tests"), ("GIT_COMMITTER_EMAIL", "tests@artel.invalid")):
        if not os.environ.get(var):
            monkeypatch.setenv(var, value)
    if "GIT_CONFIG_COUNT" not in os.environ:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "commit.gpgsign")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")


@pytest.fixture(autouse=True)
def no_host_evolve_process(monkeypatch):
    """Tests must not depend on a real `artel evolve` running on the machine (the repo lock refuses to start then)."""
    import artel.evolve.supervisor as sv
    monkeypatch.setattr(sv, "evolve_process_running", lambda **kw: False)


@pytest.fixture(autouse=True)
def fake_cli_env_passes_the_allowlist(monkeypatch):
    """The fake CLIs in tests/fakes are steered by FAKE_* variables; the agent env allowlist must let them through."""
    import re

    import artel.agent_env
    monkeypatch.setattr(artel.agent_env, "COMMON_RE", re.compile(r"^(LC_.*|XDG_.*|FAKE_.*)$"))
