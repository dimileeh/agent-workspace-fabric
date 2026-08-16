"""Workspace resource and supply-chain policy helpers for executor quality gates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from awf.db.repositories import ResourceReservationRepository
from awf.profiles.models import WorkspaceProfile
from awf.service.supply_chain_policy import (
    SupplyChainPolicyRefreshResult,
    SupplyChainPolicyRefreshService,
)


async def _parallel_worker_cpu_limit_for_workspace(
    self: Any,
    workspace_id: str,
    *,
    profile: WorkspaceProfile,
) -> int | None:
    if profile.validation.coverage.parallel_workers is None:
        return None
    async with self._session_factory() as session:
        reservation = await ResourceReservationRepository(session).active_for_workspace(
            workspace_id
        )
    if reservation is None:
        return None
    return max(1, int(reservation.steady_cpu))


async def _refresh_supply_chain_policy_for_workspace(
    self: Any,
    *,
    workspace_id: str,
    command_evidence: Sequence[str],
    changed_paths: Sequence[str],
) -> SupplyChainPolicyRefreshResult:
    async with self._session_factory() as session:
        result = await SupplyChainPolicyRefreshService(session).refresh_workspace(
            workspace_id,
            command_evidence=command_evidence,
            changed_paths=changed_paths,
        )
        await session.commit()
        return result
