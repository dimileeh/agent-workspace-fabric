"""Suspend an opted-in PR monitor at the end of a human wait."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from awf.common.workspace_policy import pr_adoption_is_hosted
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.resilience import is_transient_closed_connection_error
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.runtime.inspection import command_environment
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_operations import MonitorOperationHandle, finish_monitor_operation
from awf.runtime.pr_monitor_runner.helpers import _as_utc, _clear_transient_forge_retry_state
from awf.runtime.pr_monitor_runner.lifecycle import (
    _apply_monitor_state,
    _completed_workspace_teardown_template_sentinel,
    _superseded_monitor_owner,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.service.secret_leases import SecretLeaseService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

HUMAN_WAIT_DORMANT = "HUMAN_WAIT_DORMANT"
HUMAN_WAIT_DORMANCY_FAILED = "HUMAN_WAIT_DORMANCY_FAILED"
HUMAN_WAIT_DORMANCY_BLOCKED = "HUMAN_WAIT_DORMANCY_BLOCKED"
_OPEN_OPERATION_STATUSES = {OperationStatus.pending.value, OperationStatus.running.value}


class DormancyExit(Enum):
    DORMANT = auto()
    RECOVER_RUNTIME = auto()
    SUPERSEDED = auto()


class _DormancyError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _owns_wait(self: Any, workspace: Workspace) -> bool:
    if workspace.status != WorkspaceStatus.monitoring_pr.value or _superseded_monitor_owner(
        self, workspace
    ):
        return False
    now = datetime.now(UTC)
    if self._monitor_owner_id is not None:
        # A recovery claim can preserve the same worker's execution lease.
        expires = workspace.monitor_claim_expires_at
        return expires is not None and _as_utc(expires) > now
    execution_owner = workspace.execution_claimed_by
    if execution_owner is None:
        return self._dormancy_execution_claim is None
    expires = workspace.execution_claim_expires_at
    return (
        self._dormancy_execution_claim == (execution_owner, workspace.execution_claim_epoch)
        and expires is not None
        and _as_utc(expires) > now
    )


async def _suspension_operations(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    *,
    awaiting_since: datetime,
    threshold: float,
    operation: MonitorOperationHandle | None,
) -> tuple[Workspace, Operation, list[Operation]] | None:
    repo = WorkspaceRepository(session)
    workspace = await repo.get_for_update(workspace_id)
    if workspace is None or not _owns_wait(self, workspace):
        raise _DormancyError("ownership_changed")
    if workspace.awaiting_human_since != awaiting_since:
        raise _DormancyError("attention_episode_changed")

    latest_failure = next(
        (
            event
            for event in reversed(workspace.events)
            if event.event_type == "workspace.dormancy_failed"
        ),
        None,
    )
    if (
        latest_failure is not None
        and (_as_utc(self._deps.now()) - _as_utc(latest_failure.occurred_at)).total_seconds()
        < threshold
    ):
        return None

    if operation is None:
        raise _DormancyError("human_wait_operation_missing")
    current = await session.get(
        Operation, operation.operation_id, with_for_update=True, populate_existing=True
    )
    if current is None:
        raise _DormancyError("human_wait_operation_missing")

    superseded_waits = []
    blocking_ids = []
    for other in workspace.operations:
        if other.id == current.id or other.status not in _OPEN_OPERATION_STATUSES:
            continue
        # A restart can give the next poll a different identity. Only passive
        # monitor waits can be settled as part of suspension.
        payload = other.payload or {}
        if payload.get("owner") == "pr_monitor" and (
            other.type == OperationType.human_wait.value
            or (other.type == OperationType.monitor_state.value and "wait_seconds" in payload)
        ):
            superseded_waits.append(other)
        else:
            blocking_ids.append(other.id)

    if not blocking_ids:
        return workspace, current, superseded_waits

    episode = awaiting_since.isoformat()
    latest_outcome = next(
        (
            event
            for event in reversed(workspace.events)
            if event.event_type
            in {"workspace.dormancy_blocked", "workspace.dormancy_failed", "workspace.dormant"}
        ),
        None,
    )
    if (
        latest_outcome is None
        or latest_outcome.event_type != "workspace.dormancy_blocked"
        or (latest_outcome.payload or {}).get("awaiting_human_since") != episode
    ):
        await repo.add_event(
            workspace,
            event_type="workspace.dormancy_blocked",
            reason_code=HUMAN_WAIT_DORMANCY_BLOCKED,
            payload={"awaiting_human_since": episode, "blocking_operation_ids": blocking_ids},
        )
        await session.commit()
    return None


async def suspend_human_wait(
    self: Any,
    *,
    workspace_id: str,
    awaiting_since: datetime,
    threshold: float,
    state: MonitorState,
    operation: MonitorOperationHandle | None,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None,
) -> DormancyExit | None:
    stage = "ownership"
    teardown_started = False
    dormant_at = None
    try:
        if monitor_log is not None:
            async with self._deps.session_factory() as session:
                if (
                    await _suspension_operations(
                        self,
                        session,
                        workspace_id,
                        awaiting_since=awaiting_since,
                        threshold=threshold,
                        operation=operation,
                    )
                    is None
                ):
                    return None
            stage = "monitor_log"
            # A sink can also update workspace activity in a separate session.
            # Write the attempt before acquiring the row lock.
            await monitor_log.write(
                json.dumps(
                    {
                        "event": "monitor.dormancy_requested",
                        "workspace_id": workspace_id,
                        "reason_code": HUMAN_WAIT_DORMANT,
                        "awaiting_human_since": awaiting_since.isoformat(),
                    }
                )
                + "\n"
            )
        stage = "ownership"
        async with self._deps.session_factory() as session:
            eligible = await _suspension_operations(
                self,
                session,
                workspace_id,
                awaiting_since=awaiting_since,
                threshold=threshold,
                operation=operation,
            )
            if eligible is None:
                return None
            workspace, current, superseded_waits = eligible
            if monitor_log is None:
                _log.info(
                    "monitor.dormancy_requested",
                    workspace_id=workspace_id,
                    reason_code=HUMAN_WAIT_DORMANT,
                    awaiting_human_since=awaiting_since.isoformat(),
                )
            repo = WorkspaceRepository(session)
            operations = OperationRepository(session)

            # Hold the workspace fence during the external effect. Delay guarded
            # writes until after Compose so they cannot hold the node mutex here.
            if workspace.compose_project_name and not pr_adoption_is_hosted(workspace.task_policy):
                stage = "compose_teardown"
                manager = ComposeManager(
                    work_dir=self._work_dir,
                    template_path=_completed_workspace_teardown_template_sentinel(self._work_dir),
                    command_environment=command_environment(),
                )
                teardown_started = True
                teardown = await manager.teardown_project(
                    project_name=workspace.compose_project_name,
                    compose_file=(
                        Path(workspace.compose_file_path)
                        if workspace.compose_file_path
                        else compose_file
                    ),
                    workspace_id=workspace_id,
                    remove_volumes=False,
                )
                if not teardown.ok:
                    raise _DormancyError(teardown.reason_code)

            stage = "operation_finish"
            for other in superseded_waits:
                await operations.finish(
                    other,
                    status=OperationStatus.cancelled,
                    result={"outcome": "superseded"},
                )
            if current.status not in _OPEN_OPERATION_STATUSES:
                # Repeated waits reuse the poll operation identity. Record a new
                # wait for this action while preserving the earlier poll result.
                current = await operations.create(
                    workspace_id=workspace_id,
                    operation_type=OperationType.human_wait,
                    status=OperationStatus.running,
                    payload=current.payload,
                )
            finished = await finish_monitor_operation(
                session,
                operation_id=current.id,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "dormant",
                    "reason_code": HUMAN_WAIT_DORMANT,
                },
            )
            if (
                finished is None
                or finished.status != OperationStatus.succeeded.value
                or (finished.result or {}).get("outcome") != "dormant"
            ):
                raise _DormancyError("human_wait_operation_not_finished")

            dormant_at = _as_utc(self._deps.now())
            stage = "secret_revocation"
            await SecretLeaseService(session).revoke_workspace_secret_leases(
                workspace, now=dormant_at, reason_code=HUMAN_WAIT_DORMANT
            )
            stage = "event"
            _clear_transient_forge_retry_state(state, context="execute_action")
            _apply_monitor_state(workspace, state)
            payload = {
                "reason_code": HUMAN_WAIT_DORMANT,
                "awaiting_human_since": awaiting_since.isoformat(),
                "dormant_at": dormant_at.isoformat(),
            }
            await repo.add_event(
                workspace,
                event_type="workspace.dormant",
                reason_code=HUMAN_WAIT_DORMANT,
                payload=payload,
            )
            workspace.dormant_at = dormant_at
            workspace.monitor_claimed_by = workspace.monitor_claim_expires_at = None
            workspace.execution_claimed_by = workspace.execution_claim_expires_at = None
            stage = "commit"
            await session.commit()
    except (_DormancyError, ComposeOperationError, SQLAlchemyError, OSError) as exc:
        result = await _record_dormancy_failure(
            self,
            workspace_id=workspace_id,
            awaiting_since=awaiting_since,
            dormant_at=dormant_at,
            state=state,
            operation=operation,
            stage=stage,
            teardown_started=teardown_started,
            failure_code=(
                exc.reason_code
                if isinstance(exc, (_DormancyError, ComposeOperationError))
                else type(exc).__name__
            ),
            monitor_log=monitor_log,
        )
        if result is not DormancyExit.DORMANT:
            return result
    _log.info("monitor.dormant", workspace_id=workspace_id, reason_code=HUMAN_WAIT_DORMANT)
    return DormancyExit.DORMANT


async def _record_dormancy_failure(
    self: Any,
    *,
    workspace_id: str,
    awaiting_since: datetime,
    dormant_at: datetime | None,
    state: MonitorState,
    operation: MonitorOperationHandle | None,
    stage: str,
    teardown_started: bool,
    failure_code: str,
    monitor_log: WorkspaceLogSink | None,
) -> DormancyExit | None:
    payload = {
        "dormancy_attempt": uuid4().hex,
        "stage": stage,
        "failure_code": failure_code,
        "runtime_recovery_required": teardown_started,
    }

    async def record(session: AsyncSession) -> DormancyExit | None:
        repo = WorkspaceRepository(session)
        workspace = await repo.get_for_update(workspace_id)
        if (
            stage == "commit"
            and dormant_at is not None
            and workspace is not None
            and workspace.dormant_at == dormant_at
        ):
            return DormancyExit.DORMANT
        owns_wait = workspace is not None and _owns_wait(self, workspace)
        if (
            workspace is not None
            and workspace.dormant_at is None
            and not any(
                event.event_type == "workspace.dormancy_failed"
                and (event.payload or {}).get("dormancy_attempt") == payload["dormancy_attempt"]
                for event in workspace.events
            )
        ):
            if owns_wait and workspace.awaiting_human_since == awaiting_since:
                # Keep the notice deduplicated even when we must exit and let
                # the regular monitor-resume path restart the stack and replay setup.
                _apply_monitor_state(workspace, state)
                if teardown_started and operation is not None and operation.should_finish:
                    operations = OperationRepository(session)
                    current = await operations.get(operation.operation_id)
                    if current is not None and current.status in _OPEN_OPERATION_STATUSES:
                        try:
                            # Roll back a failed operation write without losing
                            # the failure event or the persisted monitor state.
                            async with session.begin_nested():
                                await operations.finish(
                                    current,
                                    status=OperationStatus.failed,
                                    result={"outcome": "dormancy_failed", **payload},
                                    error_code=HUMAN_WAIT_DORMANCY_FAILED,
                                )
                        except (SQLAlchemyError, OSError) as exc:
                            _log.warning(
                                "monitor.dormancy_failure_operation_finish_failed",
                                workspace_id=workspace_id,
                                operation_id=operation.operation_id,
                                reason_code=HUMAN_WAIT_DORMANCY_FAILED,
                                error_type=type(exc).__name__,
                            )
            await repo.add_event(
                workspace,
                event_type="workspace.dormancy_failed",
                reason_code=HUMAN_WAIT_DORMANCY_FAILED,
                payload=payload,
            )
        if not owns_wait:
            return DormancyExit.SUPERSEDED
        return DormancyExit.RECOVER_RUNTIME if teardown_started else None

    while True:
        try:
            async with self._deps.session_factory() as session:
                result = await record(session)
                await session.commit()
            break
        except (SQLAlchemyError, OSError) as exc:
            _log.error(
                "monitor.dormancy_failure_event_unavailable",
                workspace_id=workspace_id,
                reason_code=HUMAN_WAIT_DORMANCY_FAILED,
                error_type=type(exc).__name__,
                **payload,
            )
            if isinstance(exc, (ConnectionError, TimeoutError, DatabaseTimeoutError)) or (
                is_transient_closed_connection_error(exc)
            ):
                # Wait for a reporting session only. Never replay the external
                # effect or the dormancy commit after a failed attempt.
                await self._deps.sleep(self._config.poll_interval_seconds)
                continue
            # Claim-gated resume cannot recreate a stack for a committed
            # dormancy, even when the response to that commit was lost.
            result = DormancyExit.RECOVER_RUNTIME if teardown_started or stage == "commit" else None
            break
    if result is not DormancyExit.DORMANT:
        _log.warning(
            "monitor.dormancy_failed",
            workspace_id=workspace_id,
            reason_code=HUMAN_WAIT_DORMANCY_FAILED,
            **payload,
        )
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.dormancy_failed",
                "reason_code": HUMAN_WAIT_DORMANCY_FAILED,
                **payload,
            },
        )
    return result
