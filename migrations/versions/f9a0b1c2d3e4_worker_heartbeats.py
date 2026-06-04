"""Add worker heartbeat liveness records.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a0b1c2d3e4"
down_revision: str | Sequence[str] | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("poll_interval_seconds", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_worker_heartbeats_node_id",
        "worker_heartbeats",
        ["node_id"],
    )
    op.create_index(
        "ix_worker_heartbeats_last_heartbeat_at",
        "worker_heartbeats",
        ["last_heartbeat_at"],
    )
    op.create_index(
        "ix_worker_heartbeats_node_last_heartbeat",
        "worker_heartbeats",
        ["node_id", "last_heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_heartbeats_node_last_heartbeat",
        table_name="worker_heartbeats",
    )
    op.drop_index(
        "ix_worker_heartbeats_last_heartbeat_at",
        table_name="worker_heartbeats",
    )
    op.drop_index("ix_worker_heartbeats_node_id", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
