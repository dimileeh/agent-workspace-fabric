"""Non-secret durable authority. Independent of legacy workspace secret leases."""

from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from awf.db.base import Base


class Owner(Base):
    __tablename__ = "hosted_owners"
    __table_args__ = (CheckConstraint("recovery_generation > 0 AND boot_epoch > 0"),)
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    incarnation: Mapped[str] = mapped_column(String(128))
    recovery_generation: Mapped[int] = mapped_column(BigInteger)
    boot_epoch: Mapped[int] = mapped_column(BigInteger)
    acknowledgement: Mapped[str | None] = mapped_column(String(128), nullable=True)


class PreparationRow(Base):
    __tablename__ = "hosted_preparations"
    __table_args__ = (UniqueConstraint("owner", "namespace", "workspace_id", "preparation_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    workspace_id: Mapped[str] = mapped_column(String(128))
    preparation_id: Mapped[str] = mapped_column(String(128))
    binding: Mapped[dict[str, Any]] = mapped_column(JSON)


class Grant(Base):
    __tablename__ = "hosted_grants"
    __table_args__ = (
        UniqueConstraint("preparation", "grant_id"),
        CheckConstraint("fence > 0 AND revision > 0 AND authorization_generation > 0"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    preparation: Mapped[str] = mapped_column(String(32))
    grant_id: Mapped[str] = mapped_column(String(128))
    fence: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    authorization_generation: Mapped[int] = mapped_column(BigInteger)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    current: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pending: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Receipt(Base):
    __tablename__ = "hosted_receipts"
    __table_args__ = (UniqueConstraint("owner", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    grant: Mapped[str] = mapped_column(String(32), index=True)
    binding: Mapped[dict[str, Any]] = mapped_column(JSON)
    operation: Mapped[str] = mapped_column(String(16))
    predecessor: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(16))
