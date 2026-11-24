from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from rates.clock import FixedClock, SystemClock
from rates.settings import Settings


def test_settings_read_env_overrides(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/x")
    monkeypatch.setenv("REFRESH_WINDOW_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("REFRESH_WINDOW_START", "11:30")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://u:p@db/x"
    assert settings.refresh_window_interval_seconds == 60
    assert settings.refresh_window_start == time(11, 30)


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("sqlite+aiosqlite://")
    assert settings.boi_max_attempts == 3
    assert settings.refresh_dense_interval_seconds == 60
    assert settings.refresh_dense_tail_minutes == 30
    assert settings.calendar_warning_days == 60
    assert settings.jwt_secret is None
    assert settings.jwt_ttl_seconds == 3600
    assert settings.demo_username == "demo"
    assert settings.demo_password is None


def test_fixed_clock_returns_aware_utc():
    israel = timezone(timedelta(hours=2))
    clock = FixedClock(datetime(2026, 11, 24, 16, 0, tzinfo=israel))
    now = clock.now()
    assert now.tzinfo is UTC
    assert now == datetime(2026, 11, 24, 14, 0, tzinfo=UTC)

    clock.advance(timedelta(minutes=5))
    assert clock.now() == datetime(2026, 11, 24, 14, 5, tzinfo=UTC)


def test_fixed_clock_rejects_naive():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 11, 24, 12, 0))


def test_system_clock_is_aware():
    assert SystemClock().now().utcoffset() == timedelta(0)
