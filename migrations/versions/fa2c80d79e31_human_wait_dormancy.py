"""Persist opt-in human wait dormancy.

Revision ID: fa2c80d79e31
Revises: 9e3b7c41d2a6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa2c80d79e31"
down_revision: str | Sequence[str] | None = "9e3b7c41d2a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
destructive: bool = False


def upgrade() -> None:
    op.add_column(
        "workspaces", sa.Column("human_wait_dormant_after_seconds", sa.Float(), nullable=True)
    )
    op.add_column("workspaces", sa.Column("dormant_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "dormant_at")
    op.drop_column("workspaces", "human_wait_dormant_after_seconds")
