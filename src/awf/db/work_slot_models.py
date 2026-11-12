"""Broker-owned work-slot lease model (slot-release).

Kept out of ``awf.db.models`` (which re-exports it) so that module stays within
the first-party file-line guardrail.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, false, text
from sqlalchemy.orm import Mapped, mapped_column

from awf.db.base import Base


class WorkSlotLease(Base):
    """Broker-owned work-slot lease for one invocation (slot-release).

    A work slot means provisioning, an agent run or a validation run is in flight
    right now; waiting PR monitors and paused workspaces hold none. The resource
    broker (``control/worker/resource_broker.py``) takes, renews and deletes these
    rows under the local-capacity advisory lock, flipping the workspace reservation
    phase steady/peak in the same transaction. ``holder_worker_id`` +
    ``holder_epoch`` fence renewal and release exactly like the execution claim.
    ``acquired_at`` is NULL while the invocation waits on its FIFO ``ticket_at``;
    the partial unique index keeps at most one held/adopted lease per workspace.
    """

    __tablename__ = "work_slot_leases"
    __table_args__ = (
        Index("ix_work_slot_leases_workspace", "workspace_id"),
        Index("ix_work_slot_leases_phase_ticket", "phase", "ticket_at", "invocation_id"),
        Index(
            "uq_work_slot_leases_workspace_held",
            "workspace_id",
            unique=True,
            postgresql_where=text("acquired_at IS NOT NULL"),
            sqlite_where=text("acquired_at IS NOT NULL"),
        ),
    )

    invocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    holder_worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    holder_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    ticket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    cleanup_unproven: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
