"""Control worker dispatch operations.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from functools import partial
from typing import Any, cast

from awf.control.worker.constants import (
    _EXECUTION_SLOTS_SATURATED_LOG_INTERVAL,
    EXECUTION_CLAIM_FENCED,
)
from awf.control.worker.logging import _log
from awf.control.worker.types import _ExecutionTaskKind
from awf.db.enums import (
    OperationStatus,
    WorkspaceStatus,
)


def _draining_execution_task_count(self: Any) -> int:
    return sum(
        1
        for kind in self._execution_task_kinds.values()
        if kind is _ExecutionTaskKind.MONITOR_DRAINING
    )


def _available_execution_slots(self: Any) -> int:
    # Draining tasks (cancelled monitors not yet stopped) stay tracked for
    # same-workspace dedup but are excluded from the slot budget so a wedged
    # monitor does not keep starving other workspaces (issue #276).
    #
    # That exclusion is capped, though: cancel() is cooperative, so a steady
    # supply of stale-and-wedged monitors could otherwise accumulate draining
    # coroutines without bound and let dispatch run arbitrarily far past the
    # budget, exhausting runtime resources. Excluding at most
    # max_concurrent_executions draining tasks bounds total in-flight
    # coroutines at 2x the budget; beyond that, surplus draining tasks count
    # as occupied and throttle fresh dispatch until they truly stop.
    max_executions = self._config.max_concurrent_executions
    excluded_draining = min(self._draining_execution_task_count(), max_executions)
    occupied = len(self._execution_tasks) - excluded_draining
    return cast(int, max(0, max_executions - occupied))


def _can_dispatch_execution_when_slot_available(self: Any) -> bool:
    return self._executor is not None and self._config.max_concurrent_executions > 0


def _preserved_active_validation_can_continue(self: Any, workspace_id: str) -> bool:
    return self._executor is not None and (
        workspace_id in self._execution_tasks or self._can_dispatch_execution_when_slot_available()
    )


def _dispatchable_execution_ids(self: Any, workspace_ids: list[str], *, limit: int) -> list[str]:
    dispatchable: list[str] = []
    for workspace_id in workspace_ids:
        if len(dispatchable) >= limit:
            break
        if workspace_id in self._execution_tasks:
            continue
        dispatchable.append(workspace_id)
    return dispatchable


def _dispatch_ready_executions(self: Any, workspace_ids: list[str], *, limit: int) -> set[str]:
    dispatched: set[str] = set()
    for workspace_id in workspace_ids:
        if len(dispatched) >= limit:
            break
        if workspace_id in self._execution_tasks:
            continue

        task = asyncio.create_task(
            self._safely_execute_claimed(workspace_id),
            name=f"awf-execute-{workspace_id}",
        )
        self._track_execution_task(workspace_id, task, kind=_ExecutionTaskKind.READY)
        dispatched.add(workspace_id)
    return dispatched


def _dispatch_monitor_resumes(self: Any, workspace_ids: list[str], *, limit: int) -> set[str]:
    dispatched: set[str] = set()
    for workspace_id in workspace_ids:
        if len(dispatched) >= limit:
            break
        if workspace_id in self._execution_tasks:
            continue

        recovery_operation_id = self._monitor_recovery_operation_ids.get(workspace_id)
        task = asyncio.create_task(
            self._safely_resume_claimed_pr_monitor(
                workspace_id,
                recovery_operation_id=recovery_operation_id,
            ),
            name=f"awf-monitor-{workspace_id}",
        )
        self._track_execution_task(workspace_id, task, kind=_ExecutionTaskKind.MONITOR_RESUME)
        dispatched.add(workspace_id)
    return dispatched


def _dispatch_blocked_resumes(self: Any, workspace_ids: list[str], *, limit: int) -> set[str]:
    dispatched: set[str] = set()
    for workspace_id in workspace_ids:
        if len(dispatched) >= limit:
            break
        if workspace_id in self._execution_tasks:
            continue
        task = asyncio.create_task(
            self._safely_resume_blocked_claimed(workspace_id),
            name=f"awf-blocked-resume-{workspace_id}",
        )
        self._track_execution_task(workspace_id, task, kind=_ExecutionTaskKind.BLOCKED_RESUME)
        dispatched.add(workspace_id)
    return dispatched


async def _safely_resume_blocked_claimed(self: Any, workspace_id: str) -> None:
    """Run a blocked-resume execution under an execution-claim heartbeat.

    Mirrors ``_safely_execute_claimed``: the worker's resume CAS already
    re-acquired the epoch-fenced execution claim and transitioned the row
    ``blocked -> running``, so the executor drives the normal flow in
    ``resume_from_blocked`` mode while this loop keeps the lease warm."""
    heartbeat = asyncio.create_task(
        self._refresh_execution_claim_loop(workspace_id),
        name=f"awf-blocked-resume-claim-{workspace_id}",
    )
    try:
        # The claim CAS already transitioned the row ``blocked -> running``
        # before dispatch, so a missing executor must still fall through to the
        # ``finally`` to release the claim — returning here would strand the
        # claimed ``running`` row until lease expiry. Mirrors ``_safely_execute``.
        if self._executor is None:
            return
        await self._executor.resume_blocked_execution(
            workspace_id,
            execution_owner_id=self._worker_id,
            execution_lease_expires_at=self._execution_claim_expires_at(),
        )
    except Exception:
        _log.exception("worker.resume_blocked_failed", workspace_id=workspace_id)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await self._release_execution_claim(workspace_id)
        await self._release_terminal_runtime_promptly(workspace_id)


def _dispatch_preserved_active_validation(self: Any, workspace_id: str) -> bool:
    if self._executor is None:
        return False
    if self._available_execution_slots() <= 0:
        return False
    if workspace_id in self._execution_tasks:
        return False
    task = asyncio.create_task(
        self._safely_execute_claimed(workspace_id),
        name=f"awf-preserved-active-validate-{workspace_id}",
    )
    self._track_execution_task(workspace_id, task, kind=_ExecutionTaskKind.PRESERVED_ACTIVE)
    return True


def _track_execution_task(
    self: Any,
    workspace_id: str,
    task: asyncio.Task[None],
    *,
    kind: _ExecutionTaskKind,
) -> None:
    self._execution_tasks[workspace_id] = task
    self._execution_task_kinds[workspace_id] = kind
    task.add_done_callback(partial(self._forget_execution_task, workspace_id))


def _forget_execution_task(self: Any, workspace_id: str, task: asyncio.Task[None]) -> None:
    # Identity-guarded: a reconcile-driven cancel can free a slot and re-dispatch
    # the same workspace within one run_once, so only forget the task we tracked.
    if self._execution_tasks.get(workspace_id) is task:
        self._execution_tasks.pop(workspace_id, None)
        self._execution_task_kinds.pop(workspace_id, None)


def _tracked_monitor_workspace_ids(self: Any) -> list[str]:
    return [
        workspace_id
        for workspace_id, kind in self._execution_task_kinds.items()
        if kind is _ExecutionTaskKind.MONITOR_RESUME
    ]


def _tracked_draining_workspace_ids(self: Any) -> list[str]:
    return [
        workspace_id
        for workspace_id, kind in self._execution_task_kinds.items()
        if kind is _ExecutionTaskKind.MONITOR_DRAINING
    ]


async def _reconcile_stale_monitor_execution_tasks(self: Any) -> None:
    """Cancel tracked PR-monitor tasks whose workspace has left ``monitoring_pr``.

    A wedged monitor resume coroutine keeps occupying an execution slot
    forever. Once its workspace row is gone or has transitioned away from
    ``monitoring_pr`` the resume is stale, so we cancel it and reclassify it
    as ``MONITOR_DRAINING``. Reclassifying frees its slot for *other*
    workspaces synchronously in the current ``run_once`` (the slot budget
    excludes draining tasks) while keeping the task tracked under its
    workspace_id. ``cancel()`` is cooperative, so the coroutine can keep
    running afterwards; retaining the tracking reference keeps slot dedup
    blocking a fresh dispatch for the *same* workspace until it truly stops,
    and the existing done-callback drops it once it does. Only
    ``MONITOR_RESUME`` tasks are inspected, so ready/preserved-active and
    already-draining executions are never touched here.
    """
    monitor_ids = self._tracked_monitor_workspace_ids()
    if not monitor_ids:
        return
    statuses = await self._load_workspace_statuses(monitor_ids)
    for workspace_id in monitor_ids:
        status = statuses.get(workspace_id)
        if status == WorkspaceStatus.monitoring_pr.value:
            continue
        task = self._execution_tasks.get(workspace_id)
        if task is None:
            self._execution_task_kinds.pop(workspace_id, None)
            continue
        _log.warning(
            "worker.stale_monitor_execution_task_cancelled",
            workspace_id=workspace_id,
            status=status,
        )
        task.cancel()
        self._execution_task_kinds[workspace_id] = _ExecutionTaskKind.MONITOR_DRAINING


def _update_execution_slot_saturation(self: Any, *, dispatched: int) -> None:
    saturated = (
        self._can_dispatch_execution_when_slot_available()
        and dispatched == 0
        and self._available_execution_slots() <= 0
    )
    if not saturated:
        self._consecutive_saturated_cycles = 0
        return
    self._consecutive_saturated_cycles += 1
    if self._consecutive_saturated_cycles % _EXECUTION_SLOTS_SATURATED_LOG_INTERVAL != 0:
        return
    _log.warning(
        "worker.execution_slots_saturated",
        slot_limit=self._config.max_concurrent_executions,
        tracked_count=len(self._execution_tasks),
        tracked_workspace_ids=sorted(self._execution_tasks),
        tracked_monitor_ids=sorted(self._tracked_monitor_workspace_ids()),
        tracked_draining_ids=sorted(self._tracked_draining_workspace_ids()),
        consecutive_saturated_cycles=self._consecutive_saturated_cycles,
    )


async def _safely_provision_claimed(self: Any, workspace_id: str) -> None:
    # The execution claim was already stamped on the row by the earlier
    # scheduling transaction, so *every* exit from here must release it —
    # including a cancel landing on the initial epoch read below, before the
    # provision try/finally is even entered. Without the outer finally, an
    # external cancel (e.g. worker shutdown cancelling ``run_once``'s ``gather``)
    # during that read would exit straight out, stranding the row claimed until
    # the lease expires and delaying recovery despite no provision having
    # started. The release is owner+epoch-gated (D6), so it is a no-op when a
    # newer claimant already fenced us; the shielded helper still runs it to
    # completion across a second cancellation (worker shutdown) landing
    # mid-write so neither the DB lease nor the epoch entry leaks.
    try:
        # D2: read our fencing epoch back at provision start. ``None`` means a newer
        # claimant already superseded us (or the row is gone), so abort before any
        # work — never touch the new claimant's row.
        epoch = await self._read_execution_claim_epoch(workspace_id)
        if epoch is None:
            _log.warning(
                "worker.execution_claim_fenced",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
                phase="provision_start",
                reason_code=EXECUTION_CLAIM_FENCED,
            )
            return
        self._execution_claim_epochs[workspace_id] = epoch
        provision_task = asyncio.create_task(
            self._provisioner.provision_claimed(workspace_id, execution_claim_epoch=epoch),
            name=f"awf-provision-{workspace_id}",
        )
        # D4: the heartbeat CAS is epoch-gated; if a later claimant fences us it
        # returns False and cancels the in-flight provision before any rmtree.
        heartbeat = asyncio.create_task(
            self._refresh_execution_claim_loop(workspace_id, on_claim_lost=provision_task.cancel),
            name=f"awf-provisioning-claim-{workspace_id}",
        )
        try:
            await provision_task
        except asyncio.CancelledError:
            # Two cancellations land here: the heartbeat fence (``on_claim_lost``
            # cancels ``provision_task``) and an external cancel of *this* task —
            # e.g. worker shutdown cancelling ``run_once``'s ``gather``. Only the
            # fence is ours to abort quietly; an external cancel must propagate so
            # cooperative cancellation is never suppressed (D7's CAS leaves the row
            # untouched either way). ``current_task().cancelling()`` is the
            # discriminator: cancelling this task increments its own request count
            # even though the cancel is delegated to the awaited ``provision_task``,
            # whereas a pure heartbeat fence never touches this task's count.
            outer = cast("asyncio.Task[None]", asyncio.current_task())
            if outer.cancelling() > 0:
                raise
            # Heartbeat-cancel fired: we were fenced mid-provision. The provisioner
            # leaves the row untouched (D7 CAS), so just abort this attempt.
            _log.warning(
                "worker.execution_claim_fenced",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
                phase="provision_cancelled",
                reason_code=EXECUTION_CLAIM_FENCED,
            )
        except Exception:
            # Provisioner.provision_claimed() logs the failure and attempts to
            # transition to ``failed``; on the fenced path the epoch-CAS in
            # _mark_failed updates 0 rows, so the workspace stays in
            # ``provisioning`` for the new claimant rather than landing in
            # ``failed``. Swallow either way so one bad workspace doesn't abort
            # the batch.
            _log.exception("worker.provision_failed", workspace_id=workspace_id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
    except Exception:
        # The fencing epoch read and claim-task setup above sit *outside* the
        # inner provision try/except, yet ``run_once`` gathers us with
        # ``return_exceptions=False``: a transient failure here (e.g. a DB
        # disconnect on the D2 read) would propagate out and abort the rest of
        # the provision batch instead of being isolated like a provision
        # failure. Swallow it so one bad workspace can't wedge the cycle; an
        # external cancel still propagates (``CancelledError`` is not an
        # ``Exception``) and the outer ``finally`` releases the claim either
        # way. The claiming transaction already transitioned the row
        # ``requested -> provisioning`` before we were dispatched, so the normal
        # poll (which only claims ``requested`` rows) does not re-claim it;
        # instead the released ``provisioning`` row is picked up by the
        # stale-active execution recovery scan, the same recovery contract as
        # the heartbeat-fence path above.
        _log.exception("worker.provision_claim_setup_failed", workspace_id=workspace_id)
    finally:
        # Release CAS on the stored epoch so a release issued after a newer
        # claimant reclaimed the row cannot clobber it (D6). This finally runs
        # while an external cancel may already be propagating; shield the release
        # and the epoch pop so a second cancellation (worker shutdown) landing
        # mid-write cannot skip them and leak the DB lease or the epoch entry.
        await self._release_execution_claim_after_cancellation(workspace_id)
        # Promptly release the terminal runtime when provisioning ended terminal
        # (provision failure → ``failed``), reclaiming the compose stack + per-ws
        # auth overlay immediately rather than on the ~1h interval (#583, #584). A
        # no-op for non-terminal exits and idempotent against the periodic backstop.
        await self._release_terminal_runtime_promptly(workspace_id)


async def _safely_execute(self: Any, workspace_id: str) -> None:
    if self._executor is None:
        return
    try:
        await self._executor.execute(
            workspace_id,
            execution_owner_id=self._worker_id,
            execution_lease_expires_at=self._execution_claim_expires_at(),
        )
    except Exception:
        # WorkspaceExecutor.execute() owns state transitions, including
        # skip-if-no-longer-ready semantics. The worker must keep polling
        # even if one execution path crashes before it can mark a failure.
        _log.exception("worker.execute_failed", workspace_id=workspace_id)


async def _safely_execute_claimed(self: Any, workspace_id: str) -> None:
    heartbeat = asyncio.create_task(
        self._refresh_execution_claim_loop(workspace_id),
        name=f"awf-execution-claim-{workspace_id}",
    )
    try:
        await self._safely_execute(workspace_id)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await self._release_execution_claim(workspace_id)
        # Promptly release the terminal runtime when this execution ended terminal
        # (completed / failed, or a running ws cancelled while we executed it), so
        # the compose stack + per-ws auth overlay are reclaimed immediately instead
        # of on the ~1h interval (#583, #584). A no-op for non-terminal exits and
        # idempotent against the periodic backstop, which stays in place.
        await self._release_terminal_runtime_promptly(workspace_id)


async def _safely_resume_pr_monitor(
    self: Any,
    workspace_id: str,
    *,
    recovery_operation_id: str | None = None,
) -> bool:
    if self._executor is None:
        await self._finish_monitor_recovery_operation(
            workspace_id,
            operation_id=recovery_operation_id,
            status=OperationStatus.failed,
            error_code="MONITOR_RECOVERY_NO_EXECUTOR",
            error_message="Worker has no executor configured.",
        )
        return False
    try:
        await self._executor.resume_pr_monitor(workspace_id)
    except asyncio.CancelledError:
        # A stale-monitor reconcile cancels this task once its workspace has
        # left monitoring_pr. CancelledError is a BaseException, so it skips
        # the Exception handler below; without finalizing here the remonitor
        # operation stays stuck in running while the caller's finally drops
        # _monitor_recovery_operation_ids, losing the handle to finish it
        # later. Finalize through the shielded helper so a second cancellation
        # (e.g. worker shutdown) landing mid-write cannot re-orphan it, then
        # re-raise so the task still ends cancelled and the slot drains.
        await self._finish_monitor_recovery_operation_after_cancellation(
            workspace_id,
            operation_id=recovery_operation_id,
            status=OperationStatus.cancelled,
            error_code="MONITOR_RECOVERY_CANCELLED",
            error_message="Monitor resume cancelled after workspace left monitoring_pr.",
        )
        raise
    except Exception as exc:
        # The monitor runner owns normal terminal transitions. Recovery
        # dispatch still must not take the service worker down if a single
        # workspace hits an unexpected runtime error.
        _log.exception("worker.pr_monitor_resume_failed", workspace_id=workspace_id)
        await self._finish_monitor_recovery_operation(
            workspace_id,
            operation_id=recovery_operation_id,
            status=OperationStatus.failed,
            error_code="MONITOR_RECOVERY_FAILED",
            error_message=repr(exc)[:2000],
        )
        return False

    await self._finish_monitor_recovery_operation(
        workspace_id,
        operation_id=recovery_operation_id,
        status=OperationStatus.succeeded,
    )
    return True


async def _refresh_monitoring_pr_claim_loop(self: Any, workspace_id: str) -> None:
    interval = max(1.0, min(60.0, self._config.monitor_claim_lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        try:
            refreshed = await self._refresh_monitoring_pr_claim(workspace_id)
        except Exception:
            _log.exception(
                "worker.monitor_claim_refresh_failed",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
            )
            return
        if not refreshed:
            _log.warning(
                "worker.monitor_claim_lost",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
            )
            return


async def _refresh_execution_claim_loop(
    self: Any,
    workspace_id: str,
    *,
    on_claim_lost: Callable[[], object] | None = None,
) -> None:
    interval = max(1.0, min(60.0, self._config.execution_claim_lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        try:
            refreshed = await self._refresh_execution_claim(workspace_id)
        except Exception:
            _log.exception(
                "worker.execution_claim_refresh_failed",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
            )
            # D4: a refresh failure (e.g. a transient DB disconnect) leaves us
            # unable to confirm we still hold the lease, yet the heartbeat loop
            # is about to die so ``execution_claim_expires_at`` stops being
            # renewed. On the provisioning path, fence conservatively: cancel the
            # in-flight provision task so a worker that may have silently lost the
            # lease stops before any destructive git/compose op, rather than
            # racing a new claimant that reclaims and bumps the epoch. The row is
            # left ``provisioning`` with its claim released by
            # ``_safely_provision_claimed``'s outer ``finally``. The normal poll
            # only claims ``requested`` rows, so it never re-claims this row;
            # instead the released row is owned by the stale-active execution
            # recovery scan, which covers ``provisioning`` rows whose execution
            # claim is released/expired (see
            # ``_list_stale_active_execution_candidates``) and cleans up and
            # fails the interrupted attempt for retry-policy/operator retry —
            # the same recovery contract as any other interrupted active work.
            # The executor path passes no callback.
            if on_claim_lost is not None:
                on_claim_lost()
            return
        if not refreshed:
            _log.warning(
                "worker.execution_claim_lost",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
            )
            # D4: on the provisioning path this cancels the in-flight provision
            # task so a fenced worker stops before any destructive filesystem op.
            # The executor path passes no callback, preserving its behavior.
            if on_claim_lost is not None:
                on_claim_lost()
            return
