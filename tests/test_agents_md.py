import pytest

from artel.manager import run_goal
from artel.plugins.scripted import ScriptedProvider
from artel.project import DEFAULT_CONFIG, INSTRUCTIONS_MAX_BYTES, deep_merge, find_instructions

RULE = "ALWAYS-END-REPORTS-WITH-PINEAPPLE"


def test_priority_order(tmp_path):
    (tmp_path / "GEMINI.md").write_text("gem")
    (tmp_path / ".clinerules").write_text("cline")
    assert find_instructions(tmp_path)[1] == "gem"
    (tmp_path / "CLAUDE.md").write_text("claude")
    assert find_instructions(tmp_path)[1] == "claude"
    (tmp_path / "AGENTS.md").write_text("agents")
    path, text = find_instructions(tmp_path)
    assert path.name == "AGENTS.md" and text == "agents"


def test_clinerules_directory(tmp_path):
    d = tmp_path / ".clinerules"
    d.mkdir()
    (d / "a.md").write_text("rule-a")
    (d / "b.md").write_text("rule-b")
    path, text = find_instructions(tmp_path)
    assert path.name == ".clinerules" and "rule-a" in text and "rule-b" in text


def test_nearest_wins_and_walks_to_repo_root(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root")
    assert find_instructions(sub)[1] == "root"  # parent walk reaches the repo root
    (tmp_path / "pkg" / "CLAUDE.md").write_text("pkg")
    assert find_instructions(sub)[1] == "pkg"  # nearer directory beats a higher-priority name further up


def test_walk_stops_at_repo_root(tmp_path):
    (tmp_path / "AGENTS.md").write_text("outside")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert find_instructions(repo) is None
    no_repo = tmp_path / "plain"
    no_repo.mkdir()
    assert find_instructions(no_repo) is None  # no .git: only the work dir itself is searched


def test_cap_and_marker(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * (INSTRUCTIONS_MAX_BYTES * 2))
    _, text = find_instructions(tmp_path)
    assert "truncated" in text and len(text.encode()) < INSTRUCTIONS_MAX_BYTES + 200
    (tmp_path / "AGENTS.md").write_text("small")
    assert "truncated" not in find_instructions(tmp_path)[1]


def test_absent_yields_nothing(tmp_path):
    assert find_instructions(tmp_path) is None
    (tmp_path / "AGENTS.md").write_text("  \n")
    assert find_instructions(tmp_path) is None


@pytest.fixture
def seen(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    log = []
    orig = ScriptedProvider.chat

    def chat(self, *, system, messages, tools, model, effort, max_tokens, purpose=""):
        log.append((purpose, system if isinstance(system, str) else str(system)))
        return orig(self, system=system, messages=messages, tools=tools, model=model, effort=effort,
                    max_tokens=max_tokens, purpose=purpose)

    monkeypatch.setattr(ScriptedProvider, "chat", chat)
    return log


def test_rule_reaches_every_native_role(tmp_path, seen):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "AGENTS.md").write_text(f"Rule: {RULE}")
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, status = run_goal("Write a short guide", cfg, workdir=wd)
    assert status == "done"
    for purpose in ("plan", "work", "review", "audit"):
        systems = [s for p, s in seen if p == purpose]
        assert systems, purpose
        assert all(RULE in s and "<project_instructions" in s for s in systems), purpose
    assert project.meta["instructions_file"] == str((wd / "AGENTS.md").resolve())


def test_no_file_no_block(tmp_path, seen):
    wd = tmp_path / "wd"
    wd.mkdir()
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05})
    project, _ = run_goal("Write a short guide", cfg, workdir=wd)
    assert all("<project_instructions" not in s for _, s in seen)
    assert project.meta.get("instructions_file") is None


def test_cli_runtime_not_duplicated(tmp_path, monkeypatch):
    from artel.manager import Manager
    from artel.project import Project

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "AGENTS.md").write_text(RULE)
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "runtime_by_role": {"worker": "claude_code"}})
    project = Project.create("g", cfg, name="a", workdir=wd)
    m = Manager.__new__(Manager)
    m.project, m.cfg = project, cfg
    assert m.instructions("worker") == ""
    assert RULE in m.instructions("reviewer")
    assert project.meta["instructions_file"].endswith("AGENTS.md")  # still recorded for the project page


def test_project_page_exposes_instruction_file(tmp_path, monkeypatch):
    from pathlib import Path as P
    from artel.project import DEFAULT_CONFIG, Project, deep_merge
    from artel.web.server import Runner, project_view

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("goal", cfg, name="ui")
    project.set_meta(instructions_file="/x/AGENTS.md", instructions_truncated=True)
    meta = project_view(project, Runner(cfg))["meta"]
    assert meta["instructions_file"] == "/x/AGENTS.md" and meta["instructions_truncated"] is True
    html = (P(__file__).resolve().parents[1] / "artel/web/index.html").read_text()
    assert "m.instructions_file?" in html and "m.instructions_truncated" in html


def test_docs_agents_md():
    from pathlib import Path as P
    doc = (P(__file__).resolve().parents[1] / "docs/agents_md.md").read_text()
    for w in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".clinerules", "16 KiB", "native", "nearest"):
        assert w in doc
