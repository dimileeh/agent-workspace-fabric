"""Stale active execution recovery operations.

Mechanically extracted from recovery.py; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from time import monotonic
from typing import Any, cast

from sqlalchemy import (
    and_,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_RECOVERY_EVIDENCE_EVENTS,
    _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_SOURCE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    _ACTIVE_EXECUTION_STALE_FAILURE_BLOCKING_SALVAGE_CHECKS,
    _ACTIVE_EXECUTION_STATUSES,
    _DB_CONNECTION_TRANSIENT_EVENT_TYPE,
    _REQUESTED_ADMISSION_SLOT_STATUSES,
    _RUNTIME_HEALTH_SCAN_STATUSES,
    _STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE,
    _STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE,
    _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
    _STALE_ACTIVE_EXECUTION_REASON_CODE,
    _STALE_ACTIVE_EXECUTION_RECOVERY_FAILED_REASON_CODE,
)
from awf.control.worker.helpers import (
    _candidate_claim_is_stale,
    _execution_claim_is_stale,
    _has_running_agent_runtime,
    _running_monitoring_pr_recovery_finding,
    _runtime_snapshot_payload,
    _runtime_stranding_event_payload,
    _runtime_stranding_failure_message,
    _runtime_workspace,
    _secondary_runtime_stranding_payload,
    _secondary_stale_active_execution_payload,
    _stale_active_execution_failure_message,
    _worker_exception_is_transient_db_connection,
    _workspace_claim_recheck_passes,
)
from awf.control.worker.logging import _log
from awf.control.worker.scheduling import (
    _claim_recheck_conditions,
    _monitor_provider_recovery_resume_pending,
    _stale_execution_claim_filter,
    _stale_monitor_claim_filter,
    _utc_datetime,
)
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import (
    FailureReason,
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.models import (
    Operation,
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories import WorkspaceRepository
from awf.db.resilience import (
    DB_CONNECTION_CLOSED_REASON,
    run_db_operation_with_retry,
)
from awf.db.session import (
    session_scope,
)
from awf.node.cleanup import WorkspaceCleanupResult
from awf.runtime.inspection import RuntimeSnapshot
from awf.service.failure_causality import (
    attach_primary_failure,
    build_preserved_failure_payload,
    load_failure_causality_snapshot,
    load_primary_failure_snapshot,
    primary_failure_reason_code,
    restore_primary_failure_row_fields,
)
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_STATE_KEY,
)
from awf.service.workspace_runtime_health import (
    RUNTIME_STRANDED_EVENT_TYPE,
    WorkspaceRuntimeFinding,
    classify_runtime_snapshot,
    has_open_pr_for_remonitor,
)


async def _maybe_recover_stale_active_executions(self: Any) -> None:
    now = monotonic()
    if now < self._next_stale_active_execution_scan_at:
        return

    try:
        await self._recover_stale_active_executions()
    except Exception as exc:
        if _worker_exception_is_transient_db_connection(exc):
            interval = max(0.0, self._config.stale_active_execution_scan_interval_seconds)
            self._next_stale_active_execution_scan_at = monotonic() + interval
            _log.warning(
                "worker.stale_active_execution_scan_db_connection_closed",
                reason_code=DB_CONNECTION_CLOSED_REASON,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            return
        raise

    interval = max(0.0, self._config.stale_active_execution_scan_interval_seconds)
    self._next_stale_active_execution_scan_at = monotonic() + interval


async def _recover_stale_active_executions(self: Any) -> None:
    candidates = await self._list_stale_active_execution_candidates(
        exclude_ids=set(self._execution_tasks)
    )
    recovery_errors: list[Exception] = []
    for candidate in candidates:
        try:
            await self._recover_stale_active_execution(candidate)
        except Exception as exc:
            if _worker_exception_is_transient_db_connection(exc):
                _log.warning(
                    "worker.stale_active_execution_db_connection_closed",
                    workspace_id=candidate.workspace_id,
                    status=candidate.status.value,
                    reason_code=DB_CONNECTION_CLOSED_REASON,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
                await self._record_db_connection_closed_event(candidate, exc)
                continue
            _log.exception(
                "worker.stale_active_execution_recovery_failed",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                reason_code=_STALE_ACTIVE_EXECUTION_RECOVERY_FAILED_REASON_CODE,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            recovery_errors.append(exc)
    if len(recovery_errors) == 1:
        raise recovery_errors[0]
    if recovery_errors:
        raise ExceptionGroup(
            "stale active execution recovery failed",
            recovery_errors,
        )


async def _record_db_connection_closed_event(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    exc: BaseException,
) -> None:
    payload = {
        "workspace_status": candidate.status.value,
        "compose_project_name": candidate.compose_project_name,
        "message": "Transient closed database connection interrupted worker recovery scan.",
        "error_type": type(exc).__name__,
        "error": str(exc)[:240],
    }
    try:
        async with session_scope(self._session_factory) as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            await repo.add_event(
                ws,
                event_type=_DB_CONNECTION_TRANSIENT_EVENT_TYPE,
                reason_code=DB_CONNECTION_CLOSED_REASON,
                payload=payload,
            )
    except Exception:
        _log.exception(
            "worker.db_connection_closed_event_failed",
            workspace_id=candidate.workspace_id,
            reason_code=DB_CONNECTION_CLOSED_REASON,
        )


async def _list_stale_active_execution_candidates(
    self: Any,
    *,
    exclude_ids: set[str],
) -> list[_ActiveExecutionCandidate]:
    active_status_values = [status.value for status in _RUNTIME_HEALTH_SCAN_STATUSES]
    active_execution_values = [status.value for status in _ACTIVE_EXECUTION_STATUSES]
    admission_slot_status_values = [status.value for status in _REQUESTED_ADMISSION_SLOT_STATUSES]
    claim_cutoff = datetime.now(UTC)
    worker_node_id = self._config.node_id or "local"
    stmt = (
        select(
            Workspace.id,
            Workspace.status,
            Workspace.repo_url,
            Workspace.agent,
            Workspace.compose_project_name,
            Workspace.compose_file_path,
            Workspace.pr_url,
            Workspace.task_policy,
        )
        .where(Workspace.status.in_(active_status_values))
        .where(
            or_(
                Workspace.node_id == worker_node_id,
                and_(
                    Workspace.node_id.is_(None),
                    Workspace.status.in_(admission_slot_status_values),
                ),
            )
        )
        .where(
            or_(
                Workspace.status.in_(
                    [
                        WorkspaceStatus.requested.value,
                        WorkspaceStatus.ready.value,
                    ]
                ),
                and_(
                    Workspace.status == WorkspaceStatus.provisioning.value,
                    _stale_execution_claim_filter(claim_cutoff),
                ),
                and_(
                    Workspace.status.in_(active_execution_values),
                    _stale_execution_claim_filter(claim_cutoff),
                ),
                and_(
                    Workspace.status == WorkspaceStatus.monitoring_pr.value,
                    _stale_monitor_claim_filter(claim_cutoff),
                ),
            )
        )
        .order_by(Workspace.created_at.asc(), Workspace.id.asc())
    )
    if exclude_ids:
        stmt = stmt.where(~Workspace.id.in_(sorted(exclude_ids)))

    async def _operation(session: AsyncSession) -> list[Any]:
        result = await session.execute(stmt)
        return list(result.all())

    rows = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        on_retry=self._log_transient_db_retry,
    )

    return [
        _ActiveExecutionCandidate(
            workspace_id=row[0],
            status=WorkspaceStatus(row[1]),
            repo_url=row[2],
            agent=row[3],
            compose_project_name=row[4],
            compose_file_path=row[5],
            pr_url=row[6],
            task_policy=row[7],
        )
        for row in rows
    ]


async def _recover_stale_active_execution(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> None:
    monitor_recovery_resume_pending = await _monitor_provider_recovery_resume_pending(
        self._session_factory,
        candidate,
    )
    if monitor_recovery_resume_pending:
        _log.info(
            "worker.monitor_provider_recovery_resume_pending",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code="PROVIDER_RECOVERY_MONITOR_RESUME_PENDING",
        )
        return
    attempted_preserved_active_recovery = False
    if await self._has_preserved_active_recovery_evidence(candidate):
        attempted_preserved_active_recovery = True
        if not await self._has_operator_refresh_after_latest_preservation(
            candidate
        ) and await self._recover_preserved_active_execution(candidate):
            return
    if not await self._stale_active_candidate_is_current(candidate):
        return
    try:
        snapshot = await self._runtime_inspector.inspect(candidate.compose_project_name)
    except Exception as exc:  # pragma: no cover - defensive around Docker tooling
        _log.exception(
            "worker.stale_active_execution_inspect_failed",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
        )
        snapshot = RuntimeSnapshot(
            stack_state="unavailable",
            reason=f"runtime inspection failed: {exc}",
        )

    finding = classify_runtime_snapshot(_runtime_workspace(candidate), snapshot)
    task_policy: dict[str, Any] = candidate.task_policy or {}
    if candidate.status == WorkspaceStatus.ready and finding is None:
        return
    monitor_recovery_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
    is_retry_recovery = (
        isinstance(monitor_recovery_state, Mapping)
        and monitor_recovery_state.get("action") == "retry"
    )
    if (
        candidate.status == WorkspaceStatus.monitoring_pr
        and is_retry_recovery
        and candidate.compose_project_name
        and finding is None
        and snapshot.stack_state == "running"
    ):
        return
    if (
        candidate.status == WorkspaceStatus.monitoring_pr
        and has_open_pr_for_remonitor(candidate.status.value, candidate.pr_url)
        and finding is None
        and snapshot.stack_state == "running"
        and await self._candidate_has_claim(candidate)
    ):
        await self._record_recoverable_runtime_stranding(
            candidate,
            snapshot,
            _running_monitoring_pr_recovery_finding(candidate),
        )
        return
    if finding is not None and finding.status == "unavailable":
        if has_open_pr_for_remonitor(candidate.status, candidate.pr_url):
            recoverable_finding = WorkspaceRuntimeFinding(
                workspace_id=finding.workspace_id,
                workspace_status=finding.workspace_status,
                status=finding.status,
                reason_code=finding.reason_code,
                decision="remonitor_workspace",
                message=finding.message,
                compose_project_name=finding.compose_project_name,
                services=finding.services,
            )
            await self._record_recoverable_runtime_stranding(
                candidate,
                snapshot,
                recoverable_finding,
            )
            return
        _log.warning(
            "worker.runtime_health_inspection_unavailable",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            runtime_reason=snapshot.reason,
            reason_code=finding.reason_code,
        )
        return
    if finding is not None and finding.decision == "fail_workspace":
        await self._fail_stranded_workspace(candidate, snapshot, finding)
        return
    if finding is not None and finding.decision == "remonitor_workspace":
        await self._record_recoverable_runtime_stranding(candidate, snapshot, finding)
        return
    if finding is not None and finding.decision == "defer_retry_policy":
        await self._record_recoverable_runtime_stranding(candidate, snapshot, finding)
        return
    if candidate.status in _ACTIVE_EXECUTION_STATUSES and _has_running_agent_runtime(snapshot):
        if await self._has_expired_preserved_active_execution(candidate):
            if (
                not attempted_preserved_active_recovery
                and await self._recover_preserved_active_execution(candidate)
            ):
                return
            if not await self._record_stale_active_execution_detected(
                candidate,
                snapshot,
            ):
                await self._cleanup_and_fail_stale_active_execution(candidate, snapshot)
            return
        if not await self._has_operator_refresh_after_latest_preservation(candidate):
            await self._record_preserved_active_execution_after_restart(candidate, snapshot)
            await self._recover_preserved_active_execution(
                candidate,
                record_lineage_blocked_before_grace=False,
            )
            return
    if candidate.compose_project_name and snapshot.stack_state == "running":
        if not await self._record_stale_active_execution_detected(
            candidate,
            snapshot,
        ):
            await self._cleanup_and_fail_stale_active_execution(candidate, snapshot)
        return


async def _stale_active_candidate_is_current(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        return ws is not None and ws.status == candidate.status.value


async def _candidate_has_claim(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        return (
            ws is not None
            and ws.status == candidate.status.value
            and (ws.monitor_claimed_by is not None or ws.execution_claimed_by is not None)
        )


async def _has_preserved_active_recovery_evidence(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    if candidate.status not in _ACTIVE_EXECUTION_STATUSES:
        return False

    event_filters = [
        and_(
            WorkspaceEvent.event_type == event_type,
            WorkspaceEvent.reason_code == reason_code,
        )
        for event_type, reason_code in _ACTIVE_EXECUTION_RECOVERY_EVIDENCE_EVENTS
    ]
    preserved_event_exists = (
        select(literal(1))
        .select_from(WorkspaceEvent)
        .where(
            WorkspaceEvent.workspace_id == candidate.workspace_id,
            or_(*event_filters),
        )
        .limit(1)
        .exists()
    )
    active_recovery_operation_exists = (
        select(literal(1))
        .select_from(Operation)
        .where(
            Operation.workspace_id == candidate.workspace_id,
            Operation.status.in_(
                (
                    OperationStatus.pending.value,
                    OperationStatus.running.value,
                )
            ),
            Operation.payload["source"].as_string() == _ACTIVE_EXECUTION_SALVAGE_SOURCE,
            Operation.payload["reason_code"].as_string()
            == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
        )
        .limit(1)
        .exists()
    )
    stmt = select(literal(1)).where(or_(preserved_event_exists, active_recovery_operation_exists))

    async with self._session_factory() as session:
        return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _record_stale_active_execution_detected(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
) -> bool:
    payload = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "runtime": _runtime_snapshot_payload(snapshot),
    }

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        if not _execution_claim_is_stale(ws, datetime.now(UTC)):
            return False
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            ws,
            candidate.status,
        )
        if await self._has_stale_active_execution_event(
            session,
            candidate.workspace_id,
            event_floor=event_floor,
        ):
            return False

        primary_failure = await load_primary_failure_snapshot(session, ws)
        event_payload = attach_primary_failure(payload, primary_failure)
        await repo.add_event(
            ws,
            event_type=_STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
            payload=event_payload,
        )
        await session.commit()

    _log.warning(
        _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        runtime=payload["runtime"],
    )
    return True


async def _has_stale_active_execution_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    *,
    event_floor: datetime | None = None,
) -> bool:
    _ = self
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            WorkspaceEvent.reason_code == _STALE_ACTIVE_EXECUTION_REASON_CODE,
        )
        .limit(1)
    )
    if event_floor is not None:
        stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _cleanup_and_fail_stale_active_execution(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
) -> None:
    if not await self._stale_active_execution_can_fail(candidate):
        return
    if self._runtime_cleaner is None or not candidate.repo_url:
        await self._record_stale_active_execution_cleanup_failed(
            candidate,
            snapshot,
            cleanup=None,
            message=(
                "runtime cleanup is not configured"
                if self._runtime_cleaner is None
                else "workspace has no repo_url for cleanup"
            ),
        )
        return

    cleanup = await self._runtime_cleaner.cleanup(
        workspace_id=candidate.workspace_id,
        repo_url=candidate.repo_url,
        compose_project_name=candidate.compose_project_name,
        compose_file_path=(
            Path(candidate.compose_file_path) if candidate.compose_file_path else None
        ),
        remove_volumes=True,
        remove_worktree=False,
    )
    if not cleanup.ok:
        await self._record_stale_active_execution_cleanup_failed(
            candidate,
            snapshot,
            cleanup=cleanup,
            message="failed to stop or remove stale workspace runtime",
        )
        return

    await self._fail_stale_active_execution(candidate, snapshot)


async def _stale_active_execution_can_fail(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        if not _execution_claim_is_stale(ws, datetime.now(UTC)):
            return False
        if (
            candidate.status
            in (
                WorkspaceStatus.running,
                WorkspaceStatus.validating,
                WorkspaceStatus.pushing,
            )
            and await self._has_active_preserved_validation_recovery(
                session,
                candidate.workspace_id,
            )
            and self._preserved_active_validation_can_continue(candidate.workspace_id)
        ):
            return False
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            ws,
            candidate.status,
        )
        preservation_event_floor = await self._active_execution_preservation_salvage_event_floor(
            session,
            ws,
            candidate.status,
        )
        latest_preserved = await self._latest_preserved_active_execution_at(
            session,
            candidate.workspace_id,
            candidate.status,
            event_floor=preservation_event_floor,
            match_active_execution_statuses=True,
        )
        if latest_preserved is not None:
            latest_preserved = _utc_datetime(latest_preserved)
            for (
                event_type,
                reason_code,
            ) in _ACTIVE_EXECUTION_STALE_FAILURE_BLOCKING_SALVAGE_CHECKS:
                if event_type == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE:
                    if not await self._has_current_salvage_event(
                        session,
                        candidate.workspace_id,
                        event_type=event_type,
                        reason_code=reason_code,
                        event_floor=latest_preserved,
                        workspace_status=candidate.status,
                    ):
                        continue
                    if self._preserved_active_validation_can_continue(candidate.workspace_id):
                        return False
                    if await self._has_current_salvage_event(
                        session,
                        candidate.workspace_id,
                        event_type=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
                        reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
                        event_floor=latest_preserved,
                        workspace_status=candidate.status,
                    ):
                        continue
                    return False
                if await self._has_current_salvage_event(
                    session,
                    candidate.workspace_id,
                    event_type=event_type,
                    reason_code=reason_code,
                    event_floor=latest_preserved,
                    workspace_status=candidate.status,
                ):
                    return False
            if not self._active_execution_preservation_is_expired(latest_preserved):
                return False
        return cast(
            bool,
            await self._has_stale_active_execution_event(
                session,
                candidate.workspace_id,
                event_floor=event_floor,
            ),
        )


async def _record_stale_active_execution_cleanup_failed(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    *,
    cleanup: WorkspaceCleanupResult | None,
    message: str,
) -> None:
    payload: dict[str, Any] = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "runtime": _runtime_snapshot_payload(snapshot),
        "message": message,
    }
    if cleanup is not None:
        payload["cleanup"] = cleanup.to_dict()

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        if not _execution_claim_is_stale(ws, datetime.now(UTC)):
            return
        primary_failure = await load_primary_failure_snapshot(session, ws)
        event_payload = attach_primary_failure(payload, primary_failure)
        await repo.add_event(
            ws,
            event_type=_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE,
            reason_code=_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE,
            payload=event_payload,
        )
        await session.commit()

    _log.error(
        _STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE,
        message=message,
    )


async def _fail_stale_active_execution(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
) -> None:
    message = _stale_active_execution_failure_message(candidate, snapshot)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return
        # Keep causality and transition in one locked epoch where row
        # locks exist; the guarded transition below preserves the stale
        # claim predicate for dialects without row locks.
        failure_causality = await load_failure_causality_snapshot(session, ws)
        primary_failure = (
            failure_causality.primary_failure if failure_causality is not None else None
        )
        previous_secondary_failures = (
            failure_causality.secondary_failures if failure_causality is not None else ()
        )
        secondary_failure = _secondary_stale_active_execution_payload(
            candidate,
            snapshot,
            message=message,
        )
        reason_code = primary_failure_reason_code(
            primary_failure,
            fallback=_STALE_ACTIVE_EXECUTION_REASON_CODE,
        )
        transition_payload = (
            build_preserved_failure_payload(
                primary_failure,
                secondary_failure=secondary_failure,
                previous_secondary_failures=previous_secondary_failures,
            )
            if primary_failure is not None
            else None
        )
        transitioned = await repo.transition_if_current(
            candidate.workspace_id,
            from_status=candidate.status,
            to=WorkspaceStatus.failed,
            reason_code=reason_code,
            payload=transition_payload,
            extra_conditions=(_stale_execution_claim_filter(claim_cutoff),),
        )
        if transitioned is None:
            return
        ws = transitioned
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        if primary_failure is None:
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message[:2048]
        else:
            restore_primary_failure_row_fields(ws, primary_failure)
        await session.commit()

    _log.error(
        "worker.stale_active_execution_failed",
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        runtime_state=snapshot.stack_state,
        runtime_reason=snapshot.reason,
        reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
    )


async def _fail_stranded_workspace(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    finding: WorkspaceRuntimeFinding,
) -> None:
    message = _runtime_stranding_failure_message(candidate, finding)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        claim_cutoff = datetime.now(UTC)
        if not _workspace_claim_recheck_passes(ws, candidate.status, claim_cutoff):
            return
        # Keep causality and transition in one locked epoch where row
        # locks exist; the guarded transition below preserves the stale
        # claim predicate for dialects without row locks.
        failure_causality = await load_failure_causality_snapshot(session, ws)
        primary_failure = (
            failure_causality.primary_failure if failure_causality is not None else None
        )
        previous_secondary_failures = (
            failure_causality.secondary_failures if failure_causality is not None else ()
        )
        secondary_failure = _secondary_runtime_stranding_payload(
            candidate,
            snapshot,
            finding,
            message=message,
        )
        reason_code = primary_failure_reason_code(
            primary_failure,
            fallback=finding.reason_code,
        )
        transition_payload = (
            build_preserved_failure_payload(
                primary_failure,
                secondary_failure=secondary_failure,
                previous_secondary_failures=previous_secondary_failures,
            )
            if primary_failure is not None
            else None
        )
        transitioned = await repo.transition_if_current(
            candidate.workspace_id,
            from_status=candidate.status,
            to=WorkspaceStatus.failed,
            reason_code=reason_code,
            payload=transition_payload,
            extra_conditions=_claim_recheck_conditions(candidate.status, claim_cutoff),
        )
        if transitioned is None:
            return
        ws = transitioned
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        ws.monitor_claimed_by = None
        ws.monitor_claim_expires_at = None
        if primary_failure is None:
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message[:2048]
        else:
            restore_primary_failure_row_fields(ws, primary_failure)
        event_payload = attach_primary_failure(
            _runtime_stranding_event_payload(candidate, snapshot, finding),
            primary_failure,
        )
        await repo.add_event(
            ws,
            event_type=RUNTIME_STRANDED_EVENT_TYPE,
            reason_code=finding.reason_code,
            payload=event_payload,
        )
        await session.commit()

    _log.error(
        "worker.runtime_stranded_workspace_failed",
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        runtime_state=snapshot.stack_state,
        runtime_reason=snapshot.reason,
        reason_code=finding.reason_code,
    )


async def _record_recoverable_runtime_stranding(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    finding: WorkspaceRuntimeFinding,
) -> None:
    payload = _runtime_stranding_event_payload(candidate, snapshot, finding)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        if not _candidate_claim_is_stale(ws, candidate.status, datetime.now(UTC)):
            return
        if await self._has_runtime_stranding_event(
            session,
            candidate.workspace_id,
            finding.reason_code,
        ):
            return
        claims_will_clear = any(
            value is not None
            for value in (
                ws.execution_claimed_by,
                ws.execution_claim_expires_at,
                ws.monitor_claimed_by,
                ws.monitor_claim_expires_at,
            )
        )
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        ws.monitor_claimed_by = None
        ws.monitor_claim_expires_at = None
        if claims_will_clear:
            await repo.advance_workspace_version(ws)
        await repo.add_event(
            ws,
            event_type=RUNTIME_STRANDED_EVENT_TYPE,
            reason_code=finding.reason_code,
            payload=payload,
        )
        await session.commit()

    _log.warning(
        "worker.runtime_stranded_workspace_recoverable",
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        runtime_state=snapshot.stack_state,
        runtime_reason=snapshot.reason,
        reason_code=finding.reason_code,
        decision=finding.decision,
    )


async def _has_runtime_stranding_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    reason_code: str,
) -> bool:
    _ = self
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == RUNTIME_STRANDED_EVENT_TYPE,
            WorkspaceEvent.reason_code == reason_code,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
