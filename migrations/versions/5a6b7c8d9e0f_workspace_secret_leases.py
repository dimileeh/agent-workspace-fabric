"""Add workspace secret lease metadata.

Revision ID: 5a6b7c8d9e0f
Revises: 4f2b9c8d7e6a
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5a6b7c8d9e0f"
down_revision: str | Sequence[str] | None = "4f2b9c8d7e6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_secret_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("secret_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("ref_digest", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mounted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "issue_metadata",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "mount_metadata",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("revoke_reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "secret_name",
            "kind",
            "target",
            name="uq_workspace_secret_leases_declaration",
        ),
    )
    op.create_index(
        "ix_workspace_secret_leases_workspace_status",
        "workspace_secret_leases",
        ["workspace_id", "status"],
    )
    op.create_index(
        "ix_workspace_secret_leases_status_expires",
        "workspace_secret_leases",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_workspace_secret_leases_workspace_issued",
        "workspace_secret_leases",
        ["workspace_id", "issued_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_secret_leases_workspace_issued",
        table_name="workspace_secret_leases",
    )
    op.drop_index(
        "ix_workspace_secret_leases_status_expires",
        table_name="workspace_secret_leases",
    )
    op.drop_index(
        "ix_workspace_secret_leases_workspace_status",
        table_name="workspace_secret_leases",
    )
    op.drop_table("workspace_secret_leases")
