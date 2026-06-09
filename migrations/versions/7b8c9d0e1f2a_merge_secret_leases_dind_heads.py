"""Merge secret lease and DinD reservation heads.

Revision ID: 7b8c9d0e1f2a
Revises: 5a6b7c8d9e0f, 6a7b8c9d0e1f
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "7b8c9d0e1f2a"
down_revision: str | Sequence[str] | None = (
    "5a6b7c8d9e0f",
    "6a7b8c9d0e1f",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
