"""Committed retained-work acceptance on the existing lifecycle row.

Revision ID: ec19f5061728
Revises: db08e4f50617
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from awf.hosted_node.lifecycle_barrier import replace_barrier

revision = "ec19f5061728"
down_revision = "db08e4f50617"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_node_lifecycle", sa.Column("recovery_acceptance", postgresql.JSONB()))
    replace_barrier(op.get_bind())


def downgrade() -> None:
    op.execute("LOCK TABLE hosted_node_lifecycle IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM hosted_node_lifecycle) THEN
            RAISE EXCEPTION 'NODE_LIFECYCLE_STILL_ENABLED';
        END IF;
    END $$""")
    op.drop_column("hosted_node_lifecycle", "recovery_acceptance")
