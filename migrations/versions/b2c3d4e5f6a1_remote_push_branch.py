"""Add remote_push_branch to workspaces.

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-04-23 22:50:00.000000+00:00

Adds ``workspaces.remote_push_branch`` — the canonical push target for
the monitor's ``git push``. Nullable to accommodate pre-migration rows;
callers default to ``branch_name`` when unset.

Motivation: on 2026-04-23 four AWF feature-branch commits landed on
aira-web ``development`` because the monitor's ``git push origin HEAD``
inherited ``push.default=upstream`` + ``branch.<X>.merge=refs/heads/development``
from config that leaked across worktrees via the shared bare mirror.
The monitor now uses an explicit ``HEAD:refs/heads/<remote_push_branch>``
refspec so push semantics don't depend on git config at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a1"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("remote_push_branch", sa.String(length=256), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("remote_push_branch")
