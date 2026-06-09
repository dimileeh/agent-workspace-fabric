"""Add policy findings for out-of-scope change visibility.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-04-26 19:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(
            sa.Column(
                "task_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    with op.batch_alter_table("merge_candidates") as batch:
        batch.add_column(
            sa.Column(
                "policy_blocked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_table(
        "policy_findings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("subject_path", sa.String(length=512), nullable=True),
        sa.Column("explanation", sa.String(length=2048), nullable=False),
        sa.Column(
            "details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
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
    op.create_index("ix_policy_findings_workspace", "policy_findings", ["workspace_id"])
    op.create_index("ix_policy_findings_candidate", "policy_findings", ["candidate_id"])
    op.create_index("ix_policy_findings_status", "policy_findings", ["status"])
    op.create_index("ix_policy_findings_severity", "policy_findings", ["severity"])
    op.create_index(
        "ix_policy_findings_detected_at",
        "policy_findings",
        ["detected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_policy_findings_detected_at", table_name="policy_findings")
    op.drop_index("ix_policy_findings_severity", table_name="policy_findings")
    op.drop_index("ix_policy_findings_status", table_name="policy_findings")
    op.drop_index("ix_policy_findings_candidate", table_name="policy_findings")
    op.drop_index("ix_policy_findings_workspace", table_name="policy_findings")
    op.drop_table("policy_findings")

    with op.batch_alter_table("merge_candidates") as batch:
        batch.drop_column("policy_blocked")

    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("task_policy")
