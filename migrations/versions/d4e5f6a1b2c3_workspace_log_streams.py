"""workspace log streams

Revision ID: d4e5f6a1b2c3
Revises: c3d4e5f6a1b2
Create Date: 2026-04-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a1b2c3"
down_revision: str | Sequence[str] | None = "c3d4e5f6a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_log_streams",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("stream_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "stream_id", name="uq_workspace_log_stream"),
    )
    op.create_index(
        "ix_workspace_log_streams_workspace",
        "workspace_log_streams",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_log_streams_opened_at",
        "workspace_log_streams",
        ["opened_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_log_streams_opened_at", table_name="workspace_log_streams")
    op.drop_index("ix_workspace_log_streams_workspace", table_name="workspace_log_streams")
    op.drop_table("workspace_log_streams")
