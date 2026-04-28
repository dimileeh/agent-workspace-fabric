"""Add validation run identity provenance.

Revision ID: 4f2b9c8d7e6a
Revises: ec041f62c7fc
Create Date: 2026-04-28 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f2b9c8d7e6a"
down_revision: str | Sequence[str] | None = "ec041f62c7fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("validation_runs") as batch:
        batch.add_column(sa.Column("base_sha", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("workspace_head_sha", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("profile_name", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("profile_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("profile_source", sa.String(length=256), nullable=True))
        batch.add_column(
            sa.Column("resolved_profile_digest", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("environment_identity_digest", sa.String(length=64), nullable=True)
        )
        batch.add_column(sa.Column("environment_identity_inputs", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("validation_runs") as batch:
        batch.drop_column("environment_identity_inputs")
        batch.drop_column("environment_identity_digest")
        batch.drop_column("resolved_profile_digest")
        batch.drop_column("profile_source")
        batch.drop_column("profile_version")
        batch.drop_column("profile_name")
        batch.drop_column("workspace_head_sha")
        batch.drop_column("base_sha")
