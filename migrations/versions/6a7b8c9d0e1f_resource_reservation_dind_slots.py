"""Add DinD slot accounting to resource reservations.

Revision ID: 6a7b8c9d0e1f
Revises: 4f2b9c8d7e6a
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a7b8c9d0e1f"
down_revision: str | Sequence[str] | None = "4f2b9c8d7e6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("resource_reservations") as batch:
        batch.add_column(
            sa.Column(
                "dind_slots",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_reservations") as batch:
        batch.drop_column("dind_slots")
