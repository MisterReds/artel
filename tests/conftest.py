import pytest

from artel import manager


@pytest.fixture(autouse=True)
def fast_manager_tick(monkeypatch):
    """The scripted runs are instant; a 0.5s scheduler tick only makes the suite crawl."""
    monkeypatch.setattr(manager, "TICK_S", 0.02)
