"""Flip the workspaces.auto_merge database default to opt-out.

``auto_merge`` is one uniform, opt-in setting whose single source of truth is
``awf.common.auto_merge.DEFAULT_AUTO_MERGE`` (``False``). The column was first
added (revision ``f6a1b2c3d4e5``) with ``server_default=true()`` back when
auto-merge was the implicit behavior; that stale ``true`` default now
contradicts the opt-in contract. Any ORM construction or raw ``INSERT`` that
omits the column would otherwise create a newly auto-merging workspace, and
because such a row also lacks a persisted ``auto_merge_intent`` the provisioner
grandfathers that ``True`` permanently.

This migration changes only the column ``DEFAULT`` clause for future inserts.
It intentionally leaves existing row values untouched: rows that predate the
column (grandfathered to ``True`` by ``f6a1b2c3d4e5``'s backfill) keep their
value; the provisioner's legacy-preservation path continues to honor them.

Revision ID: e9f0a1b2c3d4
Revises: d7e8f9a0b1c2
Create Date: 2026-07-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | Sequence[str] | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "workspaces",
        "auto_merge",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.false(),
        existing_server_default=sa.true(),
    )


def downgrade() -> None:
    op.alter_column(
        "workspaces",
        "auto_merge",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
        existing_server_default=sa.false(),
    )
