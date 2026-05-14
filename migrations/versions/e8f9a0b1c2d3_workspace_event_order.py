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
    # This migration rewrites historical workspace_events rows. Keep it
    # transactional, but fail promptly instead of waiting indefinitely behind
    # production writers or holding locks longer than the deploy budget.
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '10min'"))
    op.add_column("workspace_events", sa.Column("event_order", sa.Integer(), nullable=True))
    op.execute(
        sa.text(
            """
            WITH ordered_events AS (
                SELECT
                    id,
                    (row_number() OVER (
                        PARTITION BY workspace_id
                        ORDER BY occurred_at ASC, id ASC
                    ))::integer AS backfilled_event_order
                FROM workspace_events
            )
            UPDATE workspace_events
            SET event_order = ordered_events.backfilled_event_order
            FROM ordered_events
            WHERE workspace_events.id = ordered_events.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH event_order_bounds AS (
                SELECT
                    workspace_id,
                    max(event_order) AS max_event_order
                FROM workspace_events
                GROUP BY workspace_id
            )
            UPDATE workspaces
            SET version = event_order_bounds.max_event_order
            FROM event_order_bounds
            WHERE workspaces.id = event_order_bounds.workspace_id
              AND workspaces.version < event_order_bounds.max_event_order
            """
        )
    )
    # Concurrent index creation cannot run inside Alembic's migration
    # transaction. Keep the data backfill transactional above, then release the
    # transaction before building the read-path index without blocking writers.
    with op.get_context().autocommit_block():
        op.execute(sa.text("SET lock_timeout = '5s'"))
        op.execute(sa.text("SET statement_timeout = '10min'"))
        op.create_index(
            "ix_workspace_events_workspace_occurred_order",
            "workspace_events",
            ["workspace_id", "occurred_at", "event_order"],
            unique=False,
            postgresql_concurrently=True,
        )
        op.execute(sa.text("RESET lock_timeout"))
        op.execute(sa.text("RESET statement_timeout"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_workspace_events_workspace_occurred_order",
            table_name="workspace_events",
            postgresql_concurrently=True,
        )
    op.drop_column("workspace_events", "event_order")
