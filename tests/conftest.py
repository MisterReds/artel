import pytest


@pytest.fixture(autouse=True)
def fast_manager_tick(monkeypatch):
    """Keep the Manager scheduling loop short under test (production default is 0.5s)."""
    import artel.manager

    monkeypatch.setattr(artel.manager, "TICK_S", 0.02)
