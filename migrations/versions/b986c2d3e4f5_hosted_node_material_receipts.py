"""Independent non-secret hosted owner authority and material tombstones.

Revision ID: b986c2d3e4f5
Revises: a875b1c2d3e4
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b986c2d3e4f5"
down_revision: str | Sequence[str] | None = "a875b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hosted_owners",
        sa.Column("owner", sa.String(128), primary_key=True),
        sa.Column("incarnation", sa.String(128), nullable=False),
        sa.Column("recovery_generation", sa.BigInteger(), nullable=False),
        sa.Column("boot_epoch", sa.BigInteger(), nullable=False),
        sa.Column("acknowledgement", sa.String(128)),
        sa.CheckConstraint("recovery_generation > 0 AND boot_epoch > 0"),
    )
    op.create_table(
        "hosted_preparations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("preparation_id", sa.String(128), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner", "namespace", "workspace_id", "preparation_id"),
    )
    op.create_table(
        "hosted_grants",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("preparation", sa.String(32), nullable=False),
        sa.Column("grant_id", sa.String(128), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("current", sa.String(32)),
        sa.Column("pending", sa.String(32)),
        sa.UniqueConstraint("preparation", "grant_id"),
        sa.CheckConstraint("fence > 0 AND revision > 0 AND authorization_generation > 0"),
    )
    op.create_table(
        "hosted_receipts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("grant", sa.String(32), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("predecessor", sa.String(32)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.UniqueConstraint("owner", "idempotency_key"),
    )
    op.create_index("ix_hosted_receipts_grant", "hosted_receipts", ["grant"])


def downgrade() -> None:
    for table in ("hosted_receipts", "hosted_grants", "hosted_preparations", "hosted_owners"):
        op.drop_table(table)
