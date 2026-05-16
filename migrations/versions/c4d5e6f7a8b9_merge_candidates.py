"""Add canonical attempt lineage and merge candidates.

Revision ID: c4d5e6f7a8b9
Revises: 2c3d4e5f6a1b
Create Date: 2026-04-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "2c3d4e5f6a1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_attempts") as batch:
        batch.add_column(
            sa.Column("parent_attempt_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(
            sa.Column("redispatch_from_attempt_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(
            sa.Column("superseded_by_attempt_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "is_canonical_for_merge",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.create_foreign_key(
            "fk_task_attempts_parent_attempt_id",
            "task_attempts",
            ["parent_attempt_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_task_attempts_redispatch_from_attempt_id",
            "task_attempts",
            ["redispatch_from_attempt_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_task_attempts_superseded_by_attempt_id",
            "task_attempts",
            ["superseded_by_attempt_id"],
            ["id"],
        )

    op.create_index(
        "uq_task_attempts_one_canonical_per_task",
        "task_attempts",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("is_canonical_for_merge = true"),
    )

    op.create_table(
        "merge_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("pr_url", sa.String(length=512), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("base_branch", sa.String(length=256), nullable=False),
        sa.Column("branch_name", sa.String(length=256), nullable=True),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column("base_sha", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "manual_merge_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "waiting_for_monitor",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "failed_or_cancelled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("not_canonical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_merge_candidates_attempt_id"),
    )
    op.create_index("ix_merge_candidates_task", "merge_candidates", ["task_id"])
    op.create_index("ix_merge_candidates_workspace", "merge_candidates", ["workspace_id"])
    op.create_index(
        "ix_merge_candidates_status_updated",
        "merge_candidates",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_merge_candidates_repo_base",
        "merge_candidates",
        ["repo_url", "base_branch"],
    )


def downgrade() -> None:
    op.drop_index("ix_merge_candidates_repo_base", table_name="merge_candidates")
    op.drop_index("ix_merge_candidates_status_updated", table_name="merge_candidates")
    op.drop_index("ix_merge_candidates_workspace", table_name="merge_candidates")
    op.drop_index("ix_merge_candidates_task", table_name="merge_candidates")
    op.drop_table("merge_candidates")

    op.drop_index("uq_task_attempts_one_canonical_per_task", table_name="task_attempts")
    with op.batch_alter_table("task_attempts") as batch:
        batch.drop_constraint(
            "fk_task_attempts_superseded_by_attempt_id",
            type_="foreignkey",
        )
        batch.drop_constraint(
            "fk_task_attempts_redispatch_from_attempt_id",
            type_="foreignkey",
        )
        batch.drop_constraint("fk_task_attempts_parent_attempt_id", type_="foreignkey")
        batch.drop_column("is_canonical_for_merge")
        batch.drop_column("superseded_by_attempt_id")
        batch.drop_column("redispatch_from_attempt_id")
        batch.drop_column("parent_attempt_id")
