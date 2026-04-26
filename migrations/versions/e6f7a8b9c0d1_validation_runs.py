"""Add durable validation run provenance.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("command_set_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "commands",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("base_commit", sa.String(length=64), nullable=True),
        sa.Column("target_branch", sa.String(length=256), nullable=True),
        sa.Column("target_head_sha", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "log_stream_refs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_validation_runs_workspace_started",
        "validation_runs",
        ["workspace_id", "started_at"],
    )
    op.create_index(
        "ix_validation_runs_workspace_finished",
        "validation_runs",
        ["workspace_id", "finished_at"],
    )
    op.create_index("ix_validation_runs_attempt", "validation_runs", ["attempt_id"])
    op.create_index("ix_validation_runs_status", "validation_runs", ["status"])
    op.create_index("ix_validation_runs_tier", "validation_runs", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_validation_runs_tier", table_name="validation_runs")
    op.drop_index("ix_validation_runs_status", table_name="validation_runs")
    op.drop_index("ix_validation_runs_attempt", table_name="validation_runs")
    op.drop_index("ix_validation_runs_workspace_finished", table_name="validation_runs")
    op.drop_index("ix_validation_runs_workspace_started", table_name="validation_runs")
    op.drop_table("validation_runs")
