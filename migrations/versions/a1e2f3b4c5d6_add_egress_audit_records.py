"""Add egress_audit_records table.

Revision ID: a1e2f3b4c5d6
Revises: 6747c50621ff
Create Date: 2026-05-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1e2f3b4c5d6"
down_revision: str | Sequence[str] | None = "6747c50621ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "egress_audit_records",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=True),
        sa.Column("policy_posture", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("destination_category", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column(
            "details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "enforced_at",
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
    )
    op.create_index(
        "ix_egress_audit_records_workspace",
        "egress_audit_records",
        ["workspace_id"],
    )
    op.create_index(
        "ix_egress_audit_records_attempt",
        "egress_audit_records",
        ["attempt_id"],
    )
    op.create_index(
        "ix_egress_audit_records_enforced_at",
        "egress_audit_records",
        ["enforced_at"],
    )
    op.create_index(
        "ix_egress_audit_records_posture",
        "egress_audit_records",
        ["policy_posture"],
    )


def downgrade() -> None:
    op.drop_index("ix_egress_audit_records_posture", table_name="egress_audit_records")
    op.drop_index("ix_egress_audit_records_enforced_at", table_name="egress_audit_records")
    op.drop_index("ix_egress_audit_records_attempt", table_name="egress_audit_records")
    op.drop_index("ix_egress_audit_records_workspace", table_name="egress_audit_records")
    op.drop_table("egress_audit_records")
