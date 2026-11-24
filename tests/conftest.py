"""Shared fixtures.

Integration tests run on a fresh SQLite file by default. Set ``TEST_DATABASE_URL``
(e.g. ``postgresql+asyncpg://rates:rates@localhost:5433/rates``) to run the same
suite against Postgres; the schema is reset with Alembic before every test.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.db.session import create_engine

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def is_postgres() -> bool:
    return bool(os.environ.get("TEST_DATABASE_URL"))


@pytest.fixture
def database_url(tmp_path: Path) -> Iterator[str]:
    """Empty database (no schema)."""
    pg_url = os.environ.get("TEST_DATABASE_URL")
    if pg_url:
        command.downgrade(alembic_config(pg_url), "base")
        yield pg_url
    else:
        yield f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def migrated_url(database_url: str) -> str:
    """Database migrated to head, including seed data."""
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_url)
    try:
        yield eng
    finally:
        await eng.dispose()
