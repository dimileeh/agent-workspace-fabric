"""add operations created_at id index

Revision ID: 7a8b9c0d1e2f
Revises: 9fe4720d0ffa
Create Date: 2026-04-25 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7a8b9c0d1e2f"
down_revision: str | Sequence[str] | None = "9fe4720d0ffa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_operations_created_at_id",
        "operations",
        ["created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_operations_created_at_id", table_name="operations")
