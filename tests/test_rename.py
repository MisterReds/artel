"""The product is Arteliq; `artel` stays as a console alias and the import package keeps its name."""
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project():
    """name, description and console scripts from pyproject.toml (no tomllib: Python 3.10 is supported)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    scripts = text.split("[project.scripts]")[1].split("\n[")[0]
    return {
        "name": re.search(r'^name = "([^"]+)"', text, re.M).group(1),
        "description": re.search(r'^description = "([^"]+)"', text, re.M).group(1),
        "scripts": dict(re.findall(r'^([\w-]+) = "([^"]+)"', scripts, re.M)),
    }


def test_distribution_is_arteliq():
    assert _project()["name"] == "arteliq"
    assert "bots with souls" not in _project()["description"]


def test_both_console_scripts_resolve_to_the_same_function():
    scripts = _project()["scripts"]
    assert set(scripts) == {"arteliq", "artel"}
    assert scripts["arteliq"] == scripts["artel"]
    module, func = scripts["arteliq"].split(":")
    assert callable(getattr(importlib.import_module(module), func))


def test_installers_point_at_the_renamed_repository():
    for name in ("install.sh", "install.ps1", "README.md", "roadmap.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "MisterReds/arteliq" in text, name
        assert "MisterReds/artel/" not in text and "MisterReds/artel.git" not in text, name
        assert "MisterReds/artel-agents" not in text, name
