"""Managed provider-key identity model (managed OpenRouter keys).

Kept out of ``awf.db.models`` (which re-exports it) so that module stays within
the first-party file-line guardrail.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from awf.db.base import Base, _now

USD = Numeric(14, 6)


class ProviderManagedKey(Base):
    """One AWF-minted provider key for one agent invocation of one workspace.

    Only non-secret identity is persisted: the key name ``awf-<workspace>-<seq>``,
    its provider hash, the USD limit it was minted with and the last usage read.
    The key value is held in worker memory for its invocation and never stored.
    Workspace spend is the sum of ``usage_usd + byok_usage_usd`` across rows.
    """

    __tablename__ = "provider_managed_keys"
    __table_args__ = (
        UniqueConstraint("workspace_id", "seq", name="uq_provider_managed_keys_workspace_seq"),
        UniqueConstraint("key_name", name="uq_provider_managed_keys_key_name"),
        UniqueConstraint("key_hash", name="uq_provider_managed_keys_key_hash"),
        Index("ix_provider_managed_keys_workspace", "workspace_id"),
        Index("ix_provider_managed_keys_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(USD, nullable=False)
    usage_usd: Mapped[Decimal] = mapped_column(
        USD, nullable=False, default=Decimal(0), server_default="0"
    )
    byok_usage_usd: Mapped[Decimal] = mapped_column(
        USD, nullable=False, default=Decimal(0), server_default="0"
    )
    usage_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_usage_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def spend_usd(self) -> Decimal:
        return Decimal(self.usage_usd or 0) + Decimal(self.byok_usage_usd or 0)
