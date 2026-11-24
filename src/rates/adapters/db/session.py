"""Engine construction. The database is selected only by ``DATABASE_URL``."""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str, **kwargs: Any) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    return engine


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        # WAL: readers don't block the single writer (the refresh process).
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()
