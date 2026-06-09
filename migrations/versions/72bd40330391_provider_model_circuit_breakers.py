"""Add provider/model circuit breakers.

Revision ID: 72bd40330391
Revises: 8c9d0e1f2a3b
Create Date: 2026-05-01 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "72bd40330391"
down_revision: str | Sequence[str] | None = "8c9d0e1f2a3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_model_circuit_breakers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reason_code", sa.String(length=64), nullable=True),
        sa.Column("last_failure_fingerprint", sa.String(length=512), nullable=True),
        sa.Column("last_workspace_id", sa.String(length=36), nullable=True),
        sa.Column("last_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model", name="uq_provider_model_circuit_breakers_pair"),
    )
    op.create_index(
        "ix_provider_model_circuit_breakers_provider_model",
        "provider_model_circuit_breakers",
        ["provider", "model"],
        unique=False,
    )
    op.create_index(
        "ix_provider_model_circuit_breakers_state",
        "provider_model_circuit_breakers",
        ["state", "cooldown_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_model_circuit_breakers_state",
        table_name="provider_model_circuit_breakers",
    )
    op.drop_index(
        "ix_provider_model_circuit_breakers_provider_model",
        table_name="provider_model_circuit_breakers",
    )
    op.drop_table("provider_model_circuit_breakers")
