"""Grants on the project page: API payload, index.html hook and docs."""
from pathlib import Path

import pytest

from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.web.server import Runner, project_view

ROOT = Path(__file__).resolve().parents[1]


def test_project_view_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    ref = tmp_path / "ref"
    ref.mkdir()
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("goal", cfg, name="g", refs=(ref,))
    view = project_view(project, Runner(cfg))
    g = view["grants"]
    assert str(project.shared.resolve()) in g["write"]
    assert str(project.shared.resolve()) in g["read"]
    assert str(ref.resolve()) in g["refs"] and str(ref.resolve()) in g["read"]
    assert str(ref.resolve()) not in g["write"]


def test_index_renders_grants_section():
    html = (ROOT / "artel/web/index.html").read_text()
    assert "v.grants.write" in html and "v.grants.read" in html and "v.grants.refs" in html
    assert "esc" in html.split("v.grants.write", 1)[1][:80]


def test_docs_exist():
    doc = (ROOT / "docs/grants.md").read_text()
    for word in ("shared", "--add-dir", "denied", "roles", "refs"):
        assert word in doc
