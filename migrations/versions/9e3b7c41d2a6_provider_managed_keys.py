"""Add provider_managed_keys for exact per-workspace OpenRouter spend.

Each row is the non-secret identity of one AWF-minted OpenRouter key (name,
hash, seq, USD limit and last usage read). Key values are never stored.
Workspace spend is the sum of ``usage_usd + byok_usage_usd`` across its rows.

Revision ID: 9e3b7c41d2a6
Revises: 85ce3f9b2a61
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e3b7c41d2a6"
down_revision: str | Sequence[str] | None = "85ce3f9b2a61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USD = sa.Numeric(14, 6)


def upgrade() -> None:
    op.create_table(
        "provider_managed_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("key_name", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("limit_usd", _USD, nullable=False),
        sa.Column("usage_usd", _USD, nullable=False, server_default="0"),
        sa.Column("byok_usage_usd", _USD, nullable=False, server_default="0"),
        sa.Column("usage_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_usage_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "seq", name="uq_provider_managed_keys_workspace_seq"),
        sa.UniqueConstraint("key_name", name="uq_provider_managed_keys_key_name"),
        sa.UniqueConstraint("key_hash", name="uq_provider_managed_keys_key_hash"),
    )
    op.create_index(
        "ix_provider_managed_keys_workspace", "provider_managed_keys", ["workspace_id"]
    )
    op.create_index("ix_provider_managed_keys_status", "provider_managed_keys", ["status"])


def downgrade() -> None:
    # Dropping recorded spend (or forgetting live keys) is lossy; an operator must
    # finalize and archive managed keys before an administrative downgrade.
    if op.get_bind().execute(sa.text("SELECT 1 FROM provider_managed_keys LIMIT 1")).first():
        raise RuntimeError(
            "PROVIDER_MANAGED_KEYS_DOWNGRADE_UNSAFE: retain compatible code and schema"
        )
    op.drop_index("ix_provider_managed_keys_status", table_name="provider_managed_keys")
    op.drop_index("ix_provider_managed_keys_workspace", table_name="provider_managed_keys")
    op.drop_table("provider_managed_keys")
