"""Add workspace event ordering key.

Revision ID: e8f9a0b1c2d3
Revises: d6e7f8a9b0c1
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspace_events", sa.Column("event_order", sa.Integer(), nullable=True))
    op.create_index(
        "ix_workspace_events_workspace_occurred_order",
        "workspace_events",
        ["workspace_id", "occurred_at", "event_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_events_workspace_occurred_order", table_name="workspace_events")
    op.drop_column("workspace_events", "event_order")
