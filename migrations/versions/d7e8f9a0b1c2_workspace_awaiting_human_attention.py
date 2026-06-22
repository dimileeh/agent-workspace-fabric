"""Workspace awaiting-human attention flag columns.

Adds the two nullable columns that surface a PR-monitor ``NotifyHuman``
(HUMAN_WAIT) escalation as a first-class, operator-visible attention signal on
a ``monitoring_pr`` workspace:

- ``awaiting_human_since`` — wall-clock start of the current HUMAN_WAIT episode.
- ``awaiting_human_reason`` — latest human-readable escalation reason.

Both are plain nullable columns with no server default, so the metadata-only
``add_column`` / ``drop_column`` ops are portable on SQLite and Postgres (no
batch_alter_table, no backfill, no lock guards needed).

Revision ID: d7e8f9a0b1c2
Revises: c1d2e3f4a5b6
Create Date: 2026-06-22 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("awaiting_human_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("awaiting_human_reason", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "awaiting_human_reason")
    op.drop_column("workspaces", "awaiting_human_since")
