"""Same-timestamp corrections as revisions; exact NUMERIC on Postgres.

- ``rate_observations.revision`` (default 0): BoI can restate a rate without changing
  ``lastUpdate``; each distinct value becomes the next revision, so the unique key
  becomes ``(pair, source_last_update, revision)`` and "latest" orders by revision too.
- On Postgres, ``rate`` and ``source_rate`` become unconstrained ``NUMERIC``
  (``NUMERIC(18,10)`` silently rounded beyond 10 decimal places). Widening is lossless.

Revision ID: 0004_revisions_and_exact_numeric
Revises: 0003_refresh_status
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_revisions_and_exact_numeric"
down_revision: str | None = "0003_refresh_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_UNIQUE = "uq_rate_observations_pair_source_ts"
NEW_UNIQUE = "uq_rate_observations_pair_source_ts_revision"
LATEST_INDEX = "ix_rate_observations_latest"


def upgrade() -> None:
    op.drop_index(LATEST_INDEX, table_name="rate_observations")
    with op.batch_alter_table("rate_observations") as batch:
        batch.add_column(
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch.drop_constraint(OLD_UNIQUE, type_="unique")
        batch.create_unique_constraint(
            NEW_UNIQUE, ["base_currency", "quote_currency", "source_last_update", "revision"]
        )
        batch.create_check_constraint("ck_rate_observations_revision_non_negative", "revision >= 0")
    op.create_index(
        LATEST_INDEX,
        "rate_observations",
        [
            "base_currency",
            "quote_currency",
            sa.text("publication_date DESC"),
            sa.text("source_last_update DESC"),
            sa.text("revision DESC"),
        ],
    )
    if op.get_bind().dialect.name == "postgresql":
        for column in ("rate", "source_rate"):
            op.alter_column("rate_observations", column, type_=sa.Numeric(), existing_nullable=False)


def downgrade() -> None:
    corrections = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM rate_observations WHERE revision > 0")
    ).scalar_one()
    if corrections:
        raise RuntimeError(
            f"Refusing to downgrade 0004: {corrections} same-timestamp correction(s) are stored "
            "(revision > 0). They would violate the previous unique key; deleting them would "
            "destroy history. Roll forward instead."
        )
    if op.get_bind().dialect.name == "postgresql":
        for column in ("rate", "source_rate"):
            op.alter_column(
                "rate_observations", column, type_=sa.Numeric(18, 10), existing_nullable=False
            )
    op.drop_index(LATEST_INDEX, table_name="rate_observations")
    with op.batch_alter_table("rate_observations") as batch:
        batch.drop_constraint("ck_rate_observations_revision_non_negative", type_="check")
        batch.drop_constraint(NEW_UNIQUE, type_="unique")
        batch.create_unique_constraint(OLD_UNIQUE, ["base_currency", "quote_currency", "source_last_update"])
        batch.drop_column("revision")
    op.create_index(
        LATEST_INDEX,
        "rate_observations",
        [
            "base_currency",
            "quote_currency",
            sa.text("publication_date DESC"),
            sa.text("source_last_update DESC"),
        ],
    )
