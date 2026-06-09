"""Merge stale reason and validation run migration heads.

Revision ID: f7a8b9c0d1e2
Revises: ab0b071760d1, e6f7a8b9c0d1
Create Date: 2026-04-26 17:40:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = (
    "ab0b071760d1",
    "e6f7a8b9c0d1",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
