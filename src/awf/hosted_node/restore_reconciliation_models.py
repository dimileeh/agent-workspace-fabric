"""Restore-specific batch journal and the four otherwise unowned row fences."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from awf.db.base import Base, _now


class RestoreReconciliationRow(Base):
    __tablename__ = "hosted_restore_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "owner", "restore_id", "idempotency_key", name="uq_restore_reconciliation_key"
        ),
        Index(
            "uq_restore_reconciliation_revision",
            "owner",
            "restore_id",
            "revision",
            unique=True,
            postgresql_where=text("state <> 'superseded'"),
            sqlite_where=text("state <> 'superseded'"),
        ),
        CheckConstraint(
            "CAST(state AS TEXT) IN ('staged','applying','committed','superseded')",
            name="ck_restore_reconciliation_state",
        ),
        CheckConstraint(
            "revision > 0 AND prior_revision = revision - 1",
            name="ck_restore_reconciliation_revision",
        ),
        CheckConstraint(
            "(state = 'applying') = (transaction_id IS NOT NULL)",
            name="ck_restore_reconciliation_transaction",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    restore_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    receipt_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    identity: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    binding: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    prior_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authority_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inventory_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    mutations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RestoreRowHold(Base):
    __tablename__ = "hosted_restore_row_holds"
    __table_args__ = (
        CheckConstraint(
            "CAST(family AS TEXT) IN ('tasks','callback_subscriptions','callback_deliveries','service_gc_requests')",
            name="ck_restore_hold_family",
        ),
        CheckConstraint("CAST(state AS TEXT) IN ('held','resolved')", name="ck_restore_hold_state"),
        CheckConstraint(
            "family = 'service_gc_requests' OR length(row_id) <= 36", name="ck_restore_hold_id"
        ),
        Index("ix_restore_hold_active", "owner", "family", "row_id", "state"),
    )

    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    restore_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    family: Mapped[str] = mapped_column(String(32), primary_key=True)
    row_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    preimage_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    journal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("hosted_restore_reconciliations.id"), nullable=False
    )
    resolved_journal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("hosted_restore_reconciliations.id")
    )
