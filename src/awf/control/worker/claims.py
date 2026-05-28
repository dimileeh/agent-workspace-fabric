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
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.admission import (
    _acquire_requested_admission_lock,
    _requested_admission_lock_node_id,
    _requested_admission_row_slots,
)
from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
    _MONITOR_RECOVERY_EVENT_TYPE,
    _MONITOR_RECOVERY_REASON_CODE,
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
    WorkspaceRepository,
)
from awf.db.resilience import run_db_operation_with_retry
from awf.service.scheduler import SchedulerOrderCursor


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
    if self._executor is None:
        return max(0, claim_limit)
    return await _requested_admission_row_slots(session, config=self._config)


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
        await _acquire_requested_admission_lock(
            session,
            node_id=_requested_admission_lock_node_id(self._config.node_id),
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
            node_id=self._config.node_id or "local",
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
    node_id = self._config.node_id or "local"
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
            node_id=self._config.node_id or "local",
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
        await _acquire_requested_admission_lock(
            session,
            node_id=_requested_admission_lock_node_id(self._config.node_id),
        )
        row_slots = await _requested_claim_admission_slots(
            self,
            session,
            claim_limit=1,
        )
        if row_slots <= 0:
            return False
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.requested,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
        )
        if ws is not None:
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
        execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
            ws,
            claim_cutoff=now,
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
        await session.commit()
        if active_salvage_monitor_recovery_operation_id is not None:
            self._remember_active_salvage_monitor_recovery_operation_id(
                active_salvage_monitor_recovery_operation_id
            )
        return claimed


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


async def _refresh_execution_claim(self: Any, workspace_id: str) -> bool:
    async def _operation(session: AsyncSession) -> bool:
        lease_expires_at = self._execution_claim_expires_at()
        return await WorkspaceRepository(session).refresh_execution_claim(
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


async def _release_execution_claim(self: Any, workspace_id: str) -> None:
    try:
        async with self._session_factory() as session:
            await WorkspaceRepository(session).release_execution_claim(
                workspace_id,
                owner_id=self._worker_id,
            )
            await session.commit()
    except Exception:
        _log.exception(
            "worker.execution_claim_release_failed",
            workspace_id=workspace_id,
            worker_id=self._worker_id,
        )


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
