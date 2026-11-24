"""The API refuses to start without its authentication settings (the refresh process does not need them)."""

import subprocess
import sys

import pytest

from rates.api.app import create_app
from rates.settings import Settings

SECRET = "x" * 32


def settings(**overrides) -> Settings:
    values = {"jwt_secret": SECRET, "demo_password": "pw"} | overrides
    return Settings(_env_file=None, **values)


def test_valid_settings_create_app():
    assert create_app(settings=settings()) is not None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"jwt_secret": None}, "JWT_SECRET is not set"),
        ({"jwt_secret": "x" * 31}, "at least 32 characters"),
        ({"demo_password": None}, "DEMO_PASSWORD is not set"),
    ],
)
def test_missing_or_weak_settings_refuse_to_start(overrides, message):
    with pytest.raises(RuntimeError, match=message):
        create_app(settings=settings(**overrides))


def test_api_entry_point_exits_with_named_error(monkeypatch):
    env = {"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    result = subprocess.run(
        [sys.executable, "-c", "from rates.api.__main__ import main; main()"],
        capture_output=True, text=True, env=env, timeout=60, cwd=__import__("tempfile").gettempdir(),
    )
    assert result.returncode != 0
    assert "JWT_SECRET is not set" in result.stderr
