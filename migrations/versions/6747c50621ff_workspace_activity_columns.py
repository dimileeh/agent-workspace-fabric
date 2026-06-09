"""Add workspace subphase and activity tracking columns.

Revision ID: 6747c50621ff
Revises: b1c2d3e4f6a7
Create Date: 2026-05-05 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6747c50621ff"
down_revision: str | Sequence[str] | None = "b1c2d3e4f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("subphase", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("last_log_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "last_log_at")
    op.drop_column("workspaces", "last_activity_at")
    op.drop_column("workspaces", "subphase")
