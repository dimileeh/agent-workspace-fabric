"""Guarded writes rewrite the lifecycle gate only to clear a set idle mark.

Revision ID: fa20b3c4d5e6
Revises: ec19f5061728
"""

from alembic import op

from awf.hosted_node.lifecycle_barrier import replace_barrier

revision = "fa20b3c4d5e6"
down_revision = "ec19f5061728"
branch_labels = None
depends_on = None


def upgrade() -> None:
    replace_barrier(op.get_bind())


def downgrade() -> None:
    # The replaced body is behaviorally identical; it only skipped a no-op
    # UPDATE. Earlier revisions likewise re-install the live barrier body.
    pass
