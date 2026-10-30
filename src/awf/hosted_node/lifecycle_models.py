"""One opt-in gate per dedicated node database, not a workspace state machine."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from awf.db.base import Base


class NodeLifecycleRow(Base):
    __tablename__ = "hosted_node_lifecycle"
    __table_args__ = (CheckConstraint("singleton = 1", name="ck_node_lifecycle_singleton"),)

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    previous_identity: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    disk_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    database_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    recovery_key: Mapped[str | None] = mapped_column(String(128))
    receipt_ref: Mapped[str | None] = mapped_column(String(128))
    recovery_acceptance: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    drain_generation: Mapped[int | None] = mapped_column(BigInteger)
    aborted_through: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    maintenance_generation: Mapped[int | None] = mapped_column(BigInteger)
    maintenance_released_through: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    idle_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_marker: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    restore_quarantine: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    restore_reconciliation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("hosted_restore_reconciliations.id"), nullable=True
    )
