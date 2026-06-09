"""add workspaces status updated_at index

Revision ID: 2c3d4e5f6a1b
Revises: 1b2c3d4e5f6a
Create Date: 2026-04-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "2c3d4e5f6a1b"
down_revision: str | Sequence[str] | None = "1b2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workspaces_status_updated_at",
        "workspaces",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_status_updated_at", table_name="workspaces")
