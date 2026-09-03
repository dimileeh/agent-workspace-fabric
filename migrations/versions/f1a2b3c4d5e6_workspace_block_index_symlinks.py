"""Persist pre-agent symlink checkout mode for validation cleanliness.

Revision ID: f1a2b3c4d5e6
Revises: e9f0a1b2c3d4
Create Date: 2026-09-03 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("block_index_symlinks_are_symlinks", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "block_index_symlinks_are_symlinks")
