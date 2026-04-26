"""Add stale_reasons table for the stale detection engine.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stale_reasons",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_ref", sa.String(length=512), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("explanation", sa.String(length=2048), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["merge_candidates.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stale_reasons_workspace", "stale_reasons", ["workspace_id"])
    op.create_index("ix_stale_reasons_candidate", "stale_reasons", ["candidate_id"])
    op.create_index("ix_stale_reasons_status", "stale_reasons", ["status"])
    op.create_index("ix_stale_reasons_detected_at", "stale_reasons", ["detected_at"])


def downgrade() -> None:
    op.drop_index("ix_stale_reasons_detected_at", table_name="stale_reasons")
    op.drop_index("ix_stale_reasons_status", table_name="stale_reasons")
    op.drop_index("ix_stale_reasons_candidate", table_name="stale_reasons")
    op.drop_index("ix_stale_reasons_workspace", table_name="stale_reasons")
    op.drop_table("stale_reasons")
