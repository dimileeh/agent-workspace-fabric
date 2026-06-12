"""Add task_tag to workspaces.

Revision ID: c3e5f7b9d1a2
Revises: b2d4f6a8c0e1
Create Date: 2026-06-11 00:00:00.000000+00:00

Adds ``workspaces.task_tag`` — an optional Jira-style issue key (e.g.
``PROJ-123``) that AWF prepends to the branch name, PR title, and every
AWF-authored commit message so work auto-links to the issue in
Jira/BitBucket. Nullable to accommodate pre-migration rows and the
common case where no tag is supplied (absent = no-op).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5f7b9d1a2"
down_revision: str | Sequence[str] | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("task_tag", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("task_tag")
