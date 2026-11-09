"""Worker delegates for broker-owned work-slot leases (slot-release).

A work slot means provisioning, an agent run or a validation run is in flight
right now. ``ControlWorker`` wires these through ``WorkerDelegatesMixin``:

* new work (requested claims, lease-less READY dispatch) is admitted against the
  DB lease count, never ahead of a live reacquire waiter;
* every dispatched execution task runs inside a per-invocation
  :class:`InvocationWorkSlot` bound into the executor, so agent and validation
  hooks reacquire FIFO and pure waiting holds nothing;
* at startup, before any dispatch, every lease of a dead holder is adopted (its
  ``awf-exec`` is still in flight) or released; a lease that cannot be proven
  either way is kept for the reconciler, and only an unqueryable runtime keeps
  all leases and refuses new admission until a check succeeds (fail closed);
* a periodic reconciler releases adopted and cleanup-unproven leases once their
  process is gone, and takes over leases of holders that stopped heartbeating;
* the saturation log is split into work (``worker.work_slots_saturated``) and
  waiting (``worker.work_slot_waiters``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_text
from awf.common.ids import new_work_slot_invocation_id
from awf.common.work_slot import MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
from awf.control.worker.config import effective_worker_config_node_id
from awf.control.worker.constants import (
    _EXECUTION_SLOTS_SATURATED_LOG_INTERVAL,
    WORK_SLOT_ADOPTED,
    WORK_SLOT_RELEASED_BY_RECONCILER,
    WORK_SLOT_STARTUP_CHECK_FAILED,
)
from awf.control.worker.logging import _log
from awf.control.worker.protocols import WorkSlotLeaseProbeResult
from awf.control.worker.resource_broker import (
    WorkSlotLeaseRef,
    _acquire_local_capacity_scheduler_lock,
    acquire_work,
    adopt_work,
    new_work_capacity,
    release_work,
    renew_work,
    transfer_work,
    work_slot_snapshot,
)
from awf.control.worker.work_slot_scope import InvocationWorkSlot, _run_shielded
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import WorkSlotLease
from awf.db.repositories import (
    WorkerHeartbeatRepository,
    WorkSlotLeaseRepository,
    WorkSlotSnapshot,
)
from awf.db.resilience import run_db_operation_with_retry
from awf.service.worker_heartbeat import worker_heartbeat_write_interval_seconds

_DB_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, OSError, TimeoutError)
_PROBE_ERRORS: tuple[type[BaseException], ...] = (OSError, RuntimeError, TimeoutError, ValueError)

_ReconcileAction = Literal["adopt", "renew", "release"]


@dataclass(frozen=True)
class _LeaseRuntime:
    lease: WorkSlotLease
    compose_project: str | None
    compose_file: str | None


# --- Scopes ------------------------------------------------------------------


def _work_slot_admission_open(self: Any) -> bool:
    return not self._work_slot_admission_blocked


def _notify_work_slot_released(self: Any) -> None:
    self._work_slot_wakeups.notify()


def _new_invocation_work_slot(
    self: Any,
    workspace_id: str,
    *,
    lease: WorkSlotLeaseRef | None = None,
) -> InvocationWorkSlot:
    """Build (and track) the per-invocation scope for one dispatched task."""
    scope = InvocationWorkSlot(
        session_factory=self._session_factory,
        config=self._config,
        worker_id=self._worker_id,
        workspace_id=workspace_id,
        wakeups=self._work_slot_wakeups,
        admission_open=self._work_slot_admission_open,
        lease=lease,
        on_retry=self._log_transient_db_retry,
    )
    self._work_slot_scopes[workspace_id] = scope
    return scope


def _forget_work_slot_scope(self: Any, workspace_id: str, scope: InvocationWorkSlot) -> None:
    if self._work_slot_scopes.get(workspace_id) is scope:
        self._work_slot_scopes.pop(workspace_id, None)


@contextlib.asynccontextmanager
async def _work_slot_task_scope(
    self: Any,
    workspace_id: str,
    *,
    lease: WorkSlotLeaseRef | None = None,
) -> AsyncIterator[InvocationWorkSlot | None]:
    """Run one execution task inside its work-slot scope, bound into the executor.

    ``lease`` (READY dispatch) is the lease the dispatch pre-step transferred or
    granted to this worker, handed over directly rather than re-read: a failed
    re-read would leave it held but unrenewed, and the task's own hook reacquire
    queued behind it. The first agent and validation runs are then nested no-ops.
    Every other task kind starts lease-less and reacquires at its hooks.
    Executors without ``work_slot_binding`` (test fakes) simply run unbound.
    """
    if self._executor is None:
        yield None
        return
    scope = self._new_invocation_work_slot(workspace_id, lease=lease)
    binding = getattr(self._executor, "work_slot_binding", None)
    try:
        async with scope:
            with binding(workspace_id, scope) if callable(binding) else contextlib.nullcontext():
                yield scope
    finally:
        self._forget_work_slot_scope(workspace_id, scope)


async def _own_parked_work_slot_lease(self: Any, workspace_id: str) -> WorkSlotLeaseRef | None:
    node_id = effective_worker_config_node_id(self._config)

    async def _operation(session: AsyncSession) -> WorkSlotLeaseRef | None:
        row = await WorkSlotLeaseRepository(session).held_for_workspace(workspace_id)
        if (
            row is None
            or row.holder_worker_id != self._worker_id
            or row.phase != WorkSlotLeasePhase.held.value
            or row.cleanup_unproven
        ):
            return None
        return WorkSlotLeaseRef.from_row(row, node_id=node_id)

    return await _work_slot_db(self, _operation)


async def _start_provision_work_slot(self: Any, workspace_id: str) -> InvocationWorkSlot | None:
    """Scope for a provisioning task, starting on its claim lease (renewed meanwhile).

    A failed lease read propagates: provisioning has no hook to reacquire at, so
    it must not run on a claim lease nobody renews (the reconciler would free the
    slot mid-provision). The caller aborts the attempt before any work.
    """
    if self._executor is None:
        return None
    lease = await self._own_parked_work_slot_lease(workspace_id)
    if lease is None:
        return None
    scope: InvocationWorkSlot = self._new_invocation_work_slot(workspace_id, lease=lease)
    await scope.__aenter__()
    return scope


async def _finish_provision_work_slot(
    self: Any,
    workspace_id: str,
    scope: InvocationWorkSlot | None,
) -> None:
    """Park the claim lease when provisioning ended ``ready``; release it otherwise.

    Runs in the provisioning task's ``finally``, often while a shutdown cancel is
    already propagating. Shielded so a second cancellation landing on the status
    read cannot skip ``close()``/``detach()`` (the lease would stay held with its
    renewal leaked); that cancellation is re-raised once the scope is settled.
    """
    if scope is None:
        return
    try:
        await _run_shielded(_park_or_close_provision_work_slot(self, workspace_id, scope))
    finally:
        self._forget_work_slot_scope(workspace_id, scope)


async def _park_or_close_provision_work_slot(
    self: Any,
    workspace_id: str,
    scope: InvocationWorkSlot,
) -> None:
    try:
        statuses = await self._load_workspace_statuses([workspace_id])
    except _DB_ERRORS as exc:
        _log_work_slot_db_failure("worker.work_slot_provision_status_failed", workspace_id, exc)
        statuses = {}
    if statuses.get(workspace_id) == WorkspaceStatus.ready.value:
        scope.detach()
    else:
        await scope.close()


# --- Admission -------------------------------------------------------------------


async def _new_work_slot_capacity(self: Any, *, reserve_leaseless_ready: bool) -> int:
    """Free work slots new admissions may take (0 while fail-closed or waiters queue)."""
    if self._work_slot_admission_blocked:
        return 0

    async def _operation(session: AsyncSession) -> int:
        return await new_work_capacity(
            session,
            config=self._config,
            now=datetime.now(UTC),
            reserve_leaseless_ready=reserve_leaseless_ready,
        )

    return await _work_slot_db(self, _operation, commit=False)


async def _requested_claim_admission_slots(
    self: Any,
    session: AsyncSession,
    *,
    claim_limit: int,
) -> int:
    """New-work capacity for requested claims, read under the capacity lock.

    Work-slot leases replace the old row-count cap (slot-release): waiting
    monitors and blocked/recovering workspaces hold no lease, live reacquire
    waiters go first, and lease-less ``ready`` rows keep their slots.
    Provision-only workers (no executor) take no leases and claim as before.
    Lock order: requested-admission locks (taken by the caller) -> capacity lock.
    """
    claim_limit = max(0, claim_limit)
    if self._executor is None:
        return claim_limit
    if self._work_slot_admission_blocked:
        return 0
    await _acquire_local_capacity_scheduler_lock(
        session,
        node_id=effective_worker_config_node_id(self._config),
    )
    capacity = await new_work_capacity(
        session,
        config=self._config,
        now=datetime.now(UTC),
        reserve_leaseless_ready=True,
    )
    return min(claim_limit, capacity)


async def _requested_claim_work_slot_refused(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Take the claim lease inside the requested->provisioning claim transaction.

    Returns ``True`` when the lease was refused (the caller rolls the claim back).
    Provision-only workers (no executor) take no leases and are never refused.
    """
    if self._executor is None:
        return False
    result = await acquire_work(
        session,
        workspace_id=workspace_id,
        invocation_id=new_work_slot_invocation_id(),
        holder_worker_id=self._worker_id,
        config=self._config,
        now=datetime.now(UTC),
        lease_seconds=float(self._config.execution_claim_lease_seconds),
        admission="new",
        allow_grant=self._work_slot_admission_open(),
        reserve_leaseless_ready=True,
    )
    return not result.granted


async def _list_parked_ready_work_slot_ids(self: Any, *, exclude_ids: set[str]) -> list[str]:
    """READY rows whose provisioning lease this worker parked for continuation."""

    async def _operation(session: AsyncSession) -> list[str]:
        return await WorkSlotLeaseRepository(session).list_parked_workspace_ids(
            holder_worker_id=self._worker_id,
            workspace_status=WorkspaceStatus.ready,
        )

    parked = await _work_slot_db(self, _operation, commit=False)
    return [workspace_id for workspace_id in parked if workspace_id not in exclude_ids]


async def _claim_ready_work_slots(
    self: Any, workspace_ids: list[str]
) -> dict[str, WorkSlotLeaseRef]:
    """Give each READY candidate a lease before dispatch; refuse the rest this cycle.

    A parked claim lease is transferred to this worker (same invocation, epoch+1);
    a lease-less row takes a fresh ``new`` admission, which never jumps a live
    reacquire waiter. READY dispatch therefore stays bounded by work slots.
    """
    if not workspace_ids or self._work_slot_admission_blocked:
        return {}
    node_id = effective_worker_config_node_id(self._config)
    lease_seconds = float(self._config.execution_claim_lease_seconds)

    async def _operation(session: AsyncSession) -> dict[str, WorkSlotLeaseRef]:
        await _acquire_local_capacity_scheduler_lock(session, node_id=node_id)
        now = datetime.now(UTC)
        fresh = frozenset(await WorkerHeartbeatRepository(session).list_fresh_worker_ids(now=now))
        repo = WorkSlotLeaseRepository(session)
        granted: dict[str, WorkSlotLeaseRef] = {}
        for workspace_id in workspace_ids:
            transferred = await transfer_work(
                session,
                workspace_id=workspace_id,
                holder_worker_id=self._worker_id,
                now=now,
                lease_seconds=lease_seconds,
                fresh_worker_ids=fresh,
                node_id=node_id,
            )
            if transferred is not None:
                granted[workspace_id] = transferred
                continue
            if await repo.held_for_workspace(workspace_id) is not None:
                # Held by a live worker, adopted, or cleanup-unproven: not ours.
                continue
            result = await acquire_work(
                session,
                workspace_id=workspace_id,
                invocation_id=new_work_slot_invocation_id(),
                holder_worker_id=self._worker_id,
                config=self._config,
                now=now,
                lease_seconds=lease_seconds,
                admission="new",
            )
            if result.lease is not None:
                granted[workspace_id] = result.lease
        return granted

    return await _work_slot_db(self, _operation)


# --- Startup check and reconciler ----------------------------------------------


async def _ensure_work_slot_startup_check(self: Any) -> None:
    """Adopt or release every dead holder's lease before any dispatch (R5.1/R5.2).

    Runs on every ``run_once`` until it succeeds once (at most once per heartbeat
    interval). Leases held by another *fresh* worker are left alone. A lease whose
    process cannot be proven either way (``unknown``) is kept for the reconciler
    to re-probe every pass; it never stalls the node. Only a runtime that cannot
    be queried at all (``unavailable``) aborts the pass with every lease kept, and
    the worker refuses new admission until a later pass succeeds.
    """
    if self._work_slot_startup_checked or self._executor is None:
        return
    now_monotonic = monotonic()
    if now_monotonic < self._next_work_slot_startup_check_at:
        return
    self._next_work_slot_startup_check_at = now_monotonic + _work_slot_reconcile_interval(self)
    try:
        leases, fresh = await self._load_work_slot_leases_for_reconcile()
    except _DB_ERRORS as exc:
        _fail_work_slot_startup_check(self, cause="lease_read_failed", error=exc)
        return
    decisions: list[tuple[_LeaseRuntime, _ReconcileAction]] = []
    kept_unknown = 0
    for item in leases:
        holder = item.lease.holder_worker_id
        if holder == self._worker_id or holder in fresh:
            continue
        if item.lease.acquired_at is None:
            # A dead holder's queued ticket: withdraw it.
            decisions.append((item, "release"))
            continue
        outcome = await self._probe_work_slot_lease(item)
        if outcome == "unavailable":
            _fail_work_slot_startup_check(
                self,
                cause="runtime_unavailable",
                workspace_id=item.lease.workspace_id,
                invocation_id=item.lease.invocation_id,
            )
            return
        if outcome == "unknown":
            kept_unknown += 1
            _log.warning(
                "worker.work_slot_startup_lease_kept",
                worker_id=self._worker_id,
                workspace_id=item.lease.workspace_id,
                invocation_id=item.lease.invocation_id,
                holder_worker_id=holder,
            )
            continue
        decisions.append((item, "adopt" if outcome == "present" else "release"))
    for item, action in decisions:
        await self._apply_work_slot_reconcile_action(item, action, source="startup")
    was_blocked = self._work_slot_admission_blocked
    self._work_slot_startup_checked = True
    self._work_slot_admission_blocked = False
    _log.info(
        "worker.work_slot_startup_check_completed",
        worker_id=self._worker_id,
        checked=len(decisions) + kept_unknown,
        adopted=sum(1 for _, action in decisions if action == "adopt"),
        released=sum(1 for _, action in decisions if action == "release"),
        kept_unknown=kept_unknown,
    )
    if decisions or was_blocked:
        self._notify_work_slot_released()


def _fail_work_slot_startup_check(
    self: Any,
    *,
    cause: str,
    error: BaseException | None = None,
    workspace_id: str | None = None,
    invocation_id: str | None = None,
) -> None:
    self._work_slot_admission_blocked = True
    _log.warning(
        "worker.work_slot_startup_check_failed",
        worker_id=self._worker_id,
        cause=cause,
        workspace_id=workspace_id,
        invocation_id=invocation_id,
        error_type=type(error).__name__ if error is not None else None,
        error=redact_audit_text(str(error))[:240] if error is not None else None,
        reason_code=WORK_SLOT_STARTUP_CHECK_FAILED,
    )


async def _maybe_reconcile_work_slot_leases(self: Any) -> None:
    """Periodic reconciler (every heartbeat interval, R5.3).

    Probes this worker's adopted, cleanup-unproven and expired untracked leases
    plus every lease whose holder stopped heartbeating: ``absent`` releases (and
    wakes waiters), ``present`` renews ours or adopts theirs, ``unknown`` and
    ``unavailable`` keep (and renew ours). Queued tickets whose holder stopped
    heartbeating are withdrawn at once; expired waiting/unsatisfiable tickets are
    deleted.
    """
    if self._executor is None or not self._work_slot_startup_checked:
        return
    now_monotonic = monotonic()
    if now_monotonic < self._next_work_slot_reconcile_at:
        return
    self._next_work_slot_reconcile_at = now_monotonic + _work_slot_reconcile_interval(self)
    try:
        leases, fresh = await self._load_work_slot_leases_for_reconcile()
    except _DB_ERRORS as exc:
        _log_work_slot_db_failure("worker.work_slot_reconcile_read_failed", None, exc)
        return
    now = datetime.now(UTC)
    tracked = {
        scope.lease.invocation_id
        for scope in self._work_slot_scopes.values()
        if scope.lease is not None and scope.tracks_lease
    }
    released = False
    for item in leases:
        lease = item.lease
        if lease.acquired_at is None:
            if lease.holder_worker_id != self._worker_id and lease.holder_worker_id not in fresh:
                # A crashed holder's ticket would head the FIFO and zero new-work
                # capacity until it expires; nobody re-checks it, so withdraw it.
                released |= await self._apply_work_slot_reconcile_action(
                    item, "release", source="reconciler"
                )
            continue
        own = lease.holder_worker_id == self._worker_id
        if own:
            if lease.invocation_id in tracked:
                continue
            if not (
                lease.phase == WorkSlotLeasePhase.adopted.value
                or lease.cleanup_unproven
                or lease.expires_at <= now
            ):
                continue
        elif lease.holder_worker_id in fresh:
            continue
        outcome = await self._probe_work_slot_lease(item)
        action: _ReconcileAction | None
        if outcome == "absent":
            action = "release"
        elif own:
            action = "renew"
        else:
            action = "adopt" if outcome == "present" else None
        if action is not None:
            released |= await self._apply_work_slot_reconcile_action(
                item, action, source="reconciler"
            )
    try:
        expired = await _work_slot_db(
            self,
            lambda session: WorkSlotLeaseRepository(session).delete_expired_waiters(
                node_id=effective_worker_config_node_id(self._config), now=now
            ),
        )
    except _DB_ERRORS as exc:
        _log_work_slot_db_failure("worker.work_slot_expired_waiter_cleanup_failed", None, exc)
        expired = []
    if released or expired:
        self._notify_work_slot_released()


async def _load_work_slot_leases_for_reconcile(
    self: Any,
) -> tuple[list[_LeaseRuntime], frozenset[str]]:
    node_id = effective_worker_config_node_id(self._config)

    async def _operation(session: AsyncSession) -> tuple[list[_LeaseRuntime], frozenset[str]]:
        rows = await WorkSlotLeaseRepository(session).list_with_runtime_for_node(node_id=node_id)
        fresh = await WorkerHeartbeatRepository(session).list_fresh_worker_ids(
            now=datetime.now(UTC)
        )
        return [_LeaseRuntime(*row) for row in rows], frozenset(fresh)

    return await _work_slot_db(self, _operation, commit=False)


async def _probe_work_slot_lease(self: Any, item: _LeaseRuntime) -> WorkSlotLeaseProbeResult:
    probe = self._work_slot_probe
    if probe is None:
        # No process probe configured (e.g. provision tests): nothing in flight
        # can be proven, so this live worker reclaims the slot.
        return "absent"
    try:
        result: WorkSlotLeaseProbeResult = await probe.probe(
            workspace_id=item.lease.workspace_id,
            invocation_id=item.lease.invocation_id,
            compose_project=item.compose_project,
            compose_file=Path(item.compose_file) if item.compose_file else None,
        )
    except _PROBE_ERRORS as exc:
        _log.warning(
            "worker.work_slot_probe_failed",
            workspace_id=item.lease.workspace_id,
            invocation_id=item.lease.invocation_id,
            error_type=type(exc).__name__,
            error=redact_audit_text(str(exc))[:240],
        )
        return "unknown"
    return result


async def _apply_work_slot_reconcile_action(
    self: Any,
    item: _LeaseRuntime,
    action: _ReconcileAction,
    *,
    source: str,
) -> bool:
    """Apply one fenced adopt/renew/release; ``True`` when a slot or ticket was freed."""
    node_id = effective_worker_config_node_id(self._config)
    lease = WorkSlotLeaseRef.from_row(item.lease, node_id=node_id)
    lease_seconds = float(self._config.execution_claim_lease_seconds)

    async def _operation(session: AsyncSession) -> bool:
        now = datetime.now(UTC)
        if action == "adopt":
            adopted = await adopt_work(
                session,
                lease=lease,
                holder_worker_id=self._worker_id,
                now=now,
                lease_seconds=lease_seconds,
            )
            return adopted is not None
        if action == "renew":
            return await renew_work(session, lease=lease, now=now, lease_seconds=lease_seconds)
        return await release_work(session, lease=lease, now=now)

    try:
        applied = await _work_slot_db(self, _operation)
    except _DB_ERRORS as exc:
        _log_work_slot_db_failure(
            "worker.work_slot_reconcile_write_failed", lease.workspace_id, exc
        )
        return False
    if applied and action != "renew":
        _log.info(
            "worker.work_slot_adopted" if action == "adopt" else "worker.work_slot_released",
            workspace_id=lease.workspace_id,
            invocation_id=lease.invocation_id,
            previous_holder_worker_id=lease.holder_worker_id,
            worker_id=self._worker_id,
            phase=item.lease.phase,
            cleanup_unproven=item.lease.cleanup_unproven,
            source=source,
            reason_code=(
                WORK_SLOT_ADOPTED if action == "adopt" else WORK_SLOT_RELEASED_BY_RECONCILER
            ),
        )
    return applied and action == "release"


def _work_slot_reconcile_interval(self: Any) -> float:
    return worker_heartbeat_write_interval_seconds(self._config.poll_interval_seconds)


# --- Visibility --------------------------------------------------------------------


async def _work_slot_snapshot(self: Any) -> WorkSlotSnapshot:
    async def _operation(session: AsyncSession) -> WorkSlotSnapshot:
        return await work_slot_snapshot(session, config=self._config, now=datetime.now(UTC))

    return await _work_slot_db(self, _operation, commit=False)


async def _update_work_slot_saturation(self: Any, *, dispatched: int) -> None:
    """Emit the split work-slot saturation logs at the saturation cadence (R3.7).

    ``worker.work_slots_saturated``: every slot is held and nothing dispatched
    this cycle (holders listed). ``worker.work_slot_waiters``: invocations are
    queued for a slot (reason ``MONITOR_WAITING_FOR_WORK_SLOT``).
    """
    if not self._can_dispatch_execution_when_slot_available():
        self._consecutive_saturated_cycles = 0
        self._consecutive_work_slot_waiting_cycles = 0
        return
    try:
        snapshot = await self._work_slot_snapshot()
    except _DB_ERRORS as exc:
        _log_work_slot_db_failure("worker.work_slot_snapshot_failed", None, exc)
        return
    saturated = dispatched == 0 and snapshot.in_use >= snapshot.limit
    if saturated:
        self._consecutive_saturated_cycles += 1
        if self._consecutive_saturated_cycles % _EXECUTION_SLOTS_SATURATED_LOG_INTERVAL == 0:
            _log.warning(
                "worker.work_slots_saturated",
                slot_limit=snapshot.limit,
                in_use=snapshot.in_use,
                waiting=snapshot.waiting,
                holders=[
                    {
                        "workspace_id": holder.workspace_id,
                        "phase": holder.phase,
                        "cleanup_unproven": holder.cleanup_unproven,
                    }
                    for holder in snapshot.holders()
                ],
                tracked_draining_ids=sorted(self._tracked_draining_workspace_ids()),
                consecutive_saturated_cycles=self._consecutive_saturated_cycles,
            )
    else:
        self._consecutive_saturated_cycles = 0
    if snapshot.waiting or snapshot.unsatisfiable:
        self._consecutive_work_slot_waiting_cycles += 1
        if (
            self._consecutive_work_slot_waiting_cycles % _EXECUTION_SLOTS_SATURATED_LOG_INTERVAL
            == 0
        ):
            oldest = snapshot.oldest_ticket_at
            _log.warning(
                "worker.work_slot_waiters",
                slot_limit=snapshot.limit,
                in_use=snapshot.in_use,
                waiting_workspace_ids=snapshot.waiting_workspace_ids(),
                unsatisfiable_workspace_ids=snapshot.unsatisfiable_workspace_ids(),
                oldest_wait_seconds=(
                    round((datetime.now(UTC) - oldest).total_seconds(), 3)
                    if oldest is not None
                    else None
                ),
                tracked_monitor_ids_without_lease=sorted(
                    workspace_id
                    for workspace_id in self._tracked_monitor_workspace_ids()
                    if self._work_slot_scopes.get(workspace_id) is None
                    or self._work_slot_scopes[workspace_id].lease is None
                ),
                consecutive_waiting_cycles=self._consecutive_work_slot_waiting_cycles,
                reason_code=MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE,
            )
    else:
        self._consecutive_work_slot_waiting_cycles = 0


# --- Helpers -------------------------------------------------------------------------


async def _work_slot_db[T](
    self: Any,
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    commit: bool = True,
) -> T:
    # Lease writes are fenced and idempotent, so replaying an ambiguous commit is safe.
    return await run_db_operation_with_retry(
        self._session_factory,
        operation,
        commit=commit,
        retry_commit_failures=commit,
        on_retry=self._log_transient_db_retry,
    )


def _log_work_slot_db_failure(event: str, workspace_id: str | None, exc: BaseException) -> None:
    _log.warning(
        event,
        workspace_id=workspace_id,
        error_type=type(exc).__name__,
        error=redact_audit_text(str(exc))[:240],
    )
