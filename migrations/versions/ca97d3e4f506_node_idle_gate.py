"""Opt-in durable node admission and idle-drain barrier.

Revision ID: ca97d3e4f506
Revises: b986c2d3e4f5
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES, install_barrier

revision: str = "ca97d3e4f506"
down_revision: str | Sequence[str] | None = "b986c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hosted_node_lifecycle",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("identity", JSONB(), nullable=False),
        sa.Column("previous_identity", JSONB()),
        sa.Column("disk_ref", sa.String(128), nullable=False),
        sa.Column("database_ref", sa.String(128), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("recovery_key", sa.String(128)),
        sa.Column("receipt_ref", sa.String(128)),
        sa.Column("drain_generation", sa.BigInteger()),
        sa.Column("aborted_through", sa.BigInteger(), nullable=False),
        sa.Column("idle_since", sa.DateTime(timezone=True)),
        sa.CheckConstraint("singleton = 1", name="ck_node_lifecycle_singleton"),
    )
    install_barrier(op.get_bind())


def downgrade() -> None:
    # Rollback must not silently reopen an opted-in node. Removal requires an
    # explicit offline authority retirement, outside this migration.
    op.execute("LOCK TABLE hosted_node_lifecycle IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM hosted_node_lifecycle) THEN
                RAISE EXCEPTION 'NODE_LIFECYCLE_STILL_ENABLED';
            END IF;
        END $$
    """)
    for table in GUARDED_TABLES:
        op.execute(f"DROP TRIGGER awf_node_admission ON {table}")
    op.execute("DROP FUNCTION awf_node_admission_guard()")
    op.drop_table("hosted_node_lifecycle")
