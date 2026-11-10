"""Repository for ``provider_managed_keys`` rows (managed OpenRouter keys).

Flush-only like every repository; callers commit at their boundary. Spend
aggregation is one grouped query so list/overview surfaces stay O(1) queries.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import ProviderManagedKeyStatus
from awf.db.provider_key_models import ProviderManagedKey

UNFINALIZED_KEY_STATUSES: tuple[str, ...] = (
    ProviderManagedKeyStatus.minting.value,
    ProviderManagedKeyStatus.active.value,
    ProviderManagedKeyStatus.delete_failed.value,
)
"""Rows whose key may still exist at the provider and still reserve headroom."""


@dataclass(frozen=True)
class ProviderSpendSummary:
    """Exact provider spend for one workspace, summed across its keys."""

    spend_usd: Decimal
    key_count: int
    live_key_count: int
    last_read_at: datetime | None


class ProviderManagedKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: ProviderManagedKey) -> ProviderManagedKey:
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, key_id: str, *, for_update: bool = False) -> ProviderManagedKey | None:
        return await self._session.get(ProviderManagedKey, key_id, with_for_update=for_update)

    async def for_workspace(self, workspace_id: str) -> list[ProviderManagedKey]:
        stmt = (
            select(ProviderManagedKey)
            .where(ProviderManagedKey.workspace_id == workspace_id)
            .order_by(ProviderManagedKey.seq)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def unfinalized_for_workspace(self, workspace_id: str) -> list[ProviderManagedKey]:
        stmt = (
            select(ProviderManagedKey)
            .where(
                ProviderManagedKey.workspace_id == workspace_id,
                ProviderManagedKey.status.in_(UNFINALIZED_KEY_STATUSES),
            )
            .order_by(ProviderManagedKey.seq)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def next_seq(self, workspace_id: str) -> int:
        stmt = select(func.coalesce(func.max(ProviderManagedKey.seq), 0)).where(
            ProviderManagedKey.workspace_id == workspace_id
        )
        return int((await self._session.execute(stmt)).scalar_one()) + 1

    async def by_hashes(self, hashes: Sequence[str]) -> dict[str, ProviderManagedKey]:
        if not hashes:
            return {}
        stmt = select(ProviderManagedKey).where(ProviderManagedKey.key_hash.in_(list(hashes)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {str(row.key_hash): row for row in rows}

    async def by_names(self, names: Sequence[str]) -> dict[str, ProviderManagedKey]:
        if not names:
            return {}
        stmt = select(ProviderManagedKey).where(ProviderManagedKey.key_name.in_(list(names)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.key_name: row for row in rows}

    async def hashless_intents_created_before(
        self, cutoff: datetime, *, limit: int
    ) -> list[ProviderManagedKey]:
        """``minting`` intents that never recorded a hash, locked against activation."""
        stmt = (
            select(ProviderManagedKey)
            .where(
                ProviderManagedKey.status == ProviderManagedKeyStatus.minting.value,
                ProviderManagedKey.key_hash.is_(None),
                ProviderManagedKey.created_at <= cutoff,
            )
            .order_by(ProviderManagedKey.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def workspace_ids_with_unfinalized_keys(self, *, limit: int) -> list[str]:
        stmt = (
            select(ProviderManagedKey.workspace_id)
            .where(ProviderManagedKey.status.in_(UNFINALIZED_KEY_STATUSES))
            .group_by(ProviderManagedKey.workspace_id)
            .order_by(func.min(ProviderManagedKey.created_at))
            .limit(limit)
        )
        return [str(value) for value in (await self._session.execute(stmt)).scalars().all()]

    async def spend_by_workspace_ids(
        self, workspace_ids: Sequence[str]
    ) -> dict[str, ProviderSpendSummary]:
        """Sum exact spend per workspace; minting failures never count as keys."""
        if not workspace_ids:
            return {}
        counted = ProviderManagedKey.status != ProviderManagedKeyStatus.mint_failed.value
        stmt = (
            select(
                ProviderManagedKey.workspace_id,
                func.coalesce(
                    func.sum(ProviderManagedKey.usage_usd + ProviderManagedKey.byok_usage_usd), 0
                ),
                func.count(case((counted, 1))),
                func.count(case((ProviderManagedKey.status.in_(UNFINALIZED_KEY_STATUSES), 1))),
                func.max(ProviderManagedKey.usage_read_at),
            )
            .where(ProviderManagedKey.workspace_id.in_(list(workspace_ids)))
            .group_by(ProviderManagedKey.workspace_id)
        )
        return {
            str(workspace_id): ProviderSpendSummary(
                spend_usd=Decimal(spend),
                key_count=int(key_count),
                live_key_count=int(live_count),
                last_read_at=last_read_at,
            )
            for workspace_id, spend, key_count, live_count, last_read_at in (
                await self._session.execute(stmt)
            ).all()
        }
