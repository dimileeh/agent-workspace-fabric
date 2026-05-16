"""add operations idempotency index

Revision ID: 8b9c0d1e2f3a
Revises: f7a8b9c0d1e2
Create Date: 2026-04-26 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "8b9c0d1e2f3a"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_operations_idempotency_key_created_at_id",
        "operations",
        ["idempotency_key", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_operations_idempotency_key_created_at_id", table_name="operations")
