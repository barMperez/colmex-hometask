"""Refresh status: one row recording the refresh process's last attempt and success.

Revision ID: 0003_refresh_status
Revises: 0002_seed
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from rates.adapters.db.types import UtcDateTime

revision: str = "0003_refresh_status"
down_revision: str | None = "0002_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "refresh_status",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_attempt_at", UtcDateTime(), nullable=True),
        sa.Column("last_attempt_outcome", sa.String(32), nullable=True),
        sa.Column("last_success_at", UtcDateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_refresh_status_single_row"),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_status"),
    )
    op.bulk_insert(table, [{"id": 1, "consecutive_failures": 0}])


def downgrade() -> None:
    op.drop_table("refresh_status")
