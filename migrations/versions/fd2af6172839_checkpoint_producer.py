"""Add node checkpoint intent without changing ordinary lifecycle behavior.

Revision ID: fd2af6172839
Revises: ec19f5061728
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from awf.hosted_node.lifecycle_barrier import replace_barrier

revision = "fd2af6172839"
down_revision = "ec19f5061728"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_node_lifecycle", sa.Column("checkpoint_marker", postgresql.JSONB()))
    op.add_column("hosted_node_lifecycle", sa.Column("restore_quarantine", postgresql.JSONB()))
    replace_barrier(op.get_bind())


def downgrade() -> None:
    op.execute("LOCK TABLE hosted_node_lifecycle IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM hosted_node_lifecycle) THEN
            RAISE EXCEPTION 'NODE_LIFECYCLE_STILL_ENABLED';
        END IF;
    END $$""")
    op.drop_column("hosted_node_lifecycle", "checkpoint_marker")
    op.drop_column("hosted_node_lifecycle", "restore_quarantine")
