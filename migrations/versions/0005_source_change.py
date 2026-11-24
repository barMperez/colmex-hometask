"""Store BoI's ``currentChange`` on each observation.

A new ``lastUpdate`` alone does not prove a new publication: BoI can re-stamp an
entry without publishing. ``currentChange`` (the percent change from the previous
publication) is what tells a genuine unchanged-rate publication (change 0) from a
re-stamp of the old entry (the old change). Existing rows get NULL, which the
ingestion treats as unknown and therefore fails closed on.

Revision ID: 0005_source_change
Revises: 0004_revisions_and_exact_numeric
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from rates.adapters.db.types import DecimalText

revision: str = "0005_source_change"
down_revision: str | None = "0004_revisions_and_exact_numeric"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rate_observations") as batch:
        batch.add_column(sa.Column("source_change", DecimalText(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rate_observations") as batch:
        batch.drop_column("source_change")
