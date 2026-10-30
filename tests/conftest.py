import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def fresh_env(tmp_path, monkeypatch):
    """Every test gets its own SQLite DB, default settings and real-time clock."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    from app import clock
    from app.config import reset_settings_cache

    reset_settings_cache()
    clock.set_clock(None)
    from app import db

    db.init_db()
    yield
    clock.set_clock(None)
    reset_settings_cache()
