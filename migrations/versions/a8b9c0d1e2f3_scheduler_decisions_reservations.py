"""Add scheduler decision and resource reservation records.

Revision ID: a8b9c0d1e2f3
Revises: 8b9c0d1e2f3a
Create Date: 2026-04-26 18:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "8b9c0d1e2f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "queue_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("class_priority", sa.Integer(), nullable=False),
        sa.Column("computed_priority", sa.Integer(), nullable=False),
        sa.Column("age_boost", sa.Integer(), nullable=False),
        sa.Column("retry_bonus", sa.Integer(), nullable=False),
        sa.Column(
            "resource_summary",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "overlap_risk_summary",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_queue_decisions_workspace", "queue_decisions", ["workspace_id"])
    op.create_index("ix_queue_decisions_attempt", "queue_decisions", ["attempt_id"])
    op.create_index("ix_queue_decisions_task", "queue_decisions", ["task_id"])
    op.create_index("ix_queue_decisions_decision", "queue_decisions", ["decision"])
    op.create_index("ix_queue_decisions_decided_at", "queue_decisions", ["decided_at"])

    op.create_table(
        "resource_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("steady_cpu", sa.Float(), nullable=False),
        sa.Column("steady_memory_gb", sa.Float(), nullable=False),
        sa.Column("peak_cpu", sa.Float(), nullable=False),
        sa.Column("peak_memory_gb", sa.Float(), nullable=False),
        sa.Column("disk_mb", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_reservations_workspace",
        "resource_reservations",
        ["workspace_id"],
    )
    op.create_index(
        "ix_resource_reservations_attempt",
        "resource_reservations",
        ["attempt_id"],
    )
    op.create_index(
        "ix_resource_reservations_node_active",
        "resource_reservations",
        ["node_id", "released_at"],
    )
    op.create_index(
        "ix_resource_reservations_reserved_at",
        "resource_reservations",
        ["reserved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_reservations_reserved_at", table_name="resource_reservations")
    op.drop_index("ix_resource_reservations_node_active", table_name="resource_reservations")
    op.drop_index("ix_resource_reservations_attempt", table_name="resource_reservations")
    op.drop_index("ix_resource_reservations_workspace", table_name="resource_reservations")
    op.drop_table("resource_reservations")

    op.drop_index("ix_queue_decisions_decided_at", table_name="queue_decisions")
    op.drop_index("ix_queue_decisions_decision", table_name="queue_decisions")
    op.drop_index("ix_queue_decisions_task", table_name="queue_decisions")
    op.drop_index("ix_queue_decisions_attempt", table_name="queue_decisions")
    op.drop_index("ix_queue_decisions_workspace", table_name="queue_decisions")
    op.drop_table("queue_decisions")
