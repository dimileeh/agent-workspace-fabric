"""Durable admission-only maintenance on the existing hosted lifecycle gate.

Revision ID: db08e4f50617
Revises: ca97d3e4f506
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from awf.hosted_node.lifecycle_barrier import replace_barrier

revision: str = "db08e4f50617"
down_revision: str | Sequence[str] | None = "ca97d3e4f506"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosted_node_lifecycle", sa.Column("maintenance_generation", sa.BigInteger()))
    op.add_column(
        "hosted_node_lifecycle",
        sa.Column("maintenance_released_through", sa.BigInteger(), nullable=False, server_default="0"),
    )
    replace_barrier(op.get_bind())


def downgrade() -> None:
    # Erasing a live hold or its replay tombstone could silently reopen a node.
    op.execute("LOCK TABLE hosted_node_lifecycle IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM hosted_node_lifecycle) THEN
                RAISE EXCEPTION 'NODE_LIFECYCLE_STILL_ENABLED';
            END IF;
        END $$
    """)
    op.drop_column("hosted_node_lifecycle", "maintenance_released_through")
    op.drop_column("hosted_node_lifecycle", "maintenance_generation")
