import subprocess
from pathlib import Path

import pytest

from artel.updater import UP_TO_DATE, UpdateError, Updater

ROADMAP = """items:
  - id: W1-x
    status: STATUS
    title: Feature X
    source: {harness: Donor}
"""


def g(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


@pytest.fixture
def repos(tmp_path):
    up = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(up)], check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(up), str(dev)], check=True)
    (dev / "tests").mkdir()
    (dev / "tests" / "test_invariants.py").write_text("def test_pillar():\n    assert True\n", encoding="utf-8")
    (dev / "roadmap.yaml").write_text(ROADMAP.replace("STATUS", "planned"), encoding="utf-8")
    g(dev, "checkout", "-q", "-b", "master")
    g(dev, "add", "-A")
    g(dev, "commit", "-q", "-m", "v1")
    g(dev, "push", "-q", "origin", "master")
    inst = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", str(up), str(inst)], check=True)
    return dev, inst, Updater(root=inst, home=tmp_path / "home")


def publish(dev, msg, files):
    for rel, text in files.items():
        (dev / rel).write_text(text, encoding="utf-8")
    g(dev, "add", "-A")
    g(dev, "commit", "-q", "-m", msg)
    g(dev, "push", "-q", "origin", "master")
    return g(dev, "rev-parse", "HEAD")


def test_update_shows_news_verifies_applies_and_rolls_back(repos):
    dev, inst, up = repos
    assert up.status()["reason"] == UP_TO_DATE
    v1 = g(inst, "rev-parse", "HEAD")
    v2 = publish(dev, "evolve gen 3: adopt W1-x", {"roadmap.yaml": ROADMAP.replace("STATUS", "done"), "x.py": "X = 1\n"})
    st = up.status()
    assert st["available"] and st["can_update"] and st["behind"] == 1
    assert st["commits"][0]["subject"] == "evolve gen 3: adopt W1-x"
    assert st["roadmap_done"] == [{"id": "W1-x", "title": "Feature X", "from": "Donor"}]
    out = up.apply()
    assert out["updated"] and g(inst, "rev-parse", "HEAD") == v2 and (inst / "x.py").exists()
    assert out["roadmap_done"] == ["W1-x"]
    back = up.rollback()
    assert back["rolled_back"] and g(inst, "rev-parse", "HEAD") == v1 and not (inst / "x.py").exists()
    assert [h["action"] for h in up.history()] == ["update", "rollback"]


def test_broken_version_is_never_installed(repos):
    dev, inst, up = repos
    v1 = g(inst, "rev-parse", "HEAD")
    publish(dev, "breaks a pillar", {"tests/test_invariants.py": "def test_pillar():\n    assert False\n"})
    with pytest.raises(UpdateError, match="fails its invariants checks"):
        up.apply()
    assert g(inst, "rev-parse", "HEAD") == v1
    assert up.history()[-1]["action"] == "refused"
    assert not g(inst, "worktree", "list").count("\n")  # the temporary worktree is gone


def test_local_edits_and_divergence_block_updates(repos):
    dev, inst, up = repos
    publish(dev, "v2", {"y.py": "Y = 1\n"})
    (inst / "roadmap.yaml").write_text("local edit\n", encoding="utf-8")
    st = up.status()
    assert st["available"] and not st["can_update"] and "локальные изменения" in st["reason"]
    with pytest.raises(UpdateError, match="локальные изменения"):
        up.apply()


def test_dashboard_update_endpoints(repos, tmp_path, monkeypatch):
    from artel.project import DEFAULT_CONFIG, deep_merge
    from artel.web.server import Runner

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "h"))
    dev, inst, up = repos
    runner = Runner(deep_merge(DEFAULT_CONFIG, {"provider": "scripted"}))
    runner.updater = up
    v2 = publish(dev, "new feature", {"z.py": "Z = 1\n"})
    st = runner.update_status(fetch=True)
    assert st["available"] and st["state"]["phase"] == "idle"
    out = runner.update_apply()
    assert out["ok"] and runner.restart_pending and runner.update_state["phase"] == "restart_pending"
    assert g(inst, "rev-parse", "HEAD") == v2


def test_without_pytest_the_update_is_smoke_checked_not_refused(repos, monkeypatch):
    """Installations made by the installer had no pytest, so every update was refused. pytest is installed on
    first use; when even that is impossible (offline), the new version must at least import and start its CLI."""
    dev, inst, up = repos
    monkeypatch.setattr(Updater, "_ensure_pytest", staticmethod(lambda: False))
    cli = "def main(argv=None):\n    raise SystemExit(0)\n"
    v2 = publish(dev, "v2", {"artel/__init__.py": "", "artel/cli.py": cli}) if (dev / "artel").mkdir() is None else None
    up.status(fetch=True)
    assert up.verify(v2)["mode"].startswith("smoke") and up.apply()["updated"]
    publish(dev, "v3 breaks the import", {"artel/cli.py": "import no_such_module\n" + cli})
    with pytest.raises(UpdateError, match="smoke"):
        up.apply()
    assert g(inst, "rev-parse", "HEAD") == v2


def test_ctrl_c_stops_the_dashboard_and_commands_without_a_traceback(tmp_path, monkeypatch, capsys):
    """A person stops `artel web` with Ctrl+C; a KeyboardInterrupt traceback looked like a crash."""
    import socketserver

    from artel import cli
    from artel.project import DEFAULT_CONFIG, deep_merge
    from artel.web import server

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))

    def interrupted(self, *a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(socketserver.BaseServer, "serve_forever", interrupted)
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "evolve": {"auto_resume": False},
                                      "update": {"check_interval_min": 0}})
    server.serve(port=0, config=cfg)
    out = capsys.readouterr()
    assert "Artel остановлен." in out.out and "Traceback" not in out.out + out.err
    monkeypatch.setattr(cli, "cmd_web", interrupted)
    assert cli.main(["web"]) == 130 and "Остановлено." in capsys.readouterr().err
