from pathlib import Path

import pytest


def test_from_env_rejects_missing_admin_password(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INTEL_DB_PATH", str(tmp_path / "intel.db"))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.delenv("INTEL_ADMIN_PASSWORD", raising=False)
    from intel_daily.config import AppSettings

    with pytest.raises(ValueError, match="INTEL_ADMIN_PASSWORD"):
        AppSettings.from_env()


def test_from_env_rejects_default_password(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INTEL_DB_PATH", str(tmp_path / "intel.db"))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "changeme")
    from intel_daily.config import AppSettings

    with pytest.raises(ValueError, match="INTEL_ADMIN_PASSWORD"):
        AppSettings.from_env()


def test_from_env_rejects_missing_db_path(monkeypatch) -> None:
    monkeypatch.delenv("INTEL_DB_PATH", raising=False)
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "ops")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse-battery")
    from intel_daily.config import AppSettings

    with pytest.raises(ValueError, match="INTEL_DB_PATH"):
        AppSettings.from_env()


def test_from_env_loads_valid_settings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INTEL_DB_PATH", str(tmp_path / "intel.db"))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "ops")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse-battery")
    from intel_daily.config import AppSettings

    settings = AppSettings.from_env()

    assert settings.db_path == tmp_path / "intel.db"
    assert settings.admin_username == "ops"
    assert settings.admin_password == "correct-horse-battery"
