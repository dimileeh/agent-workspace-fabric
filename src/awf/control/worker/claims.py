"""Extracted ControlWorker domain operations.

This module contains mechanically moved methods from ``awf.control.worker.manager`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import contextlib as contextlib
import hashlib as hashlib
import json as json
import re as re
import subprocess as subprocess
import uuid as uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.executor.types import PauseResumeReason
from awf.control.worker.admission import (
    _acquire_requested_admission_locks,
    _requested_admission_lock_node_ids,
    _requested_admission_row_slots,
)
from awf.control.worker.config import effective_worker_config_node_id
from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
    _BLOCKED_RESUME_NO_EXECUTOR_REASON_CODE,
    _BLOCKED_RESUME_REASON_CODE,
    _MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM_REASON_CODE,
    _MONITOR_RECOVERY_DEFERRED_EVENT_TYPE,
    _MONITOR_RECOVERY_EVENT_TYPE,
    _MONITOR_RECOVERY_OWNER,
    _MONITOR_RECOVERY_REASON_CODE,
    _MONITOR_RECOVERY_SOURCE,
    _RECOVERING_RESUME_NO_EXECUTOR_REASON_CODE,
    _RECOVERING_RESUME_REASON_CODE,
    _SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL,
    LOCAL_CAPACITY_DEFERRED_REASON,
    LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON,
    LOCAL_CAPACITY_UNSATISFIABLE_REASON,
    QUEUE_DECISION_DEFERRED,
    QUEUE_DECISION_ORDERED,
)
from awf.control.worker.helpers import (
    _earliest_future_datetime,
    _latest_runtime_stranding_reason,
    _monitor_recovery_claim_cleanup_payload,
    _monitor_recovery_execution_claim_cleanup_payload,
    _monitor_recovery_payload,
    _requested_capacity_age_boost_changed,
    _requested_capacity_queue_signature,
    _utc_datetime,
    _workspace_claim_snapshot,
)
from awf.control.worker.logging import _log
from awf.control.worker.resource_broker import (
    _acquire_local_capacity_scheduler_lock,
    _allocated_reservation_signature,
    _allocated_totals_for_capacity_gate,
    _AllocatedReservationTotals,
    _local_capacity_blockers,
    _local_capacity_configured,
    _record_capacity_queue_decision,
    _reservation_demand_for_workspace,
)
from awf.control.worker.scheduling import (
    _CapacityQueueDecisionContext,
    _scheduler_candidate_cursor,
    _scheduler_candidate_fetch_limit,
)
from awf.control.worker.types import (
    _AllocatedReservationSignature,
    _RequestedCapacityClaimResult,
    _RequestedCapacityQueueSignature,
)
from awf.db.enums import (
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkerHeartbeatRepository,
    WorkspaceRepository,
)
from awf.db.repositories._scheduler import _scheduler_node_scope_condition
from awf.db.resilience import run_db_operation_with_retry
from awf.service.provider_recovery import rearm_recovering_cooldown_task_policy
from awf.service.scheduler import SchedulerOrderCursor


def _apply_execution_claim(self: Any, ws: Workspace, *, owner_id: str) -> None:
    """Stamp ``ws`` with this worker's execution claim and bump the fencing epoch.

    Shared by both requested-claim blocks (D8). ``execution_claim_epoch`` is
    incremented with a SQL-side expression so monotonicity holds regardless of
    any in-memory staleness: the row is freshly row-locked / atomically
    transitioned by the caller, so a later claimant always lands a strictly
    higher epoch and a stale worker is fenced on its next CAS write. The
    concrete value is read back later via ``_read_execution_claim_epoch`` (D2),
    so the SQL-expression placeholder is never read inside the claim txn.
    """
    ws.execution_claimed_by = owner_id
    ws.execution_claim_expires_at = self._execution_claim_expires_at()
    ws.execution_claim_epoch = Workspace.execution_claim_epoch + 1
    if self._config.node_id is not None:
        # Recovery for named workers is node-scoped, so ownership must be
        # persisted with the claim before a provisioner crash can strand it.
        ws.node_id = self._config.node_id


def _empty_requested_capacity_claim_result() -> _RequestedCapacityClaimResult:
    return _RequestedCapacityClaimResult(
        workspace_ids=[],
        resume_after=None,
        allocated_signature=None,
        requested_queue_signature=None,
        provider_suppression_resume_expires_at=None,
    )


async def _requested_claim_admission_slots(
    self: Any,
    session: AsyncSession,
    *,
    claim_limit: int,
) -> int:
    claim_limit = max(0, claim_limit)
    if self._executor is None:
        return claim_limit
    row_slots = await _requested_admission_row_slots(session, config=self._config)
    return min(claim_limit, row_slots)


async def _claim_requested_ids(
    self: Any,
    workspace_ids: list[str] | None = None,
    *,
    limit: int | None = None,
) -> list[str]:
    if self._config.max_concurrent_provisions <= 0:
        return []
    claim_limit = self._config.max_concurrent_provisions
    if limit is not None:
        claim_limit = min(claim_limit, max(0, limit))
    if claim_limit <= 0:
        return []
    if workspace_ids is not None and not workspace_ids:
        return []
    if not _local_capacity_configured(self._config):
        if workspace_ids is None:
            return []
        claimed: list[str] = []
        for workspace_id in workspace_ids:
            if len(claimed) >= claim_limit:
                break
            if await self._claim_requested_for_provisioning(workspace_id):
                claimed.append(workspace_id)
        return claimed

    async def _operation(session: AsyncSession) -> _RequestedCapacityClaimResult:
        await _acquire_requested_admission_locks(
            session,
            node_ids=_requested_admission_lock_node_ids(self._config.node_id),
        )
        row_slots = await _requested_claim_admission_slots(
            self,
            session,
            claim_limit=claim_limit,
        )
        if row_slots <= 0:
            return _empty_requested_capacity_claim_result()
        await _acquire_local_capacity_scheduler_lock(
            session,
            node_id=effective_worker_config_node_id(self._config),
        )
        return cast(
            _RequestedCapacityClaimResult,
            await self._claim_requested_ids_with_capacity(
                session,
                resume_after=self._requested_capacity_resume_after,
                resume_allocated_signature=self._requested_capacity_resume_signature,
                resume_requested_queue_signature=(self._requested_capacity_resume_queue_signature),
                resume_provider_suppression_expires_at=(
                    self._requested_capacity_resume_provider_suppression_expires_at
                ),
                claim_limit=min(claim_limit, row_slots),
            ),
        )

    result = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=False,
        on_retry=self._log_transient_db_retry,
    )
    self._requested_capacity_resume_after = result.resume_after
    self._requested_capacity_resume_signature = result.allocated_signature
    self._requested_capacity_resume_queue_signature = result.requested_queue_signature
    self._requested_capacity_resume_provider_suppression_expires_at = (
        result.provider_suppression_resume_expires_at
    )
    return result.workspace_ids


async def _claim_requested_ids_with_capacity(
    self: Any,
    session: AsyncSession,
    *,
    resume_after: SchedulerOrderCursor | None,
    resume_allocated_signature: _AllocatedReservationSignature | None,
    resume_requested_queue_signature: _RequestedCapacityQueueSignature | None,
    resume_provider_suppression_expires_at: datetime | None,
    claim_limit: int | None = None,
) -> _RequestedCapacityClaimResult:
    effective_claim_limit = self._config.max_concurrent_provisions
    if claim_limit is not None:
        effective_claim_limit = min(effective_claim_limit, max(0, claim_limit))
    if effective_claim_limit <= 0:
        return _empty_requested_capacity_claim_result()
    reservation_repo = ResourceReservationRepository(session)
    allocated = await _allocated_totals_for_capacity_gate(
        session,
        reservation_repo=reservation_repo,
        config=self._config,
    )
    allocated_signature = _allocated_reservation_signature(allocated)
    claimed: list[str] = []
    repo = WorkspaceRepository(session)
    node_id = effective_worker_config_node_id(self._config)
    decided_at = datetime.now(UTC)
    requested_queue_signature = await _requested_capacity_queue_signature(
        session,
        node_id=node_id,
        scoring_at=decided_at,
    )
    # A provider circuit can open between polls without changing the queue
    # or allocation signatures. That may reuse the cursor for one cycle; a
    # scan that observes suppression stores its expiry and later resets here.
    resume_provider_suppression_open = (
        resume_provider_suppression_expires_at is None
        or _utc_datetime(resume_provider_suppression_expires_at) > decided_at
    )
    candidate_limit = _scheduler_candidate_fetch_limit(effective_claim_limit)
    candidate_after: SchedulerOrderCursor | None = None
    if (
        resume_after is not None
        and resume_allocated_signature == allocated_signature
        and resume_requested_queue_signature == requested_queue_signature
        and resume_provider_suppression_open
        and not await _requested_capacity_age_boost_changed(
            session,
            node_id=node_id,
            since=resume_after.scoring_at,
            now=decided_at,
        )
    ):
        candidate_after = resume_after
    scoring_at = candidate_after.scoring_at if candidate_after is not None else decided_at
    capacity_refill_pages_remaining: int | None = None
    next_resume_after: SchedulerOrderCursor | None = None
    next_resume_signature: _AllocatedReservationSignature | None = None
    next_resume_queue_signature: _RequestedCapacityQueueSignature | None = None
    next_resume_provider_suppression_expires_at: datetime | None = None

    while len(claimed) < effective_claim_limit:
        workspaces = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.requested,
            limit=candidate_limit,
            node_id=node_id,
            after=candidate_after,
            scoring_at=scoring_at,
        )
        if not workspaces:
            break
        page_end_cursor = _scheduler_candidate_cursor(
            workspaces,
            scoring_at=scoring_at,
            dialect_name=repo.dialect_name,
        )

        workspaces_by_id = {workspace.id: workspace for workspace in workspaces}
        claim_slots = effective_claim_limit - len(claimed)
        page_filter_result = await self._filter_scheduler_candidate_workspaces_with_result(
            session,
            workspaces,
            limit=len(workspaces),
            scoring_at=scoring_at,
            provider_recovery_decision_candidate_limit=claim_slots,
        )
        page_dispatchable_ids = page_filter_result.workspace_ids
        next_resume_provider_suppression_expires_at = _earliest_future_datetime(
            next_resume_provider_suppression_expires_at,
            page_filter_result.provider_suppression_resume_expires_at,
            now=decided_at,
        )
        page_candidates = [
            workspaces_by_id[workspace_id]
            for workspace_id in page_dispatchable_ids
            if workspace_id in workspaces_by_id
        ]
        page_claimed = await self._claim_requested_capacity_candidates(
            session,
            repo=repo,
            reservation_repo=reservation_repo,
            candidates=page_candidates,
            allocated=allocated,
            claim_slots=claim_slots,
            decided_at=decided_at,
        )
        claimed.extend(page_claimed)
        if len(claimed) >= effective_claim_limit:
            break
        if len(workspaces) < candidate_limit:
            break
        if not page_claimed:
            if capacity_refill_pages_remaining is None:
                capacity_refill_pages_remaining = _SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL
            if capacity_refill_pages_remaining <= 0:
                next_resume_after = page_end_cursor
                if next_resume_after is not None:
                    next_resume_signature = _allocated_reservation_signature(allocated)
                    next_resume_queue_signature = requested_queue_signature
                break
            capacity_refill_pages_remaining -= 1
        else:
            capacity_refill_pages_remaining = None
        candidate_after = page_end_cursor
        if candidate_after is None:
            break

    return _RequestedCapacityClaimResult(
        workspace_ids=claimed,
        resume_after=next_resume_after,
        allocated_signature=next_resume_signature,
        requested_queue_signature=next_resume_queue_signature,
        provider_suppression_resume_expires_at=(
            next_resume_provider_suppression_expires_at if next_resume_after is not None else None
        ),
    )


async def _claim_requested_capacity_candidates(
    self: Any,
    session: AsyncSession,
    *,
    repo: WorkspaceRepository,
    reservation_repo: ResourceReservationRepository,
    candidates: list[Workspace],
    allocated: _AllocatedReservationTotals,
    claim_slots: int,
    decided_at: datetime,
) -> list[str]:
    if not candidates or claim_slots <= 0:
        return []

    candidate_ids = [workspace.id for workspace in candidates]
    reservations = await reservation_repo.active_latest_by_workspace_ids(candidate_ids)
    attempts_by_workspace = await TaskAttemptRepository(session).get_by_workspace_ids(candidate_ids)
    latest_decisions_by_workspace = await QueueDecisionRepository(session).latest_by_workspace_ids(
        candidate_ids
    )
    claimed: list[str] = []
    for workspace in candidates:
        if len(claimed) >= claim_slots:
            break
        queue_decision_context = _CapacityQueueDecisionContext(
            attempt=attempts_by_workspace.get(workspace.id),
            latest_decision=latest_decisions_by_workspace.get(workspace.id),
        )
        demand = _reservation_demand_for_workspace(
            workspace,
            reservation=reservations.get(workspace.id),
            config=self._config,
        )
        blockers = _local_capacity_blockers(
            allocated=allocated,
            demand=demand,
            config=self._config,
        )
        if blockers:
            reason_code = (
                LOCAL_CAPACITY_UNSATISFIABLE_REASON
                if any(blocker.unsatisfiable for blocker in blockers)
                else LOCAL_CAPACITY_DEFERRED_REASON
            )
            await _record_capacity_queue_decision(
                session,
                workspace,
                decision=QUEUE_DECISION_DEFERRED,
                reason_code=reason_code,
                decided_at=decided_at,
                allocated=allocated,
                demand=demand,
                blockers=blockers,
                context=queue_decision_context,
            )
            continue
        ws = await repo.transition_if_current(
            workspace.id,
            from_status=WorkspaceStatus.requested,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
        )
        if ws is None:
            await self._log_stale_requested_claims(session, [workspace.id])
            continue
        self._apply_execution_claim(ws, owner_id=self._worker_id)
        if demand.defaulted:
            await _record_capacity_queue_decision(
                session,
                ws,
                decision=QUEUE_DECISION_ORDERED,
                reason_code=LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON,
                decided_at=decided_at,
                allocated=allocated,
                demand=demand,
                blockers=[],
                context=queue_decision_context,
            )
        claimed.append(workspace.id)
        allocated.add(demand)
    return claimed


async def _log_stale_requested_claims(
    self: Any,
    session: AsyncSession,
    workspace_ids: list[str],
) -> None:
    _ = self
    repo = WorkspaceRepository(session)
    for workspace_id in workspace_ids:
        current = await repo.get(workspace_id)
        _log.info(
            "worker.skip_stale_dispatch",
            workspace_id=workspace_id,
            action="provision",
            expected_status=WorkspaceStatus.requested.value,
            status=current.status if current is not None else None,
        )


async def _claim_requested_for_provisioning(self: Any, workspace_id: str) -> bool:
    async with self._session_factory() as session:
        await _acquire_requested_admission_locks(
            session,
            node_ids=_requested_admission_lock_node_ids(self._config.node_id),
        )
        row_slots = await _requested_claim_admission_slots(
            self,
            session,
            claim_limit=1,
        )
        if row_slots <= 0:
            return False
        repo = WorkspaceRepository(session)
        claim_node_id = effective_worker_config_node_id(self._config)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.requested,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
            extra_conditions=(
                _scheduler_node_scope_condition(
                    status=WorkspaceStatus.requested,
                    node_id=claim_node_id,
                ),
            ),
        )
        if ws is not None:
            self._apply_execution_claim(ws, owner_id=self._worker_id)
            await session.commit()
            return True

        current = await repo.get(workspace_id)
        _log.info(
            "worker.skip_stale_dispatch",
            workspace_id=workspace_id,
            action="provision",
            expected_status=WorkspaceStatus.requested.value,
            status=current.status if current is not None else None,
        )
        return False


async def _claim_monitoring_pr_ids(self: Any, workspace_ids: list[str], *, limit: int) -> list[str]:
    claimed: list[str] = []
    for workspace_id in workspace_ids:
        if len(claimed) >= limit:
            break
        if workspace_id in self._execution_tasks:
            continue
        if await self._active_salvage_monitor_resume_cooldown_blocks_claim(workspace_id):
            continue
        if await self._claim_monitoring_pr(workspace_id):
            claimed.append(workspace_id)
    return claimed


async def _record_monitor_recovery_deferred_active_execution_claims(
    self: Any,
    *,
    limit: int,
    exclude_ids: set[str] | None = None,
) -> None:
    if limit <= 0:
        return

    claim_cutoff = datetime.now(UTC)
    row_limit = _scheduler_candidate_fetch_limit(limit)

    async def _operation(session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        fresh_execution_claim_owner_ids = await WorkerHeartbeatRepository(
            session
        ).list_fresh_worker_ids(now=claim_cutoff)
        workspaces = await repo.list_monitoring_pr_deferred_active_execution_claim_workspaces(
            limit=row_limit,
            claim_cutoff=claim_cutoff,
            owner_id=self._worker_id,
            exclude_ids=exclude_ids,
            scoring_at=claim_cutoff,
        )
        for ws in workspaces:
            await _record_monitor_recovery_deferred_active_execution_claim(
                self,
                repo,
                ws,
                previous_claim=_workspace_claim_snapshot(ws),
                runtime_stranding_reason=_latest_runtime_stranding_reason(ws.events),
                claim_cutoff=claim_cutoff,
                fresh_execution_claim_owner_ids=fresh_execution_claim_owner_ids,
                execution_claim_owner_id=self._worker_id,
            )

    await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=False,
        on_retry=self._log_transient_db_retry,
    )


def _monitor_recovery_deferred_event_seen(
    events: list[Any],
    *,
    execution_claim_cleanup: dict[str, Any],
) -> bool:
    for event in reversed(events):
        if (
            event.event_type != _MONITOR_RECOVERY_DEFERRED_EVENT_TYPE
            or event.reason_code != _MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM_REASON_CODE
        ):
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        prior_execution_claim = payload.get("execution_claim")
        if not isinstance(prior_execution_claim, dict):
            continue
        if prior_execution_claim.get("previous_claimed_by") == execution_claim_cleanup.get(
            "previous_claimed_by"
        ):
            return True
    return False


async def _record_monitor_recovery_deferred_active_execution_claim(
    self: Any,
    repo: WorkspaceRepository,
    ws: Workspace,
    *,
    previous_claim: Mapping[str, object],
    runtime_stranding_reason: str | None,
    claim_cutoff: datetime,
    fresh_execution_claim_owner_ids: set[str] | None = None,
    execution_claim_owner_id: str | None = None,
) -> None:
    execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
        ws,
        claim_cutoff=claim_cutoff,
        fresh_execution_claim_owner_ids=fresh_execution_claim_owner_ids,
        execution_claim_owner_id=execution_claim_owner_id,
    )
    if (
        execution_claim_cleanup.get("action") != "preserved_unexpired"
        or execution_claim_cleanup.get("previous_claimed_by") == self._worker_id
        or _monitor_recovery_deferred_event_seen(
            ws.events,
            execution_claim_cleanup=execution_claim_cleanup,
        )
    ):
        return
    await repo.add_event(
        ws,
        event_type=_MONITOR_RECOVERY_DEFERRED_EVENT_TYPE,
        reason_code=_MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM_REASON_CODE,
        payload={
            "source": _MONITOR_RECOVERY_SOURCE,
            "owner": _MONITOR_RECOVERY_OWNER,
            "reason_code": _MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM_REASON_CODE,
            "message": (
                "AWF deferred monitor recovery because another unexpired execution "
                "claim still owns the workspace."
            ),
            "worker_id": self._worker_id,
            "workspace_status": ws.status,
            "previous_claim": dict(previous_claim),
            "runtime_stranding_reason": runtime_stranding_reason,
            "execution_claim": execution_claim_cleanup,
            "monitor_claim": {
                "action": "deferred",
                "reason_code": _MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM_REASON_CODE,
                "claimed_by": ws.monitor_claimed_by,
                "expires_at": (
                    _utc_datetime(ws.monitor_claim_expires_at).isoformat()
                    if ws.monitor_claim_expires_at is not None
                    else None
                ),
            },
        },
    )


async def _claim_monitoring_pr(self: Any, workspace_id: str) -> bool:
    now = datetime.now(UTC)
    lease_expires_at = now + timedelta(seconds=self._config.monitor_claim_lease_seconds)
    active_salvage_monitor_recovery_operation_id: str | None = None
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:
            return False
        previous_claim = _workspace_claim_snapshot(ws)
        runtime_stranding_reason = _latest_runtime_stranding_reason(ws.events)
        fresh_execution_claim_owner_ids = await WorkerHeartbeatRepository(
            session
        ).list_fresh_worker_ids(now=now)
        execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
            ws,
            claim_cutoff=now,
            fresh_execution_claim_owner_ids=fresh_execution_claim_owner_ids,
            execution_claim_owner_id=self._worker_id,
        )
        claimed = await repo.claim_monitoring_pr(
            workspace_id,
            owner_id=self._worker_id,
            lease_expires_at=lease_expires_at,
            now=now,
            clear_stale_execution_claim_cutoff=now,
        )
        if claimed:
            await session.refresh(ws)
            if (
                ws.execution_claimed_by is not None
                or ws.execution_claim_expires_at is not None
                or execution_claim_cleanup["action"] == "preserved_unexpired"
            ):
                execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
                    ws,
                    claim_cutoff=now,
                )
            claim_cleanup = _monitor_recovery_claim_cleanup_payload(
                ws,
                claim_cutoff=now,
                monitor_claimed_by=self._worker_id,
                monitor_claim_expires_at=lease_expires_at,
                execution_claim_cleanup=execution_claim_cleanup,
            )
            operation_payload = _monitor_recovery_payload(
                ws,
                worker_id=self._worker_id,
                previous_claim=previous_claim,
                claim_cleanup=claim_cleanup,
                runtime_stranding_reason=runtime_stranding_reason,
            )
            operation = await OperationRepository(session).create(
                workspace_id=workspace_id,
                operation_type=OperationType.remonitor,
                status=OperationStatus.running,
                payload=operation_payload,
            )
            if (
                operation_payload.get("active_execution_salvage_reason_code")
                == _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE
            ):
                active_salvage_monitor_recovery_operation_id = operation.id
            await repo.add_event(
                ws,
                event_type=_MONITOR_RECOVERY_EVENT_TYPE,
                reason_code=_MONITOR_RECOVERY_REASON_CODE,
                payload={
                    **operation_payload,
                    "operation_id": operation.id,
                },
            )
            self._monitor_recovery_operation_ids[workspace_id] = operation.id
        else:
            ws = await repo.get(workspace_id, populate_existing=True)
            if ws is None:
                await session.commit()
                return False
            fresh_execution_claim_owner_ids = await WorkerHeartbeatRepository(
                session
            ).list_fresh_worker_ids(now=now)
            await _record_monitor_recovery_deferred_active_execution_claim(
                self,
                repo,
                ws,
                previous_claim=previous_claim,
                runtime_stranding_reason=runtime_stranding_reason,
                claim_cutoff=now,
                fresh_execution_claim_owner_ids=fresh_execution_claim_owner_ids,
                execution_claim_owner_id=self._worker_id,
            )
        await session.commit()
        if active_salvage_monitor_recovery_operation_id is not None:
            self._remember_active_salvage_monitor_recovery_operation_id(
                active_salvage_monitor_recovery_operation_id
            )
        return claimed


_PAUSED_RESUME_FROM_STATUS: dict[str, WorkspaceStatus] = {
    "blocked": WorkspaceStatus.blocked,
    "recovering": WorkspaceStatus.recovering,
}

_PAUSED_RESUME_CLAIM_REASON_CODE: dict[str, str] = {
    "blocked": _BLOCKED_RESUME_REASON_CODE,
    "recovering": _RECOVERING_RESUME_REASON_CODE,
}


async def _claim_paused_for_resume(
    self: Any, workspace_id: str, *, reason: PauseResumeReason
) -> bool:
    """Atomically claim a paused (``blocked``/``recovering``) workspace for resume.

    Epoch-fenced: the ``<paused> -> running`` transition is the CAS — exactly one
    worker wins (the loser sees ``running`` and updates 0 rows), so a double
    resume cannot run the warm stack twice. ``reason`` selects the paused source
    status (``blocked`` operator pause vs ``recovering`` provider-failure pause).
    The execution claim + fencing epoch are (re-)stamped via the shared
    ``_apply_execution_claim`` so a stale prior executor is fenced on its next
    write."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=_PAUSED_RESUME_FROM_STATUS[reason],
            to=WorkspaceStatus.running,
            reason_code=_PAUSED_RESUME_CLAIM_REASON_CODE[reason],
        )
        if ws is None:
            return False
        _apply_execution_claim(self, ws, owner_id=self._worker_id)
        await session.commit()
        return True


async def _claim_blocked_for_resume(self: Any, workspace_id: str) -> bool:
    """Claim a ``blocked`` workspace for resume — shim over ``_claim_paused_for_resume``."""
    return await _claim_paused_for_resume(self, workspace_id, reason="blocked")


async def _claim_recovering_for_resume(self: Any, workspace_id: str) -> bool:
    """Claim a ``recovering`` workspace for resume — shim over ``_claim_paused_for_resume`` (#612)."""
    return await _claim_paused_for_resume(self, workspace_id, reason="recovering")


async def _restore_paused_resume_claim(
    self: Any,
    workspace_id: str,
    *,
    reason: PauseResumeReason,
    reason_code: str,
    rearm_recovering_cooldown: bool = False,
) -> None:
    """Revert a won paused-resume back to its paused status when it cannot be driven.

    ``_claim_paused_for_resume`` performs the ``<paused> -> running`` CAS *before*
    dispatch, so if the resume never starts the row would otherwise be left
    stranded in ``running`` (its claim released elsewhere) until stale-active
    recovery FAILS it — dropping the paused state operators/auto-retry expect.
    Reverting to the paused status restores it: the claim CAS never touched the
    pause bookkeeping (``block_epoch``/``pending_operator_hint``/grants for
    ``blocked``; ``provider_recovery_state``/cooldown for ``recovering``), so the
    next cycle resumes it cleanly once it can run. Owner-gated so a newer claimant
    that already fenced us is never clobbered. The ``reason_code`` records *why*
    the resume was abandoned (no executor vs. an aborted post-claim dispatch).

    ``rearm_recovering_cooldown`` (#647) re-arms ``provider_recovery_state``
    ``not_before`` to a fresh cooldown for every ``recovering`` revert that did not
    make progress (worktree-reset abort, post-claim executor failure, and
    dispatch abort): those paths leave the cooldown in the past, so without
    re-arming ``list_resumable_recovering_ids`` would re-select the row every poll
    and a persistent failure would busy-loop the executor slot instead of backing
    off for a later safe retry."""
    try:
        async with self._session_factory() as session:
            ws = await WorkspaceRepository(session).transition_if_current(
                workspace_id,
                from_status=WorkspaceStatus.running,
                to=_PAUSED_RESUME_FROM_STATUS[reason],
                reason_code=reason_code,
                extra_conditions=(Workspace.execution_claimed_by == self._worker_id,),
            )
            if ws is not None:
                if rearm_recovering_cooldown:
                    rearmed = rearm_recovering_cooldown_task_policy(
                        ws.task_policy, now=datetime.now(UTC)
                    )
                    # A ``recovering`` row reverted on the reset-abort path always
                    # carries ``provider_recovery_state`` (the divert into recovering
                    # writes it), so ``rearmed`` is never ``None`` here; the ``None``
                    # arc is unreachable defensive code for a malformed row — hence
                    # ``# pragma: no branch``.
                    if rearmed is not None:  # pragma: no branch
                        ws.task_policy = rearmed
                await session.commit()
    except Exception:
        _log.exception(
            f"worker.{reason}_resume_restore_failed",
            workspace_id=workspace_id,
            worker_id=self._worker_id,
            reason_code=reason_code,
        )


async def _restore_blocked_resume_claim(self: Any, workspace_id: str, *, reason_code: str) -> None:
    """Revert a won blocked-resume to ``blocked`` — shim over ``_restore_paused_resume_claim``."""
    await _restore_paused_resume_claim(
        self, workspace_id, reason="blocked", reason_code=reason_code
    )


async def _restore_recovering_resume_claim(
    self: Any, workspace_id: str, *, reason_code: str
) -> None:
    """Revert a won recovering-resume to ``recovering`` — shim over ``_restore_paused_resume_claim`` (#612).

    Re-arms the provider cooldown (#647) like the worktree-reset-abort path: this
    shim drives the post-claim dispatch-abort revert (a failed ordered-decision
    write, etc.), which leaves ``not_before`` in the past. Without re-arming a
    persistent dispatch abort would let ``list_resumable_recovering_ids`` re-select
    the row every poll and busy-loop the executor slot instead of backing off a
    full cooldown before the next safe retry."""
    await _restore_paused_resume_claim(
        self,
        workspace_id,
        reason="recovering",
        reason_code=reason_code,
        rearm_recovering_cooldown=True,
    )


_PAUSED_RESUME_NO_EXECUTOR_REASON_CODE: dict[str, str] = {
    "blocked": _BLOCKED_RESUME_NO_EXECUTOR_REASON_CODE,
    "recovering": _RECOVERING_RESUME_NO_EXECUTOR_REASON_CODE,
}


async def _restore_paused_after_missing_executor(
    self: Any, workspace_id: str, *, reason: PauseResumeReason
) -> None:
    """Revert a won paused-resume back to its paused status when no executor can drive it.

    Used inside the dispatched resume task (the claim is released by the caller's
    ``finally``); ``reason`` selects the paused status + no-executor reason code.

    Like the other ``recovering`` revert paths (worktree-reset abort, post-claim
    dispatch abort), re-arm the provider cooldown (#647) for ``recovering``: this
    revert also leaves ``not_before`` in the past, so without re-arming
    ``list_resumable_recovering_ids`` would re-select the row every poll and a
    worker that persistently has no executor would busy-loop the executor slot
    instead of backing off a full cooldown before the next safe retry."""
    await _restore_paused_resume_claim(
        self,
        workspace_id,
        reason=reason,
        reason_code=_PAUSED_RESUME_NO_EXECUTOR_REASON_CODE[reason],
        rearm_recovering_cooldown=(reason == "recovering"),
    )


async def _restore_blocked_after_missing_executor(self: Any, workspace_id: str) -> None:
    """Revert a won blocked-resume to ``blocked`` — shim over ``_restore_paused_after_missing_executor``."""
    await _restore_paused_after_missing_executor(self, workspace_id, reason="blocked")


async def _restore_recovering_after_missing_executor(self: Any, workspace_id: str) -> None:
    """Revert a won recovering-resume to ``recovering`` — shim over ``_restore_paused_after_missing_executor`` (#612).

    Re-arms the provider cooldown (#647) via the ``reason == "recovering"`` gate so
    a worker that persistently lacks an executor backs off a full cooldown instead
    of re-selecting the row every poll and busy-looping the slot."""
    await _restore_paused_after_missing_executor(self, workspace_id, reason="recovering")


async def _restore_paused_resume_claim_after_cancellation(
    self: Any, workspace_id: str, *, reason: PauseResumeReason, reason_code: str
) -> None:
    """Restore a cancelled paused-resume back to its paused status even if cancelled again.

    ``_safely_resume_paused_claimed``'s ``CancelledError`` handler must revert the
    post-claim ``running`` row to its paused status before re-raising, but the
    restore is itself a cancellable DB write. A second cancellation (e.g. worker
    shutdown cancelling outstanding tasks) landing mid-write would propagate out of
    the un-shielded restore (which only catches ``Exception``), skipping the commit
    so the caller's ``finally`` releases the claim and leaves the row stranded in
    ``running`` for stale-active recovery to FAIL. Shield the restore and re-await
    across repeated cancellations so it always runs to completion, mirroring
    ``_release_execution_claim_after_cancellation``.

    Re-arm the provider cooldown (#647) for a ``recovering`` revert exactly like the
    post-claim executor-failure path: the claim CAS already moved the row to
    ``running`` while leaving ``not_before`` in the past, so without re-arming
    ``list_resumable_recovering_ids`` would re-select the row every poll and a
    repeated cancellation loop (e.g. worker shutdown) would busy-loop the executor
    slot instead of backing off a full cooldown. ``blocked`` reverts carry no
    cooldown, so the rearm is gated to ``recovering``.
    """
    restore_task = asyncio.create_task(
        self._restore_paused_resume_claim(
            workspace_id,
            reason=reason,
            reason_code=reason_code,
            rearm_recovering_cooldown=reason == "recovering",
        ),
        name=f"awf-{reason}-resume-restore-{workspace_id}",
    )
    while not restore_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(restore_task)


async def _restore_blocked_resume_claim_after_cancellation(
    self: Any, workspace_id: str, *, reason_code: str
) -> None:
    """Shielded blocked-resume restore — shim over ``_restore_paused_resume_claim_after_cancellation``."""
    await _restore_paused_resume_claim_after_cancellation(
        self, workspace_id, reason="blocked", reason_code=reason_code
    )


async def _restore_recovering_resume_claim_after_cancellation(
    self: Any, workspace_id: str, *, reason_code: str
) -> None:
    """Shielded recovering-resume restore — shim over ``_restore_paused_resume_claim_after_cancellation`` (#612)."""
    await _restore_paused_resume_claim_after_cancellation(
        self, workspace_id, reason="recovering", reason_code=reason_code
    )


async def _claim_paused_resume_ids(
    self: Any, workspace_ids: list[str], *, limit: int, reason: PauseResumeReason
) -> list[str]:
    """Claim each resumable paused workspace for resume, in order.

    Mirrors ``_claim_monitoring_pr_ids``: the per-workspace CAS in
    ``_claim_paused_for_resume`` performs the ``<paused> -> running`` transition,
    so only the rows it actually wins (and that are not already running locally)
    are returned for dispatch. ``reason`` selects the paused source status."""
    claimed: list[str] = []
    for workspace_id in workspace_ids:
        if len(claimed) >= limit:
            break
        if workspace_id in self._execution_tasks:
            continue
        if await _claim_paused_for_resume(self, workspace_id, reason=reason):
            claimed.append(workspace_id)
    return claimed


async def _claim_blocked_resume_ids(
    self: Any, workspace_ids: list[str], *, limit: int
) -> list[str]:
    """Claim ``blocked`` resumes — shim over ``_claim_paused_resume_ids``."""
    return await _claim_paused_resume_ids(self, workspace_ids, limit=limit, reason="blocked")


async def _claim_recovering_resume_ids(
    self: Any, workspace_ids: list[str], *, limit: int
) -> list[str]:
    """Claim ``recovering`` resumes — shim over ``_claim_paused_resume_ids`` (#612)."""
    return await _claim_paused_resume_ids(self, workspace_ids, limit=limit, reason="recovering")


async def _safely_resume_claimed_pr_monitor(
    self: Any,
    workspace_id: str,
    *,
    recovery_operation_id: str | None = None,
) -> None:
    heartbeat = asyncio.create_task(
        self._refresh_monitoring_pr_claim_loop(workspace_id),
        name=f"awf-monitor-claim-{workspace_id}",
    )
    resume_succeeded = False
    try:
        resume_succeeded = await self._safely_resume_pr_monitor(
            workspace_id,
            recovery_operation_id=recovery_operation_id,
        )
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        if (
            resume_succeeded
            and recovery_operation_id is not None
            and recovery_operation_id in self._active_salvage_monitor_recovery_operation_ids
        ):
            cooldown_seconds = max(0.0, self._config.monitor_claim_lease_seconds)
            self._remember_active_salvage_monitor_resume_cooldown(
                workspace_id,
                monotonic() + cooldown_seconds,
            )
            if cooldown_seconds > 0:
                await self._record_active_salvage_monitor_resume_cooldown(
                    workspace_id,
                    recovery_operation_id=recovery_operation_id,
                    cooldown_until=datetime.now(UTC) + timedelta(seconds=cooldown_seconds),
                )
        await self._release_monitoring_pr_claim(workspace_id)
        self._monitor_recovery_operation_ids.pop(workspace_id, None)
        if recovery_operation_id is not None:
            self._forget_active_salvage_monitor_recovery_operation_id(recovery_operation_id)
        # Promptly release the terminal runtime when the monitor ended terminal
        # (merge → ``completed``, abort → ``failed``), reclaiming the compose stack
        # + per-ws auth overlay immediately rather than on the ~1h interval
        # (#583, #584). A no-op for a still-monitoring exit and idempotent against
        # the periodic backstop, which stays in place.
        await self._release_terminal_runtime_promptly(workspace_id)


async def _finish_monitor_recovery_operation(
    self: Any,
    workspace_id: str,
    *,
    operation_id: str | None,
    status: OperationStatus,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if operation_id is None:
        return
    try:
        async with self._session_factory() as session:
            operation_repo = OperationRepository(session)
            operation = await operation_repo.get(operation_id)
            if operation is None or operation.workspace_id != workspace_id:
                return
            ws = await WorkspaceRepository(session).get(workspace_id)
            result: dict[str, Any] = {
                "requested_action": OperationType.remonitor.value,
                "worker_id": self._worker_id,
            }
            if ws is not None:
                result.update(
                    {
                        "status": ws.status,
                        "pr_url": ws.pr_url,
                        "pr_number": ws.pr_number,
                    }
                )
            await operation_repo.finish(
                operation,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            await session.commit()
    except Exception:
        _log.exception(
            "worker.monitor_recovery_operation_finish_failed",
            workspace_id=workspace_id,
            operation_id=operation_id,
            status=status.value,
        )


async def _finish_monitor_recovery_operation_after_cancellation(
    self: Any,
    workspace_id: str,
    *,
    operation_id: str | None,
    status: OperationStatus,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Finalize the recovery operation even if cancelled again mid-write.

    The CancelledError handler must persist this status before re-raising,
    but the finalize is itself a cancellable DB write. A second cancellation
    (e.g. worker shutdown cancelling outstanding tasks) landing mid-write
    would leave the operation stuck in ``running`` once the caller's
    ``finally`` drops its ``_monitor_recovery_operation_ids`` handle, with
    nothing able to finish it later. Shield the write and re-await across
    repeated cancellations so it always runs to completion, mirroring
    ``cleanup_compose_exec_invocation_after_cancellation``.
    """
    finalize_task = asyncio.create_task(
        self._finish_monitor_recovery_operation(
            workspace_id,
            operation_id=operation_id,
            status=status,
            error_code=error_code,
            error_message=error_message,
        ),
        name=f"awf-monitor-recovery-finalize-{workspace_id}",
    )
    while True:
        try:
            await asyncio.shield(finalize_task)
            return
        except asyncio.CancelledError:
            if finalize_task.done():
                return


async def _refresh_monitoring_pr_claim(self: Any, workspace_id: str) -> bool:
    async def _operation(session: AsyncSession) -> bool:
        lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=self._config.monitor_claim_lease_seconds
        )
        return await WorkspaceRepository(session).refresh_monitoring_pr_claim(
            workspace_id,
            owner_id=self._worker_id,
            lease_expires_at=lease_expires_at,
        )

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=True,
        on_retry=self._log_transient_db_retry,
    )


async def _read_execution_claim_epoch(self: Any, workspace_id: str) -> int | None:
    """Read this worker's current execution-claim epoch (D2).

    Returns ``None`` when the claim is no longer held by this worker (a newer
    claimant reclaimed it, or the row is gone), in which case the caller aborts
    the provision before doing any work.
    """
    async with self._session_factory() as session:
        return await WorkspaceRepository(session).read_execution_claim_epoch(
            workspace_id,
            owner_id=self._worker_id,
        )


async def _refresh_execution_claim(self: Any, workspace_id: str) -> bool:
    async def _operation(session: AsyncSession) -> bool:
        lease_expires_at = self._execution_claim_expires_at()
        return await WorkspaceRepository(session).refresh_execution_claim(
            workspace_id,
            owner_id=self._worker_id,
            lease_expires_at=lease_expires_at,
            execution_claim_epoch=self._execution_claim_epochs.get(workspace_id),
        )

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=True,
        on_retry=self._log_transient_db_retry,
    )


async def _release_execution_claim(
    self: Any, workspace_id: str, *, skip_if_blocked: bool = False
) -> None:
    try:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            if skip_if_blocked:
                ws = await repo.get(workspace_id)
                if ws is not None and ws.status in {
                    WorkspaceStatus.blocked.value,
                    WorkspaceStatus.recovering.value,
                }:
                    # A ``blocked`` (operator pause) or ``recovering`` (auto-healing
                    # provider-failure pause, #612) workspace keeps its worktree,
                    # warm stack, and execution claim as the *durable lease* (see
                    # ``enter_blocked_for_protected_violation`` /
                    # ``enter_recovering_for_provider_failure`` and
                    # ``tests/.../test_*_status_membership``). When a genuine
                    # execution pauses into one of these statuses, this ``finally``
                    # reaches here right after the pause; releasing the claim now
                    # would leave the row paused with ``execution_claimed_by``
                    # cleared, stranding it without the fencing/ownership the
                    # membership contract and the resume path expect until a resume
                    # re-stamps it. Skip the release so the warm-stack lease stays
                    # held across the operator decision / provider cooldown.
                    #
                    # Only the execution dispatch passes ``skip_if_blocked``: the
                    # paused-resume paths deliberately release when they revert to
                    # the paused status after finding no executor, so a capable
                    # worker can re-claim it (see
                    # ``test_resume_blocked_claimed_releases_claim_when_executor_missing``).
                    return
            released = await repo.release_execution_claim(
                workspace_id,
                owner_id=self._worker_id,
                execution_claim_epoch=self._execution_claim_epochs.get(workspace_id),
            )
            if released:
                await session.commit()
    except Exception:
        _log.exception(
            "worker.execution_claim_release_failed",
            workspace_id=workspace_id,
            worker_id=self._worker_id,
        )


async def _release_execution_claim_after_cancellation(self: Any, workspace_id: str) -> None:
    """Release the execution claim and drop its epoch even if cancelled again.

    ``_safely_provision_claimed``'s ``finally`` runs while an external cancel
    (e.g. worker shutdown) is already propagating. The release is itself a
    cancellable DB write and the in-memory epoch pop follows it; a second
    cancellation landing mid-write would propagate out of the un-shielded
    release (which only catches ``Exception``), skipping both and leaking the DB
    lease plus the epoch entry. Shield the release and re-await across repeated
    cancellations so it always runs to completion, then drop the epoch, mirroring
    ``_finish_monitor_recovery_operation_after_cancellation``.
    """
    release_task = asyncio.create_task(
        self._release_execution_claim(workspace_id),
        name=f"awf-execution-claim-release-{workspace_id}",
    )
    while not release_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(release_task)
    self._execution_claim_epochs.pop(workspace_id, None)


async def _release_monitoring_pr_claim(self: Any, workspace_id: str) -> None:
    try:
        async with self._session_factory() as session:
            await WorkspaceRepository(session).release_monitoring_pr_claim(
                workspace_id,
                owner_id=self._worker_id,
            )
            await session.commit()
    except Exception:
        _log.exception(
            "worker.monitor_claim_release_failed",
            workspace_id=workspace_id,
            worker_id=self._worker_id,
        )
