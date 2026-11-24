"""Schema (SQLAlchemy Core). Kept in sync with the hand-written Alembic migrations."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)

from rates.adapters.db.types import DecimalText, JsonDocument, UtcDateTime

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

currency_pairs = Table(
    "currency_pairs",
    metadata,
    Column("base_currency", String(3), primary_key=True),
    Column("quote_currency", String(3), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("source_key", String(16), nullable=False),
)

rate_observations = Table(
    "rate_observations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("base_currency", String(3), nullable=False),
    Column("quote_currency", String(3), nullable=False),
    Column("rate", DecimalText, nullable=False),
    Column("source_rate", DecimalText, nullable=False),
    Column("source_unit", Integer, nullable=False),
    Column("publication_date", Date, nullable=False),
    Column("source_last_update", UtcDateTime, nullable=False),
    Column("retrieved_at", UtcDateTime, nullable=False),
    ForeignKeyConstraint(
        ["base_currency", "quote_currency"],
        ["currency_pairs.base_currency", "currency_pairs.quote_currency"],
    ),
    UniqueConstraint(
        "base_currency", "quote_currency", "source_last_update", name="uq_rate_observations_pair_source_ts"
    ),
    CheckConstraint("source_unit > 0", name="positive_unit"),
)
Index(
    "ix_rate_observations_latest",
    rate_observations.c.base_currency,
    rate_observations.c.quote_currency,
    rate_observations.c.publication_date.desc(),
    rate_observations.c.source_last_update.desc(),
)

non_publication_days = Table(
    "non_publication_days",
    metadata,
    Column("day", Date, primary_key=True),
    Column("reason", String(200), nullable=False),
    Column("origin", String(16), nullable=False),
    Column("created_by", String(100), nullable=False),
    Column("created_at", UtcDateTime, nullable=False),
    CheckConstraint("origin IN ('BOI_CALENDAR', 'OPERATOR')", name="origin"),
)

calendar_coverage = Table(
    "calendar_coverage",
    metadata,
    Column("year", Integer, primary_key=True, autoincrement=False),
    Column("source_ref", String(500), nullable=False),
)

policies = Table(
    "policies",
    metadata,
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("grace_minutes", Integer, nullable=False),
    Column("max_age_days", Integer, nullable=False),
    Column("weekday_publish_time", Time, nullable=False),
    Column("friday_publish_time", Time, nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", UtcDateTime, nullable=False),
    CheckConstraint("grace_minutes >= 0", name="grace_non_negative"),
    CheckConstraint("max_age_days >= 0", name="max_age_non_negative"),
)
Index(
    "uq_policies_single_active",
    policies.c.active,
    unique=True,
    postgresql_where=text("active"),
    sqlite_where=text("active"),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("occurred_at", UtcDateTime, nullable=False),
    Column("actor", String(100), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("entity_type", String(64), nullable=False),
    Column("entity_id", String(200), nullable=False),
    Column("before", JsonDocument, nullable=True),
    Column("after", JsonDocument, nullable=True),
    Column("reason", Text, nullable=True),
)
