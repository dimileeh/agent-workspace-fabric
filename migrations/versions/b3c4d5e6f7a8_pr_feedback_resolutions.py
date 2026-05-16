"""Add PR-scoped feedback resolution state.

Revision ID: b3c4d5e6f7a8
Revises: a1e2f3b4c5d6
Create Date: 2026-05-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "a1e2f3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pr_feedback_resolutions",
        sa.Column("scm_provider", sa.String(length=32), nullable=False),
        sa.Column("repository_key", sa.String(length=512), nullable=False),
        sa.Column("pull_request_key", sa.String(length=128), nullable=False),
        sa.Column("head_sha", sa.String(length=128), nullable=False),
        sa.Column("feedback_kind", sa.String(length=32), nullable=False),
        sa.Column("feedback_id", sa.String(length=256), nullable=False),
        sa.Column("feedback_body_hash", sa.String(length=64), nullable=False),
        sa.Column("pull_request_url", sa.String(length=2048), nullable=True),
        sa.Column("feedback_url", sa.String(length=2048), nullable=True),
        sa.Column("feedback_author", sa.String(length=256), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=2048), nullable=True),
        sa.Column("source_workspace_id", sa.String(length=36), nullable=True),
        sa.Column("source_operation_id", sa.String(length=36), nullable=True),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
        sa.PrimaryKeyConstraint(
            "scm_provider",
            "repository_key",
            "pull_request_key",
            "feedback_kind",
            "feedback_id",
            "feedback_body_hash",
            name="pk_pr_feedback_resolutions",
        ),
    )
    op.create_index(
        "ix_pr_feedback_resolutions_pr",
        "pr_feedback_resolutions",
        ["scm_provider", "repository_key", "pull_request_key"],
    )
    op.create_index(
        "ix_pr_feedback_resolutions_head_sha",
        "pr_feedback_resolutions",
        ["head_sha"],
    )
    op.create_index(
        "ix_pr_feedback_resolutions_source_workspace",
        "pr_feedback_resolutions",
        ["source_workspace_id"],
    )
    op.create_index(
        "ix_pr_feedback_resolutions_updated_at",
        "pr_feedback_resolutions",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pr_feedback_resolutions_updated_at", table_name="pr_feedback_resolutions")
    op.drop_index("ix_pr_feedback_resolutions_head_sha", table_name="pr_feedback_resolutions")
    op.drop_index(
        "ix_pr_feedback_resolutions_source_workspace",
        table_name="pr_feedback_resolutions",
    )
    op.drop_index("ix_pr_feedback_resolutions_pr", table_name="pr_feedback_resolutions")
    op.drop_table("pr_feedback_resolutions")
