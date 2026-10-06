import pytest


@pytest.fixture(autouse=True)
def fast_manager_tick(monkeypatch):
    """The manager loop polls every TICK_S; shrink it so scripted runs don't spend the suite sleeping."""
    import artel.manager
    monkeypatch.setattr(artel.manager, "TICK_S", 0.02)
