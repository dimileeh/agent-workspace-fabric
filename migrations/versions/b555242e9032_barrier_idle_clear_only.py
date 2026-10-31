"""Guarded writes rewrite the lifecycle gate only to clear a set idle mark.

Databases upgraded before this revision keep whichever admission barrier body
their last ``replace_barrier`` call installed, including the previous ``tasks``
attention lookup. Re-installing the live body converges them.

Revision ID: b555242e9032
Revises: b830e6d9a142
"""

from alembic import op

from awf.hosted_node.lifecycle_barrier import replace_barrier

revision = "b555242e9032"
down_revision = "b830e6d9a142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    replace_barrier(op.get_bind())


def downgrade() -> None:
    # No schema object is added. The previous body is behaviorally identical: it
    # only also ran a no-op UPDATE and an unindexed equivalent tasks lookup.
    pass
