"""Add PR monitor columns + task_kind to workspaces.

Revision ID: a1b2c3d4e5f6
Revises: 135102b9a037
Create Date: 2026-04-22 15:30:00.000000+00:00

Adds the columns the PR monitor phase needs on ``workspaces``:

* ``pr_number`` — parsed out of ``pr_url`` when the PR is opened so the
  monitor can issue GraphQL queries without re-parsing the URL each poll.
* ``pr_merge_sha`` — populated on successful squash-merge.
* ``task_kind`` — distinguishes feature-branch-PR work (default) from
  release-PR monitoring (no initial clone + no auto-merge).
* ``monitor_iter_count`` / ``monitor_threads_addressed`` /
  ``monitor_last_commit_sha`` / ``monitor_started_at`` — persisted
  ``MonitorState`` so the loop is crash-safe.

No backfill required for the counter/state columns (defaults suffice).
Existing rows get ``task_kind = 'feature_branch_pr'`` via the column
default — every workspace pre-migration was exactly that.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "135102b9a037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("pr_number", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pr_merge_sha", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "task_kind",
                sa.String(length=32),
                nullable=False,
                server_default="feature_branch_pr",
            )
        )
        batch.add_column(
            sa.Column(
                "monitor_iter_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "monitor_threads_addressed",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch.add_column(
            sa.Column(
                "monitor_last_commit_sha", sa.String(length=64), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "monitor_started_at", sa.DateTime(timezone=True), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("monitor_started_at")
        batch.drop_column("monitor_last_commit_sha")
        batch.drop_column("monitor_threads_addressed")
        batch.drop_column("monitor_iter_count")
        batch.drop_column("task_kind")
        batch.drop_column("pr_merge_sha")
        batch.drop_column("pr_number")
