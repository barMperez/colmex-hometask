"""Initial schema.

Revision ID: 0001_schema
Revises:
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from rates.adapters.db.types import DecimalText, JsonDocument, UtcDateTime

revision: str = "0001_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "currency_pairs",
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source_key", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("base_currency", "quote_currency", name="pk_currency_pairs"),
    )

    op.create_table(
        "rate_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("rate", DecimalText(), nullable=False),
        sa.Column("source_rate", DecimalText(), nullable=False),
        sa.Column("source_unit", sa.Integer(), nullable=False),
        sa.Column("publication_date", sa.Date(), nullable=False),
        sa.Column("source_last_update", UtcDateTime(), nullable=False),
        sa.Column("retrieved_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rate_observations"),
        sa.ForeignKeyConstraint(
            ["base_currency", "quote_currency"],
            ["currency_pairs.base_currency", "currency_pairs.quote_currency"],
            name="fk_rate_observations_base_currency_currency_pairs",
        ),
        sa.UniqueConstraint(
            "base_currency", "quote_currency", "source_last_update",
            name="uq_rate_observations_pair_source_ts",
        ),
        sa.CheckConstraint("source_unit > 0", name="ck_rate_observations_positive_unit"),
    )
    op.create_index(
        "ix_rate_observations_latest",
        "rate_observations",
        [
            "base_currency",
            "quote_currency",
            sa.text("publication_date DESC"),
            sa.text("source_last_update DESC"),
        ],
    )

    op.create_table(
        "non_publication_days",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("day", name="pk_non_publication_days"),
        sa.CheckConstraint(
            "origin IN ('BOI_CALENDAR', 'OPERATOR')", name="ck_non_publication_days_origin"
        ),
    )

    op.create_table(
        "calendar_coverage",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("source_ref", sa.String(500), nullable=False),
        sa.PrimaryKeyConstraint("year", name="pk_calendar_coverage"),
    )

    op.create_table(
        "policies",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("grace_minutes", sa.Integer(), nullable=False),
        sa.Column("max_age_days", sa.Integer(), nullable=False),
        sa.Column("weekday_publish_time", sa.Time(), nullable=False),
        sa.Column("friday_publish_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("version", name="pk_policies"),
        sa.CheckConstraint("grace_minutes >= 0", name="ck_policies_grace_non_negative"),
        sa.CheckConstraint("max_age_days >= 0", name="ck_policies_max_age_non_negative"),
    )
    op.create_index(
        "uq_policies_single_active",
        "policies",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", UtcDateTime(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(200), nullable=False),
        sa.Column("before", JsonDocument, nullable=True),
        sa.Column("after", JsonDocument, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_index("uq_policies_single_active", table_name="policies")
    op.drop_table("policies")
    op.drop_table("calendar_coverage")
    op.drop_table("non_publication_days")
    op.drop_index("ix_rate_observations_latest", table_name="rate_observations")
    op.drop_table("rate_observations")
    op.drop_table("currency_pairs")
