"""Per-invocation work-slot scope injected into agent and validation runs (slot-release).

The worker builds one :class:`InvocationWorkSlot` per dispatched task and hands it
to the executor, which injects it into ``AgentAdapter.run()`` and the
``ValidationRunner``. It is the "async context manager injected by callers", not
a gate: it owns no admission logic, count, lock or queue. Every decision is the
resource broker's (``resource_broker.acquire_work``/``release_work``/
``renew_work``), taken in a short transaction under the capacity advisory lock;
this scope only waits between re-checks, renews the lease it holds, and decides
when the invocation is done with it.

Lifecycle:

* ``hold(phase)`` acquires (FIFO ``reacquire`` class) unless this invocation
  already holds a lease, in which case it is a nested no-op. A different
  invocation never shares this one's lease.
* A worker scope *retains* its lease across hold exits so a fix cycle (agent ->
  settle -> validation -> push) keeps one slot; the lease is released at the
  first pure wait (``release_idle()``) or when the task ends (``close()``).
* A run that ends with ``ComposeExecCleanupError`` anywhere in its exception
  chain keeps the lease with ``cleanup_unproven``; the worker's reconciler
  releases it only once no tagged ``awf-exec`` remains. A later hold (a recovery
  retry) leaves it there and waits for a fresh lease under a new invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from types import TracebackType
from typing import Any, TypeVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.ids import new_work_slot_invocation_id
from awf.common.work_slot import MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
from awf.control.worker.config import WorkerConfig, effective_worker_config_node_id
from awf.control.worker.constants import (
    WORK_SLOT_CLEANUP_UNPROVEN_RETAINED,
    WORK_SLOT_LEASE_FENCED,
)
from awf.control.worker.logging import _log
from awf.control.worker.resource_broker import (
    WorkSlotAcquireOutcome,
    WorkSlotAcquireResult,
    WorkSlotLeaseRef,
    _capacity_blocker_payload,
    acquire_work,
    release_work,
    renew_work,
)
from awf.db.repositories import WorkspaceRepository
from awf.db.resilience import run_db_operation_with_retry

_T = TypeVar("_T")

_WORK_SLOT_WAIT_RECHECK_SECONDS = 10.0
"""DB re-check fallback for waiters; releases by this worker wake them sooner."""

WORK_SLOT_WAITING_EVENT_TYPE = "workspace.work_slot_waiting"
"""Workspace event written once per wait episode (reason MONITOR_WAITING_FOR_WORK_SLOT)."""

_DB_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, OSError, TimeoutError)


class WorkSlotWakeups:
    """In-process release notifications owned by one worker instance.

    Waiters grab the current event *before* re-checking the DB, so a release that
    lands between the re-check and the wait still wakes them. ``notify`` swaps in
    a fresh event so later waiters never see a stale set event.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def waiter(self) -> asyncio.Event:
        return self._event

    def notify(self) -> None:
        event = self._event
        self._event = asyncio.Event()
        event.set()


class InvocationWorkSlot:
    """Work-slot scope for one invocation (see module docstring)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: WorkerConfig,
        worker_id: str,
        workspace_id: str,
        wakeups: WorkSlotWakeups,
        admission_open: Callable[[], bool] = lambda: True,
        lease: WorkSlotLeaseRef | None = None,
        invocation_id: str | None = None,
        retain: bool = True,
        on_retry: Callable[[BaseException, int], Awaitable[None]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._worker_id = worker_id
        self._workspace_id = workspace_id
        self._wakeups = wakeups
        self._admission_open = admission_open
        self._lease = lease
        self._invocation_id = (
            lease.invocation_id
            if lease is not None
            else invocation_id or new_work_slot_invocation_id()
        )
        self._retain = retain
        self._on_retry = on_retry
        self._depth = 0
        self._cleanup_unproven = False
        self._cleanup_flag_pending = False
        self._renewal: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def invocation_id(self) -> str:
        return self._invocation_id

    @property
    def lease(self) -> WorkSlotLeaseRef | None:
        return self._lease

    @property
    def cleanup_unproven(self) -> bool:
        return self._cleanup_unproven

    async def __aenter__(self) -> InvocationWorkSlot:
        if self._lease is not None:
            self._start_renewal()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @contextlib.asynccontextmanager
    async def hold(self, phase: str) -> AsyncIterator[InvocationWorkSlot]:
        """Hold a work slot for one agent/validation run (nested holds are no-ops)."""
        if self._depth == 0:
            if self._cleanup_unproven:
                await self._leave_unproven_lease_to_reconciler()
            if await self._still_holding():
                # A failed idle release keeps the lease but stopped renewing it.
                self._start_renewal()
            else:
                await self._acquire_until_granted(phase)
        self._depth += 1
        try:
            yield self
        except BaseException as exc:
            if _cleanup_unproven_in_chain(exc):
                await _run_shielded(self._retain_cleanup_unproven(phase))
            raise
        finally:
            self._depth -= 1
            if self._depth == 0 and not self._retain:
                await _run_shielded(self._release_lease(reason="hold_exit"))

    async def release_idle(self) -> None:
        """Release a retained lease at a pure wait (no hold active).

        Called by the PR monitor before every non-work action (WaitForCI,
        NotifyHuman, ...). A cleanup-unproven lease is never released here.
        """
        if self._depth > 0 or self._cleanup_unproven:
            return
        await self._release_lease(reason="idle")

    async def close(self) -> None:
        """End of the worker task: release, or hand an unproven lease to the reconciler."""
        if self._closed:
            return
        self._closed = True
        await self._stop_renewal()
        if self._cleanup_unproven:
            # Holder stays this worker and the row is kept; the periodic
            # reconciler releases it once no tagged ``awf-exec`` remains.
            return
        await _run_shielded(self._release_lease(reason="close"))

    def detach(self) -> WorkSlotLeaseRef | None:
        """Stop renewing and keep the lease parked (provisioning ended ``ready``)."""
        lease = self._lease
        self._lease = None
        self._cancel_renewal()
        self._closed = True
        return lease

    async def abandon_to_reconciler(self) -> None:
        """Hand a wedged task's lease to the reconciler (#276 draining monitors)."""
        if self._lease is None:
            return
        self._cancel_renewal()
        await self._retain_cleanup_unproven("abandoned")

    async def _leave_unproven_lease_to_reconciler(self) -> None:
        """Never run a retry (agent-service recovery) on a cleanup-unproven lease.

        The reconciler does not track that lease and may release it once the old
        ``awf-exec`` is gone, before the retry spawns its own. The retry instead
        waits under a fresh invocation; the broker grants it only after the
        flagged row was reconciled away (one held lease per workspace).
        """
        self._lease = None
        await self._stop_renewal()
        self._invocation_id = new_work_slot_invocation_id()

    async def _still_holding(self) -> bool:
        lease = self._lease
        if lease is None:
            return False
        try:
            renewed = await self._db(
                lambda session: renew_work(
                    session,
                    lease=lease,
                    now=datetime.now(UTC),
                    lease_seconds=self._lease_seconds,
                ),
            )
        except _DB_ERRORS as exc:
            # Cannot confirm; keep running on the lease we believe we hold rather
            # than queueing behind ourselves. The renewal loop re-checks.
            _log.warning(
                "worker.work_slot_hold_check_failed",
                workspace_id=self._workspace_id,
                invocation_id=self._invocation_id,
                worker_id=self._worker_id,
                error_type=type(exc).__name__,
                error=redact_audit_text(str(exc))[:240],
            )
            return True
        if not renewed:
            self._fenced(lease, phase="hold")
        return renewed

    async def _acquire_until_granted(self, phase: str) -> None:
        try:
            await self._wait_for_grant(phase)
        except BaseException:
            # Any abort (cancel, or a re-check DB error) withdraws the persisted
            # ticket: a dead row would stay the FIFO head until it expires.
            await _run_shielded(self._withdraw_ticket())
            raise

    async def _wait_for_grant(self, phase: str) -> None:
        waited_since: float | None = None
        while True:
            waiter = self._wakeups.waiter()
            result = await self._try_acquire()
            if result.granted:
                self._lease = result.lease
                # An unproven lease we no longer hold was reconciled (or adopted)
                # away; the fresh lease is ordinary and must release at idle.
                self._cleanup_unproven = False
                self._cleanup_flag_pending = False
                self._start_renewal()
                self._log_granted(phase, waited_since)
                return
            if result.outcome is WorkSlotAcquireOutcome.refused:
                # Another worker adopted this invocation's lease while we were
                # considered dead; continue under a fresh invocation.
                self._invocation_id = new_work_slot_invocation_id()
            else:
                waited_since = await self._wait_for_release(
                    phase, result, waiter, waited_since=waited_since
                )

    async def _wait_for_release(
        self,
        phase: str,
        result: WorkSlotAcquireResult,
        waiter: asyncio.Event,
        *,
        waited_since: float | None,
    ) -> float:
        """Record the wait episode once, then sleep until a release or the re-check."""
        if waited_since is None:
            waited_since = monotonic()
            await self._record_waiting(phase, result)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(waiter.wait(), timeout=_WORK_SLOT_WAIT_RECHECK_SECONDS)
        return waited_since

    def _log_granted(self, phase: str, waited_since: float | None) -> None:
        if waited_since is None:
            return
        _log.info(
            "worker.work_slot_granted",
            workspace_id=self._workspace_id,
            invocation_id=self._invocation_id,
            worker_id=self._worker_id,
            phase=phase,
            waited_seconds=round(monotonic() - waited_since, 3),
        )

    async def _try_acquire(self) -> WorkSlotAcquireResult:
        invocation_id = self._invocation_id
        allow_grant = self._admission_open()
        return await self._db(
            lambda session: acquire_work(
                session,
                workspace_id=self._workspace_id,
                invocation_id=invocation_id,
                holder_worker_id=self._worker_id,
                config=self._config,
                now=datetime.now(UTC),
                lease_seconds=self._lease_seconds,
                admission="reacquire",
                allow_grant=allow_grant,
            ),
        )

    async def _record_waiting(self, phase: str, result: WorkSlotAcquireResult) -> None:
        payload: dict[str, Any] = {
            "phase": phase,
            "invocation_id": self._invocation_id,
            "worker_id": self._worker_id,
            "outcome": result.outcome.value,
            "in_use": result.in_use,
            "limit": result.limit,
            "ticket_at": result.ticket_at.isoformat() if result.ticket_at else None,
            "blockers": [_capacity_blocker_payload(blocker) for blocker in result.blockers],
        }
        _log.info(
            "worker.work_slot_waiting",
            workspace_id=self._workspace_id,
            reason_code=MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE,
            **payload,
        )

        async def _operation(session: AsyncSession) -> None:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(self._workspace_id)
            if workspace is None:
                return
            await repo.add_event(
                workspace,
                event_type=WORK_SLOT_WAITING_EVENT_TYPE,
                reason_code=MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE,
                payload=payload,
            )

        try:
            await self._db(_operation, retry_commit_failures=False)
        except _DB_ERRORS as exc:
            # The wait itself is unaffected; the structured log above keeps the
            # reason code even when the timeline write fails.
            _log.warning(
                "worker.work_slot_waiting_event_failed",
                workspace_id=self._workspace_id,
                reason_code=MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE,
                error_type=type(exc).__name__,
                error=redact_audit_text(str(exc))[:240],
            )

    async def _withdraw_ticket(self) -> None:
        ticket = WorkSlotLeaseRef(
            invocation_id=self._invocation_id,
            workspace_id=self._workspace_id,
            holder_worker_id=self._worker_id,
            holder_epoch=1,
            node_id=effective_worker_config_node_id(self._config),
        )
        try:
            await self._db(
                lambda session: release_work(session, lease=ticket, now=datetime.now(UTC)),
            )
        except _DB_ERRORS as exc:
            # An orphaned waiting row expires on its own and stops blocking the
            # FIFO head; the reconciler deletes it.
            _log.warning(
                "worker.work_slot_ticket_withdraw_failed",
                workspace_id=self._workspace_id,
                invocation_id=self._invocation_id,
                error_type=type(exc).__name__,
                error=redact_audit_text(str(exc))[:240],
            )

    async def _retain_cleanup_unproven(self, phase: str) -> None:
        lease = self._lease
        if lease is None:
            return
        self._cleanup_unproven = True
        _log.warning(
            "worker.work_slot_cleanup_unproven_retained",
            workspace_id=self._workspace_id,
            invocation_id=lease.invocation_id,
            worker_id=self._worker_id,
            phase=phase,
            reason_code=WORK_SLOT_CLEANUP_UNPROVEN_RETAINED,
        )
        await self._flag_cleanup_unproven(lease, phase=phase)

    async def _flag_cleanup_unproven(self, lease: WorkSlotLeaseRef, *, phase: str) -> bool:
        """Persist ``cleanup_unproven`` on the held row; ``False`` means we were fenced."""
        try:
            flagged = await self._db(
                lambda session: release_work(
                    session,
                    lease=lease,
                    now=datetime.now(UTC),
                    cleanup_unproven=True,
                ),
            )
        except _DB_ERRORS as exc:
            # The row stays held but unflagged, and the reconciler skips an own
            # lease we keep renewing: the renewal loop retries the write. A
            # stopped renewal lets the row expire into the reconciler's probe.
            self._cleanup_flag_pending = True
            _log.warning(
                "worker.work_slot_cleanup_unproven_flag_failed",
                workspace_id=self._workspace_id,
                invocation_id=lease.invocation_id,
                error_type=type(exc).__name__,
                error=redact_audit_text(str(exc))[:240],
            )
            return True
        self._cleanup_flag_pending = False
        if not flagged:
            self._fenced(lease, phase=phase)
        return flagged

    async def _release_lease(self, *, reason: str) -> None:
        lease = self._lease
        if lease is None or self._cleanup_unproven:
            return
        self._lease = None
        await self._stop_renewal()
        try:
            released = await self._db(
                lambda session: release_work(session, lease=lease, now=datetime.now(UTC)),
            )
        except _DB_ERRORS as exc:
            # The row is still held; never pretend the slot was freed. An open
            # scope keeps the ref so the next pure wait (or close) retries the
            # delete; a closed one leaves the untracked row to the reconciler.
            if not self._closed:
                self._lease = lease
            _log.warning(
                "worker.work_slot_release_failed",
                workspace_id=self._workspace_id,
                invocation_id=lease.invocation_id,
                worker_id=self._worker_id,
                error_type=type(exc).__name__,
                error=redact_audit_text(str(exc))[:240],
            )
            return
        _log.info(
            "worker.work_slot_released",
            workspace_id=self._workspace_id,
            invocation_id=lease.invocation_id,
            worker_id=self._worker_id,
            reason=reason,
            released=released,
        )
        self._wakeups.notify()

    def _start_renewal(self) -> None:
        if self._renewal is None or self._renewal.done():
            self._renewal = asyncio.create_task(
                self._renew_loop(),
                name=f"awf-work-slot-renew-{self._workspace_id}",
            )

    def _cancel_renewal(self) -> None:
        if self._renewal is not None:
            self._renewal.cancel()

    async def _stop_renewal(self) -> None:
        renewal = self._renewal
        self._renewal = None
        if renewal is None:
            return
        renewal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewal

    async def _renew_loop(self) -> None:
        interval = max(1.0, min(60.0, self._lease_seconds / 3))
        renewing = True
        while renewing:
            await asyncio.sleep(interval)
            lease = self._lease
            # Stop once the lease is gone (released or fenced) or renewal is fenced.
            renewing = lease is not None and await self._renew_once(lease)

    async def _renew_once(self, lease: WorkSlotLeaseRef) -> bool:
        if self._cleanup_flag_pending:
            # Land the flag first so the reconciler probes this row; the next
            # tick (well inside the lease) renews it again.
            return await self._flag_cleanup_unproven(lease, phase="renew")
        try:
            renewed = await self._db(
                lambda session: renew_work(
                    session,
                    lease=lease,
                    now=datetime.now(UTC),
                    lease_seconds=self._lease_seconds,
                ),
            )
        except _DB_ERRORS as exc:
            _log.warning(
                "worker.work_slot_renew_failed",
                workspace_id=self._workspace_id,
                invocation_id=lease.invocation_id,
                error_type=type(exc).__name__,
                error=redact_audit_text(str(exc))[:240],
            )
            return True
        if not renewed:
            self._fenced(lease, phase="renew")
        return renewed

    def _fenced(self, lease: WorkSlotLeaseRef, *, phase: str) -> None:
        # Drop the ref without deleting: a newer holder/epoch owns the row now.
        # The running work is not cancelled; its next hold reacquires.
        _log.warning(
            "worker.work_slot_lease_fenced",
            workspace_id=self._workspace_id,
            invocation_id=lease.invocation_id,
            worker_id=self._worker_id,
            holder_epoch=lease.holder_epoch,
            phase=phase,
            reason_code=WORK_SLOT_LEASE_FENCED,
        )
        if self._lease == lease:
            self._lease = None

    @property
    def _lease_seconds(self) -> float:
        return float(self._config.execution_claim_lease_seconds)

    async def _db(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
        *,
        retry_commit_failures: bool = True,
    ) -> _T:
        # Lease writes are idempotent per invocation (owner + epoch fenced), so
        # replaying after an ambiguous commit boundary is safe.
        return await run_db_operation_with_retry(
            self._session_factory,
            operation,
            commit=True,
            retry_commit_failures=retry_commit_failures,
            on_retry=self._on_retry,
        )


def _cleanup_unproven_in_chain(exc: BaseException) -> bool:
    """Whether ``exc`` (or anything in its cause/context chain) is a cleanup failure."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ComposeExecCleanupError):
            return True
        pending.extend(linked for linked in (current.__cause__, current.__context__) if linked)
    return False


async def _run_shielded(awaitable: Awaitable[None]) -> None:
    """Run a lease write to completion even if the caller is cancelled meanwhile.

    A cancellation that lands mid-write is re-raised once the write finished, so
    cooperative cancellation is delayed, never swallowed.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError
