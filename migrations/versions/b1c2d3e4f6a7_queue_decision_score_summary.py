"""Add scheduler score summaries to queue decisions.

Revision ID: b1c2d3e4f6a7
Revises: 72bd40330391
Create Date: 2026-05-02 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f6a7"
down_revision: str | Sequence[str] | None = "72bd40330391"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "queue_decisions",
        sa.Column(
            "score_summary",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("queue_decisions", "score_summary")
