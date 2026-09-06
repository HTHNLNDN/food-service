import pytest


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path, monkeypatch):
    """Keep every test's SQLite file inside a throwaway tmp dir."""
    monkeypatch.setenv("FOOD_SERVICE_DATA_DIR", str(tmp_path))
