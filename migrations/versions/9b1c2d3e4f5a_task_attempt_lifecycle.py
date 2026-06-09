"""Add first-class tasks and task attempts.

Revision ID: 9b1c2d3e4f5a
Revises: a7b8c9d0e1f2
Create Date: 2026-04-26 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b1c2d3e4f5a"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("base_branch", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("prompt", sa.String(length=16384), nullable=False),
        sa.Column("task_class", sa.String(length=32), nullable=True),
        sa.Column(
            "owned_paths",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_tasks_external_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"], unique=False)
    op.create_index("ix_tasks_repo_base", "tasks", ["repo_url", "base_branch"], unique=False)

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("base_branch", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("task_class", sa.String(length=32), nullable=True),
        sa.Column(
            "owned_paths",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_task_attempts_workspace_id"),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_number",
        ),
    )
    op.create_index("ix_task_attempts_created_at", "task_attempts", ["created_at"])
    op.create_index("ix_task_attempts_status", "task_attempts", ["status"])
    op.create_index("ix_task_attempts_task", "task_attempts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_attempts_task", table_name="task_attempts")
    op.drop_index("ix_task_attempts_status", table_name="task_attempts")
    op.drop_index("ix_task_attempts_created_at", table_name="task_attempts")
    op.drop_table("task_attempts")

    op.drop_index("ix_tasks_repo_base", table_name="tasks")
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_table("tasks")
