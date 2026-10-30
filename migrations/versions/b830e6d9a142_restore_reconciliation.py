"""Add exact restore decisions and non-workspace row containment.

Revision ID: b830e6d9a142
Revises: fd2af6172839
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from awf.hosted_node.lifecycle_barrier import replace_barrier

revision = "b830e6d9a142"
down_revision = "fd2af6172839"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosted_restore_reconciliations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("restore_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("receipt_ref", sa.String(128), nullable=False),
        sa.Column("identity", postgresql.JSONB(), nullable=False),
        sa.Column("binding", postgresql.JSONB(), nullable=False),
        sa.Column("prior_revision", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("authority_revision", sa.BigInteger(), nullable=False),
        sa.Column("inventory_sha256", sa.String(64), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("inventory", postgresql.JSONB(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("transaction_id", sa.BigInteger()),
        sa.Column("mutations", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner", "restore_id", "idempotency_key", name="uq_restore_reconciliation_key"),
        sa.CheckConstraint("CAST(state AS TEXT) IN ('staged','applying','committed','superseded')", name="ck_restore_reconciliation_state"),
        sa.CheckConstraint("revision > 0 AND prior_revision = revision - 1", name="ck_restore_reconciliation_revision"),
        sa.CheckConstraint("(state = 'applying') = (transaction_id IS NOT NULL)", name="ck_restore_reconciliation_transaction"),
    )
    op.create_index("uq_restore_reconciliation_revision", "hosted_restore_reconciliations", ["owner", "restore_id", "revision"], unique=True, postgresql_where=sa.text("state <> 'superseded'"))
    op.create_table(
        "hosted_restore_row_holds",
        sa.Column("owner", sa.String(128), primary_key=True),
        sa.Column("restore_id", sa.String(36), primary_key=True),
        sa.Column("family", sa.String(32), primary_key=True),
        sa.Column("row_id", sa.String(64), primary_key=True),
        sa.Column("preimage_sha256", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("journal_id", sa.String(36), sa.ForeignKey("hosted_restore_reconciliations.id"), nullable=False),
        sa.Column("resolved_journal_id", sa.String(36), sa.ForeignKey("hosted_restore_reconciliations.id")),
        sa.CheckConstraint("CAST(family AS TEXT) IN ('tasks','callback_subscriptions','callback_deliveries','service_gc_requests')", name="ck_restore_hold_family"),
        sa.CheckConstraint("CAST(state AS TEXT) IN ('held','resolved')", name="ck_restore_hold_state"),
        sa.CheckConstraint("family = 'service_gc_requests' OR length(row_id) <= 36", name="ck_restore_hold_id"),
    )
    op.create_index("ix_restore_hold_active", "hosted_restore_row_holds", ["owner", "family", "row_id", "state"])
    op.add_column("hosted_node_lifecycle", sa.Column("restore_reconciliation_id", sa.String(36)))
    op.create_foreign_key("fk_node_restore_reconciliation", "hosted_node_lifecycle", "hosted_restore_reconciliations", ["restore_reconciliation_id"], ["id"])
    replace_barrier(op.get_bind())


def downgrade() -> None:
    op.execute("LOCK TABLE hosted_node_lifecycle, hosted_restore_reconciliations, hosted_restore_row_holds IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM hosted_node_lifecycle) THEN
            RAISE EXCEPTION 'NODE_LIFECYCLE_STILL_ENABLED';
        END IF;
        IF EXISTS(SELECT 1 FROM hosted_restore_reconciliations) OR EXISTS(SELECT 1 FROM hosted_restore_row_holds) THEN
            RAISE EXCEPTION 'RESTORE_JOURNAL_REQUIRES_PRESERVATION';
        END IF;
    END $$""")
    op.execute("DROP TRIGGER IF EXISTS awf_restore_metadata ON hosted_node_lifecycle")
    op.drop_constraint("fk_node_restore_reconciliation", "hosted_node_lifecycle", type_="foreignkey")
    op.drop_column("hosted_node_lifecycle", "restore_reconciliation_id")
    op.drop_table("hosted_restore_row_holds")
    op.drop_table("hosted_restore_reconciliations")
    replace_barrier(op.get_bind())
