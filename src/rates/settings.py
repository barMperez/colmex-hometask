"""Runtime configuration from environment variables (and an optional .env file).

Policy values (grace period, max age, publication times) are deliberately NOT here:
they live in the versioned ``policies`` table so that changes are auditable.
"""

from datetime import time

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./rates.db"

    boi_url: str = "https://boi.org.il/PublicApi/GetExchangeRates"
    boi_timeout_seconds: float = Field(default=10.0, gt=0)
    boi_max_attempts: int = Field(default=3, ge=1)

    refresh_window_interval_seconds: int = Field(default=300, ge=1)
    refresh_off_window_interval_seconds: int = Field(default=3600, ge=1)
    refresh_window_start: time = time(12, 0)
    refresh_window_end: time = time(18, 0)
    refresh_dense_interval_seconds: int = Field(default=60, ge=1)
    """Interval from the expected publication time until the deadline plus the tail."""
    refresh_dense_tail_minutes: int = Field(default=30, ge=0)

    calendar_warning_days: int = Field(default=60, ge=0)
    """Warn when the loaded BoI calendar ends within this many days."""

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_workers: int = Field(default=1, ge=1)

    log_level: str = "INFO"

    # Demo authentication (API process only). Values for the demo user are plain
    # configuration: there is no user store and no password hashing.
    jwt_secret: SecretStr | None = None
    """HS256 signing secret shared by all API workers; at least 32 characters."""
    jwt_ttl_seconds: int = Field(default=3600, ge=60)
    demo_username: str = "demo"
    demo_password: SecretStr | None = None


MIN_JWT_SECRET_LENGTH = 32


def require_api_settings(settings: "Settings") -> None:
    """Fail fast when the API process lacks its authentication settings.

    The refresh process does not authenticate anyone and never calls this.
    """
    secret = settings.jwt_secret.get_secret_value() if settings.jwt_secret else ""
    if not secret:
        raise RuntimeError("JWT_SECRET is not set: the API needs a token-signing secret")
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        raise RuntimeError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
    if not settings.demo_password or not settings.demo_password.get_secret_value():
        raise RuntimeError("DEMO_PASSWORD is not set: the demo user needs a password")
