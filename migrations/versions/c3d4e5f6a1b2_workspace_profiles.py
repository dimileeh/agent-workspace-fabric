"""Add workspace profile snapshot columns.

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-04-24 18:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a1b2"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("profile_ref", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("requested_profile", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("resolved_profile", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("resolved_profile")
        batch.drop_column("requested_profile")
        batch.drop_column("profile_ref")
