"""Scope provider cooldowns by connection; advertise worker contract support.

Revision ID: 85ce3f9b2a61
Revises: c6d7e8f9a0b1
"""

import sqlalchemy as sa
from alembic import op

revision = "85ce3f9b2a61"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None
destructive = True


def upgrade() -> None:
    op.add_column(
        "provider_model_circuit_breakers",
        sa.Column("connection_scope", sa.String(64), nullable=False, server_default="legacy"),
    )
    op.drop_constraint(
        "uq_provider_model_circuit_breakers_pair", "provider_model_circuit_breakers", type_="unique"
    )
    op.create_unique_constraint(
        "uq_provider_model_circuit_breakers_connection",
        "provider_model_circuit_breakers",
        ["provider", "model", "connection_scope"],
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("execution_contract_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # Operational rollback retains this schema. An explicit administrative
    # downgrade must never collapse independently scoped cooldown history.
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM provider_model_circuit_breakers WHERE connection_scope <> 'legacy' LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError(
            "PROVIDER_CONNECTION_DOWNGRADE_UNSAFE: retain compatible code and schema"
        )
    op.drop_column("worker_heartbeats", "execution_contract_version")
    op.drop_constraint(
        "uq_provider_model_circuit_breakers_connection",
        "provider_model_circuit_breakers",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_provider_model_circuit_breakers_pair",
        "provider_model_circuit_breakers",
        ["provider", "model"],
    )
    op.drop_column("provider_model_circuit_breakers", "connection_scope")
