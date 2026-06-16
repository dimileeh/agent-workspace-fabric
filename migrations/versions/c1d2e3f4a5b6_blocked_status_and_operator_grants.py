"""Blocked workspace state columns and operator grant audit records.

Adds the durable block-state columns used by the pre-PR ``blocked`` operator
pause (protected quality-gate violation) and the ``operator_grant_audit_records``
table that records scoped, single-use, epoch-bound APPROVE-AND-KEEP grants.

Revision ID: c1d2e3f4a5b6
Revises: b582d1c4e7a9
Create Date: 2026-06-16 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b582d1c4e7a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("block_reason_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("block_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("block_violations", sa.JSON(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("block_resume_phase", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "block_epoch",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("pending_operator_hint", sa.JSON(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("block_baseline_coverage", sa.JSON(), nullable=True),
    )

    op.create_table(
        "operator_grant_audit_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("operator", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.String(length=2048), nullable=False),
        sa.Column("normalized_path", sa.String(length=1024), nullable=False),
        sa.Column("block_epoch", sa.Integer(), nullable=False),
        sa.Column(
            "approve_policy_downgrade",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operator_grant_audit_records_workspace",
        "operator_grant_audit_records",
        ["workspace_id"],
    )
    op.create_index(
        "ix_operator_grant_audit_records_created_at",
        "operator_grant_audit_records",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_grant_audit_records_created_at",
        table_name="operator_grant_audit_records",
    )
    op.drop_index(
        "ix_operator_grant_audit_records_workspace",
        table_name="operator_grant_audit_records",
    )
    op.drop_table("operator_grant_audit_records")

    op.drop_column("workspaces", "block_baseline_coverage")
    op.drop_column("workspaces", "pending_operator_hint")
    op.drop_column("workspaces", "blocked_at")
    op.drop_column("workspaces", "block_epoch")
    op.drop_column("workspaces", "block_resume_phase")
    op.drop_column("workspaces", "block_violations")
    op.drop_column("workspaces", "block_type")
    op.drop_column("workspaces", "block_reason_code")
