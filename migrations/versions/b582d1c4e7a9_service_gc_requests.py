"""Add service_gc_requests for on-demand worker GC delegation (#582).

``awf service gc --execute`` resolves in the capability-less API container and so
cannot reclaim the per-workspace Claude auth overlays or ``_shared/claude-base``.
This table is the API→worker channel: the API inserts a ``pending`` row, the
worker (holding ``CAP_SYS_ADMIN``) claims it via ``SELECT ... FOR UPDATE SKIP
LOCKED``, runs the real reap, and writes the reclaimed report back into
``result`` so the API can fold actual reclamation into the gc response. Mirrors
the system-scoped ``worker_heartbeats`` table (no ``workspace_id``).

Revision ID: b582d1c4e7a9
Revises: c3e5f7b9d1a2
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b582d1c4e7a9"
down_revision: str | Sequence[str] | None = "c3e5f7b9d1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_gc_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=2048), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_gc_requests_status",
        "service_gc_requests",
        ["status"],
    )
    op.create_index(
        "ix_service_gc_requests_requested_at",
        "service_gc_requests",
        ["requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_service_gc_requests_requested_at", table_name="service_gc_requests")
    op.drop_index("ix_service_gc_requests_status", table_name="service_gc_requests")
    op.drop_table("service_gc_requests")
