import pytest


@pytest.fixture(autouse=True)
def _isolate_user_cache(tmp_path, monkeypatch):
    """Keep tests from reading/writing the user's real ~/.cache/gearcore."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
