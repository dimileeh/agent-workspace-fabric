"""Add monitor recovery claim lease columns.

Revision ID: 0a1b2c3d4e5f
Revises: 9b1c2d3e4f5a
Create Date: 2026-04-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0a1b2c3d4e5f"
down_revision: str | Sequence[str] | None = "9b1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("monitor_claimed_by", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("monitor_claim_expires_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("monitor_claim_expires_at")
        batch.drop_column("monitor_claimed_by")
