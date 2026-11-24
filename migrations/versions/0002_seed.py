"""Seed data: supported pairs, policy v1, BoI non-publication calendar 2026-2027.

The calendar is transcribed from the Bank of Israel Markets Department document
"List of Dates Affecting Bank of Israel's Services in 2026-2027", section 1
("Days on which the Bank of Israel will not publish representative exchange rates").
Weekends are implicit and not listed. Add a new migration for each new year.

Revision ID: 0002_seed
Revises: 0001_schema
Create Date: 2026-11-24
"""
from collections.abc import Sequence
from datetime import UTC, date, datetime, time

import sqlalchemy as sa
from alembic import op

from rates.adapters.db.types import UtcDateTime

revision: str = "0002_seed"
down_revision: str | None = "0001_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEEDED_AT = datetime(2026, 11, 24, tzinfo=UTC)
SEEDED_BY = "migration:0002_seed"
BOI_CALENDAR_REF = (
    "https://www.boi.org.il/media/c0lhii5f/"
    "list-of-holidays-affecting-bank-of-israel-services-in-2026-2027.pdf"
)

PAIRS = [
    {"base_currency": "USD", "quote_currency": "ILS", "enabled": True, "source_key": "USD"},
    {"base_currency": "EUR", "quote_currency": "ILS", "enabled": True, "source_key": "EUR"},
    {"base_currency": "GBP", "quote_currency": "ILS", "enabled": True, "source_key": "GBP"},
]

NON_PUBLICATION_DAYS: list[tuple[date, str]] = [
    # 2026
    (date(2026, 1, 1), "Holiday Abroad"),
    (date(2026, 3, 3), "Feast of Purim (Tel Aviv)"),
    (date(2026, 3, 4), "Feast of Purim (Jerusalem)"),
    (date(2026, 4, 1), "Eve of Pesach (Passover)"),
    (date(2026, 4, 2), "Pesach (Passover) 1st day"),
    (date(2026, 4, 3), "Holiday Abroad"),
    (date(2026, 4, 6), "Holiday Abroad"),
    (date(2026, 4, 8), "Last Day of Pesach (Passover)"),
    (date(2026, 4, 22), "Israel Independence Day"),
    (date(2026, 5, 22), "Feast of Shavout (Pentecost)"),
    (date(2026, 5, 25), "Holiday Abroad"),
    (date(2026, 7, 23), "Fast of Ninth of Av"),
    (date(2026, 9, 11), "Eve of Rosh Hashanah (New Year)"),
    (date(2026, 9, 21), "Yom Kippur (Atonement Day)"),
    (date(2026, 10, 27), "Elections for the Knesset"),
    (date(2026, 12, 25), "Holiday Abroad"),
    # 2027
    (date(2027, 1, 1), "Holiday Abroad"),
    (date(2027, 3, 23), "Feast of Purim (Tel Aviv)"),
    (date(2027, 3, 24), "Feast of Purim (Jerusalem)"),
    (date(2027, 3, 26), "Holiday Abroad"),
    (date(2027, 4, 21), "Eve of Pesach (Passover)"),
    (date(2027, 4, 22), "Pesach (Passover) 1st day"),
    (date(2027, 4, 28), "Last Day of Pesach (Passover)"),
    (date(2027, 5, 12), "Israel Independence Day"),
    (date(2027, 5, 31), "Holiday Abroad"),
    (date(2027, 6, 11), "Feast of Shavout (Pentecost)"),
    (date(2027, 8, 12), "Fast of Ninth of Av"),
    (date(2027, 10, 1), "Eve of Rosh Hashanah (New Year)"),
    (date(2027, 10, 11), "Yom Kippur (Atonement Day)"),
]

COVERED_YEARS = [2026, 2027]

currency_pairs = sa.table(
    "currency_pairs",
    sa.column("base_currency", sa.String),
    sa.column("quote_currency", sa.String),
    sa.column("enabled", sa.Boolean),
    sa.column("source_key", sa.String),
)
policies = sa.table(
    "policies",
    sa.column("version", sa.Integer),
    sa.column("grace_minutes", sa.Integer),
    sa.column("max_age_days", sa.Integer),
    sa.column("weekday_publish_time", sa.Time),
    sa.column("friday_publish_time", sa.Time),
    sa.column("timezone", sa.String),
    sa.column("active", sa.Boolean),
    sa.column("created_at", UtcDateTime),
)
non_publication_days = sa.table(
    "non_publication_days",
    sa.column("day", sa.Date),
    sa.column("reason", sa.String),
    sa.column("origin", sa.String),
    sa.column("created_by", sa.String),
    sa.column("created_at", UtcDateTime),
)
calendar_coverage = sa.table(
    "calendar_coverage",
    sa.column("year", sa.Integer),
    sa.column("source_ref", sa.String),
)


def upgrade() -> None:
    op.bulk_insert(currency_pairs, PAIRS)
    op.bulk_insert(
        policies,
        [
            {
                "version": 1,
                "grace_minutes": 45,
                "max_age_days": 7,
                "weekday_publish_time": time(15, 15),
                "friday_publish_time": time(12, 15),
                "timezone": "Asia/Jerusalem",
                "active": True,
                "created_at": SEEDED_AT,
            }
        ],
    )
    op.bulk_insert(
        non_publication_days,
        [
            {
                "day": day,
                "reason": reason,
                "origin": "BOI_CALENDAR",
                "created_by": SEEDED_BY,
                "created_at": SEEDED_AT,
            }
            for day, reason in NON_PUBLICATION_DAYS
        ],
    )
    op.bulk_insert(
        calendar_coverage,
        [{"year": year, "source_ref": BOI_CALENDAR_REF} for year in COVERED_YEARS],
    )


def downgrade() -> None:
    op.execute(calendar_coverage.delete().where(calendar_coverage.c.year.in_(COVERED_YEARS)))
    op.execute(
        non_publication_days.delete().where(non_publication_days.c.origin == "BOI_CALENDAR")
    )
    op.execute(policies.delete().where(policies.c.version == 1))
    op.execute(
        currency_pairs.delete().where(
            currency_pairs.c.base_currency.in_(["USD", "EUR", "GBP"]),
            currency_pairs.c.quote_currency == "ILS",
        )
    )
