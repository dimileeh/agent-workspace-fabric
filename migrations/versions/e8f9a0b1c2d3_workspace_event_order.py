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

_INDEX_DDL_LOCK_NAMESPACE = 0x415746
_INDEX_DDL_LOCK_KEY = 0x45564F52


def upgrade() -> None:
    # This migration rewrites historical workspace_events rows. Keep it
    # transactional, but fail promptly instead of waiting indefinitely behind
    # production writers or holding locks longer than the deploy budget.
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '10min'"))
    op.execute(sa.text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS event_sequence INTEGER"))
    op.execute(
        sa.text("ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS event_order INTEGER")
    )
    # Bootstrap upgrades run migrations before recreating already-running API
    # and worker containers. Keep legacy writers that omit event_order safe
    # during that window by assigning the next workspace-local order in the DB.
    # Old writers can also flush their own ORM-side version bump before this
    # trigger advances the event sequence, but the optimistic-control version
    # remains decoupled from append-only event traffic.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION awf_assign_workspace_event_order()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.event_order IS NULL THEN
                    UPDATE workspaces
                    SET event_sequence = COALESCE(event_sequence, 0) + 1
                    WHERE id = NEW.workspace_id
                    RETURNING event_sequence INTO NEW.event_order;
                END IF;

                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_workspace_events_assign_event_order "
            "ON workspace_events"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_workspace_events_assign_event_order
            BEFORE INSERT ON workspace_events
            FOR EACH ROW
            EXECUTE FUNCTION awf_assign_workspace_event_order()
            """
        )
    )
    # Keep the short lock wait on deploy-facing DDL only. The backfill may need
    # to wait behind ordinary row writers, while statement_timeout still bounds
    # total runtime.
    op.execute(sa.text("SET LOCAL lock_timeout = '0'"))
    # Event IDs are uuid4-derived, so they cannot recover chronology when old
    # rows share an occurred_at tick. workspace_events is append-only; ctid is
    # the best stored hint of heap insertion order for this one-time backfill.
    op.execute(
        sa.text(
            """
            WITH ordered_events AS (
                SELECT
                    id,
                    (row_number() OVER (
                        PARTITION BY workspace_id
                        ORDER BY occurred_at ASC, ctid ASC
                    ))::integer AS backfilled_event_order
                FROM workspace_events
            )
            UPDATE workspace_events
            SET event_order = ordered_events.backfilled_event_order
            FROM ordered_events
            WHERE workspace_events.id = ordered_events.id
              AND workspace_events.event_order IS NULL
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
            SET event_sequence = event_order_bounds.max_event_order
            FROM event_order_bounds
            WHERE workspaces.id = event_order_bounds.workspace_id
              AND (
                  workspaces.event_sequence IS NULL
                  OR workspaces.event_sequence < event_order_bounds.max_event_order
              )
            """
        )
    )
    op.execute(sa.text("UPDATE workspaces SET event_sequence = 0 WHERE event_sequence IS NULL"))
    op.execute(sa.text("ALTER TABLE workspaces ALTER COLUMN event_sequence SET DEFAULT 0"))
    op.execute(sa.text("ALTER TABLE workspaces ALTER COLUMN event_sequence SET NOT NULL"))
    # Concurrent index creation cannot run inside Alembic's migration
    # transaction. Keep the data backfill transactional above, then release the
    # transaction before building the read-path index without blocking writers.
    with op.get_context().autocommit_block():
        try:
            op.execute(sa.text("SET lock_timeout = '30s'"))
            op.execute(sa.text("SET statement_timeout = '10min'"))
            op.execute(
                sa.text(
                    "SELECT pg_advisory_lock("
                    f"{_INDEX_DDL_LOCK_NAMESPACE}, {_INDEX_DDL_LOCK_KEY}"
                    ")"
                )
            )
            bind = op.get_bind()
            invalid_index = bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM pg_class c
                    JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = :index_name
                      AND pg_catalog.pg_table_is_visible(c.oid)
                      AND NOT i.indisvalid
                    LIMIT 1
                    """
                ),
                {"index_name": "ix_workspace_events_workspace_occurred_order"},
            ).scalar_one_or_none()
            if invalid_index is not None:
                op.execute(
                    sa.text(
                        'DROP INDEX CONCURRENTLY "ix_workspace_events_workspace_occurred_order"'
                    )
                )
            op.create_index(
                "ix_workspace_events_workspace_occurred_order",
                "workspace_events",
                ["workspace_id", "occurred_at", "event_order"],
                unique=False,
                if_not_exists=True,
                postgresql_concurrently=True,
            )
        finally:
            op.execute(sa.text("RESET lock_timeout"))
            op.execute(sa.text("RESET statement_timeout"))
            op.execute(
                sa.text(
                    "SELECT pg_advisory_unlock("
                    f"{_INDEX_DDL_LOCK_NAMESPACE}, {_INDEX_DDL_LOCK_KEY}"
                    ")"
                )
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_workspace_events_workspace_occurred_order",
            table_name="workspace_events",
            if_exists=True,
            postgresql_concurrently=True,
        )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_workspace_events_assign_event_order "
            "ON workspace_events"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS awf_assign_workspace_event_order()"))
    op.execute(sa.text("ALTER TABLE workspace_events DROP COLUMN IF EXISTS event_order"))
    op.execute(sa.text("ALTER TABLE workspaces DROP COLUMN IF EXISTS event_sequence"))
