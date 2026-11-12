"""Add work_slot_leases for broker-owned work slots (slot-release).

A work slot means provisioning, an agent run or a validation run is in flight
right now. Waiting PR monitors and blocked/recovering/paused workspaces no longer
occupy one: the resource broker takes a lease row when work starts and deletes it
when the workspace first waits. ``acquired_at`` stays NULL while an invocation
waits on its persisted FIFO ``ticket_at``; the partial unique index keeps at most
one held/adopted lease per workspace so a different invocation never shares it.

Revision ID: c6d7e8f9a0b1
Revises: b555242e9032
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "b555242e9032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_slot_leases",
        sa.Column("invocation_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("holder_worker_id", sa.String(length=128), nullable=False),
        sa.Column("holder_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ticket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column(
            "cleanup_unproven",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("invocation_id"),
    )
    op.create_index(
        "ix_work_slot_leases_workspace",
        "work_slot_leases",
        ["workspace_id"],
    )
    op.create_index(
        "ix_work_slot_leases_phase_ticket",
        "work_slot_leases",
        ["phase", "ticket_at", "invocation_id"],
    )
    op.create_index(
        "uq_work_slot_leases_workspace_held",
        "work_slot_leases",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("acquired_at IS NOT NULL"),
        sqlite_where=sa.text("acquired_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_work_slot_leases_workspace_held", table_name="work_slot_leases")
    op.drop_index("ix_work_slot_leases_phase_ticket", table_name="work_slot_leases")
    op.drop_index("ix_work_slot_leases_workspace", table_name="work_slot_leases")
    op.drop_table("work_slot_leases")
