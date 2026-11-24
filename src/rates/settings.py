"""Runtime configuration from environment variables (and an optional .env file).

Policy values (grace period, max age, publication times) are deliberately NOT here:
they live in the versioned ``policies`` table so that changes are auditable.
"""

from datetime import time

from pydantic import Field
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
