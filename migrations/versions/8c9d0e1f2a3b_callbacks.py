"""Add external callback subscriptions and delivery records.

Revision ID: 8c9d0e1f2a3b
Revises: 7b8c9d0e1f2a
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c9d0e1f2a3b"
down_revision: str | Sequence[str] | None = "7b8c9d0e1f2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "callback_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("event_types", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("initial_backoff_seconds", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_callback_subscriptions_idempotency_key",
        ),
    )
    op.create_index(
        "ix_callback_subscriptions_created_at",
        "callback_subscriptions",
        ["created_at"],
    )
    op.create_index(
        "ix_callback_subscriptions_enabled_created",
        "callback_subscriptions",
        ["enabled", "created_at"],
    )

    op.create_table(
        "callback_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=False),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=256), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("merge_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("envelope", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["merge_candidate_id"], ["merge_candidates.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["callback_subscriptions.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "dedupe_key",
            name="uq_callback_deliveries_dedupe",
        ),
    )
    op.create_index(
        "ix_callback_deliveries_due",
        "callback_deliveries",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_callback_deliveries_merge_candidate",
        "callback_deliveries",
        ["merge_candidate_id"],
    )
    op.create_index(
        "ix_callback_deliveries_operation",
        "callback_deliveries",
        ["operation_id"],
    )
    op.create_index(
        "ix_callback_deliveries_source",
        "callback_deliveries",
        ["event_kind", "source_id"],
    )
    op.create_index(
        "ix_callback_deliveries_subscription",
        "callback_deliveries",
        ["subscription_id"],
    )
    op.create_index(
        "ix_callback_deliveries_workspace",
        "callback_deliveries",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_callback_deliveries_workspace", table_name="callback_deliveries")
    op.drop_index("ix_callback_deliveries_subscription", table_name="callback_deliveries")
    op.drop_index("ix_callback_deliveries_source", table_name="callback_deliveries")
    op.drop_index("ix_callback_deliveries_operation", table_name="callback_deliveries")
    op.drop_index("ix_callback_deliveries_merge_candidate", table_name="callback_deliveries")
    op.drop_index("ix_callback_deliveries_due", table_name="callback_deliveries")
    op.drop_table("callback_deliveries")
    op.drop_index(
        "ix_callback_subscriptions_enabled_created",
        table_name="callback_subscriptions",
    )
    op.drop_index("ix_callback_subscriptions_created_at", table_name="callback_subscriptions")
    op.drop_table("callback_subscriptions")
