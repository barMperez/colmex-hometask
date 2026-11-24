"""Dialect differences live here and nowhere else.

* ``DecimalText``  - exact decimals: TEXT on SQLite (which has no decimal type and
  would otherwise round-trip through REAL), NUMERIC(18,10) on Postgres.
* ``UtcDateTime``  - aware UTC datetimes: fixed-width ISO-8601 TEXT on SQLite (which
  drops timezones and so sorts correctly as text), TIMESTAMPTZ on Postgres.
* ``JsonDocument`` - JSON on SQLite, JSONB on Postgres.
* ``insert_ignore`` - dialect-specific INSERT ... ON CONFLICT DO NOTHING.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, Dialect, String, Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.types import NUMERIC, TypeDecorator, TypeEngine

from rates.domain.decimals import canonical_decimal, decimal_to_plain_string

__all__ = ["DecimalText", "UtcDateTime", "JsonDocument", "insert_ignore", "canonical_decimal"]

DECIMAL_PRECISION = 18
DECIMAL_SCALE = 10


class DecimalText(TypeDecorator[Decimal]):
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(NUMERIC(DECIMAL_PRECISION, DECIMAL_SCALE, asdecimal=True))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"DecimalText requires Decimal, got {type(value).__name__}")
        if not value.is_finite():
            raise ValueError("DecimalText requires a finite Decimal")
        if dialect.name == "postgresql":
            return value
        return decimal_to_plain_string(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):  # defensive: never let a float through silently
            raise TypeError("Database returned a float for a DecimalText column")
        return canonical_decimal(value if isinstance(value, Decimal) else Decimal(value))


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.TIMESTAMP(timezone=True))
        return dialect.type_descriptor(String(32))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UtcDateTime requires datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UtcDateTime refuses naive datetimes")
        value = value.astimezone(UTC)
        if dialect.name == "postgresql":
            return value
        return value.isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            raise ValueError("Database returned a naive datetime for a UtcDateTime column")
        return value.astimezone(UTC)


JsonDocument = JSON().with_variant(postgresql.JSONB(), "postgresql")


def insert_ignore(dialect_name: str, table: Table, conflict_columns: list[str]) -> Any:
    """INSERT ... ON CONFLICT (conflict_columns) DO NOTHING for the given dialect."""
    if dialect_name == "postgresql":
        return postgresql.insert(table).on_conflict_do_nothing(index_elements=conflict_columns)
    if dialect_name == "sqlite":
        return sqlite.insert(table).on_conflict_do_nothing(index_elements=conflict_columns)
    raise NotImplementedError(f"insert_ignore not implemented for dialect {dialect_name!r}")
