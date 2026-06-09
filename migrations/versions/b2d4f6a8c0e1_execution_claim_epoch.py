"""Add execution_claim_epoch fencing column.

Revision ID: b2d4f6a8c0e1
Revises: 0f1e2d3c4b5a
Create Date: 2026-06-05 00:00:00.000000+00:00

A dedicated monotonic fencing token for the active-execution claim. ``version``
bumps on every status transition so it cannot fence a live worker; this column
only advances when the claim itself changes hands (claim or recovery clear).
``server_default text("0")`` backfills every existing row to ``0`` on both
SQLite and PostgreSQL, and the first lease action bumps an in-flight row to
``1`` — fencing any worker that captured the pre-bump value.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c0e1"
down_revision: str | Sequence[str] | None = "0f1e2d3c4b5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(
            sa.Column(
                "execution_claim_epoch",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("execution_claim_epoch")
