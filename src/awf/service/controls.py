"""Shared workspace control operations for REST and MCP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceControlResponse
from awf.common.config import get_settings
from awf.common.ids import new_event_id
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.node.cleanup import (
    WorkspaceCleaner,
    WorkspaceCleanupResult,
    WorkspaceCleanupStatus,
    WorkspaceCleanupStepResult,
    WorkspaceCleanupStepStatus,
)
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager
from awf.service.failure_causality import (
    build_preserved_failure_payload,
    load_primary_failure_snapshot,
)
from awf.service.secret_leases import (
    TERMINAL_CLEANUP_REVOKE_REASON,
    SecretLeaseService,
    secret_lease_revocation_summary,
)
from awf.service.workspace_runtime_health import (
    OPERATOR_REFRESH_EVENT_TYPE,
    OPERATOR_REFRESH_REASON_CODE,
)

ProjectStopper = Callable[[str | None], Awaitable[None]]
CleanupResultLike = WorkspaceCleanupResult | Sequence[str] | Mapping[str, object]
CleanerFactory = Callable[[], "WorkspaceCleanerProtocol"]
_REMONITOR_ELIGIBLE_STATUSES = (
    WorkspaceStatus.monitoring_pr,
    WorkspaceStatus.failed,
)
_VALIDATE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_VALIDATE_REPLAY_STATUSES = frozenset(
    {
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
    }
)
_REBASE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_DESTROYING_OR_DESTROYED_STATUSES = frozenset(
    {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}
)
_REBASE_DESTRUCTIVE_CONFLICT_TYPES = frozenset(
    {
        OperationType.cancel.value,
        OperationType.stop.value,
        OperationType.destroy.value,
    }
)
_OPERATOR_API_SOURCE = "operator_api"
_OPERATOR_CANCEL_REASON_CODE = "OPERATOR_CANCEL"
_OPERATOR_STOP_REASON_CODE = "OPERATOR_STOP"
_OPERATOR_REMONITOR_REASON_CODE = "OPERATOR_REMONITOR"
_OPERATOR_VALIDATE_REASON_CODE = "OPERATOR_VALIDATE"
_OPERATOR_REBASE_REASON_CODE = "OPERATOR_REBASE"
_OPERATOR_DESTROY_REASON_CODE = "OPERATOR_DESTROY"
_AUDIT_CONTROL_OPERATION_EVENT = "workspace.audit.control_operation"
_OPERATION_ERROR_MESSAGE_MAX_LENGTH = 2048


class _PreparedOperationKind(StrEnum):
    exact_replay = "exact_replay"
    active_coalesce = "active_coalesce"


@dataclass(frozen=True)
class _PreparedOperation:
    workspace: Workspace
    replay: Operation | None = None
    kind: _PreparedOperationKind | None = None
    idempotency_key: str | None = None


class WorkspaceCleanerProtocol(Protocol):
    async def cleanup(  # pragma: no cover - Protocol method declaration only.
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> CleanupResultLike: ...


class WorkspaceControlError(Exception):
    """Base error for framework adapters to map into HTTP/MCP errors."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.detail = detail
        super().__init__(message)


class WorkspaceNotFoundError(WorkspaceControlError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            error_code="NOT_FOUND",
            message=f"No workspace with id {workspace_id}",
        )


class ActiveWorkspaceDestroyError(WorkspaceControlError):
    def __init__(self) -> None:
        super().__init__(
            error_code="WORKSPACE_ACTIVE",
            message="Active workspaces require force=true before destroy.",
        )


class IdempotencyConflictError(WorkspaceControlError):
    def __init__(self) -> None:
        super().__init__(
            error_code="IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key previously used with a different action payload.",
        )


class VersionConflictError(WorkspaceControlError):
    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        super().__init__(
            error_code="VERSION_CONFLICT",
            message="Workspace version does not match If-Match.",
            detail={
                "expected_version": expected_version,
                "actual_version": actual_version,
            },
        )


class WorkspaceStackStopError(WorkspaceControlError):
    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout).strip() or "<no output>"
        super().__init__(
            error_code="STACK_STOP_FAILED",
            message=f"docker {operation} failed (exit={returncode}): {detail}",
        )


class WorkspaceRemonitorMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace remonitor requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceRemonitorStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_REMONITORABLE",
            message="Workspace is not in a state eligible for remonitor recovery.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _REMONITOR_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceRefreshStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_REFRESHABLE",
            message="Workspace is not in a state eligible for refresh recovery.",
            detail={"status": workspace.status},
        )


class WorkspaceValidateStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_VALIDATABLE",
            message="Workspace is not in a state eligible for validate recovery.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _VALIDATE_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceValidateMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace validate requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceRebaseMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace rebase requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceRebaseMissingCandidateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="MERGE_CANDIDATE_NOT_FOUND",
            message="Workspace rebase requires an open merge candidate.",
            detail={"workspace_id": workspace.id, "pr_url": workspace.pr_url},
        )


class WorkspaceRebaseStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_REBASEABLE",
            message="Workspace is not in a state eligible for rebase recovery.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _REBASE_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceRebaseActiveConflictError(WorkspaceControlError):
    def __init__(
        self,
        operation: Operation,
        *,
        error_code: str = "WORKSPACE_REBASE_CONFLICT",
        message: str = "Workspace already has an active rebase operation.",
    ) -> None:
        super().__init__(
            error_code=error_code,
            message=message,
            detail=_operation_conflict_detail(operation),
        )


class WorkspaceControlService:
    """Business logic for sensitive workspace lifecycle controls."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        project_stopper: ProjectStopper,
        cleaner_factory: CleanerFactory,
    ) -> None:
        self._session = session
        self._project_stopper = project_stopper
        self._cleaner_factory = cleaner_factory

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        event_payload = _event_payload(
            {"reason": reason, "stop_stack": stop_stack},
            expected_version=expected_version,
        )
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_CANCEL_REASON_CODE,
            requested_action=OperationType.cancel.value,
            extra={"stop_stack": stop_stack},
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.cancel,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            return _control_response(
                workspace=workspace,
                operation=replay,
                message="workspace cancellation requested",
            )

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.cancel,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        if stop_stack:
            try:
                await self._project_stopper(workspace.compose_project_name)
            except WorkspaceStackStopError as exc:
                await _finish_stack_stop_failed_operation(
                    self._session,
                    operations,
                    operation,
                    workspace=workspace,
                    exc=exc,
                )
                raise
        if (
            workspace.status != WorkspaceStatus.cancelled.value
            and WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status),
                WorkspaceStatus.cancelled,
            )
        ):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_CANCEL_REASON_CODE,
                payload=event_payload,
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.cancel_requested",
                reason_code=_OPERATOR_CANCEL_REASON_CODE,
                payload=event_payload,
            )
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result={"status": workspace.status},
        )
        await _add_control_audit_event(
            repo,
            workspace,
            operation=operation,
            action=OperationType.cancel.value,
            outcome="succeeded",
            reason_code=_OPERATOR_CANCEL_REASON_CODE,
            extra={
                "stop_stack": stop_stack,
                "expected_version": expected_version,
            },
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message="workspace cancellation requested",
        )

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        event_payload = _event_payload(
            {"reason": reason},
            expected_version=expected_version,
        )
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_STOP_REASON_CODE,
            requested_action=OperationType.stop.value,
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.stop,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            return _control_response(
                workspace=workspace,
                operation=replay,
                message="workspace stack stopped",
            )

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.stop,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        try:
            await self._project_stopper(workspace.compose_project_name)
        except WorkspaceStackStopError as exc:
            await _finish_stack_stop_failed_operation(
                self._session,
                operations,
                operation,
                workspace=workspace,
                exc=exc,
            )
            raise
        if _is_active(WorkspaceStatus(workspace.status)) and WorkspaceStateMachine.can_transition(
            WorkspaceStatus(workspace.status),
            WorkspaceStatus.cancelled,
        ):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_STOP_REASON_CODE,
                payload=event_payload,
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.stack_stopped",
                reason_code=_OPERATOR_STOP_REASON_CODE,
                payload=event_payload,
            )
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result={"status": workspace.status},
        )
        await _add_control_audit_event(
            repo,
            workspace,
            operation=operation,
            action=OperationType.stop.value,
            outcome="succeeded",
            reason_code=_OPERATOR_STOP_REASON_CODE,
            extra={"expected_version": expected_version},
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message="workspace stack stopped",
        )

    async def remonitor_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        workspace_for_payload = await self._require_workspace(repo, workspace_id)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_REMONITOR_REASON_CODE,
            requested_action=OperationType.remonitor.value,
            extra=_workspace_pr_operation_context(workspace_for_payload),
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            return _control_response(
                workspace=workspace,
                operation=replay,
                message="workspace PR monitor recovery requested",
            )

        current = WorkspaceStatus(workspace.status)
        if current not in _REMONITOR_ELIGIBLE_STATUSES:
            raise WorkspaceRemonitorStateError(workspace)

        if not workspace.pr_url:
            raise WorkspaceRemonitorMissingPrUrlError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        claims_reset = _claim_reset_snapshot(workspace)
        state_reset = await _reset_failed_workspace_for_remonitor(
            self._session,
            workspace,
        )
        cancelled_recovery_operations = await _cancel_stale_pr_monitor_recovery_operations(
            operations,
            workspace_id=workspace.id,
        )
        workspace.monitor_claimed_by = None
        workspace.monitor_claim_expires_at = None
        workspace.execution_claimed_by = None
        workspace.execution_claim_expires_at = None
        workspace.version += 1
        event_payload: dict[str, object | None] = {
            "reason": reason,
            "operation_id": operation.id,
            "claims_reset": claims_reset,
        }
        event_payload = _event_payload(event_payload, expected_version=expected_version)
        if state_reset is not None:
            event_payload["state_reset"] = state_reset
        if cancelled_recovery_operations:
            event_payload["cancelled_recovery_operations"] = cancelled_recovery_operations
            event_payload["cancelled_recovery_reason_code"] = _OPERATOR_REMONITOR_REASON_CODE
            event_payload["cancelled_recovery_requested_action"] = OperationType.remonitor.value
        if state_reset is not None:
            workspace.events.append(
                WorkspaceEvent(
                    id=new_event_id(),
                    workspace_id=workspace.id,
                    event_type="workspace.remonitor_requested",
                    old_state=str(state_reset["from"]),
                    new_state=str(state_reset["to"]),
                    reason_code=_OPERATOR_REMONITOR_REASON_CODE,
                    payload=event_payload,
                )
            )
            await self._session.flush()
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.remonitor_requested",
                reason_code=_OPERATOR_REMONITOR_REASON_CODE,
                payload=event_payload,
            )
        result: dict[str, object | None] = {
            "status": workspace.status,
            "claims_reset": claims_reset,
            **_workspace_pr_operation_context(workspace),
        }
        if state_reset is not None:
            result["state_reset"] = state_reset
        if cancelled_recovery_operations:
            result["cancelled_recovery_operations"] = cancelled_recovery_operations
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result=result,
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message="workspace PR monitor recovery requested",
        )

    async def request_refresh_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Create or replay an operator refresh operation.

        Exact idempotency-key replays return the stored operation even if the
        workspace later enters destruction. Fresh-key active coalescing still
        observes current state eligibility, so it is rejected once destruction
        has started.
        """
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=OPERATOR_REFRESH_REASON_CODE,
            requested_action=OperationType.refresh.value,
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=payload,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            if (
                prepared.kind != _PreparedOperationKind.exact_replay
                and WorkspaceStatus(workspace.status) in _DESTROYING_OR_DESTROYED_STATUSES
            ):
                raise WorkspaceRefreshStateError(workspace)
            return replay
        if WorkspaceStatus(workspace.status) in _DESTROYING_OR_DESTROYED_STATUSES:
            raise WorkspaceRefreshStateError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.pending,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        await repo.add_event(
            workspace,
            event_type=OPERATOR_REFRESH_EVENT_TYPE,
            reason_code=OPERATOR_REFRESH_REASON_CODE,
            payload=_event_payload(
                {
                    "source": _OPERATOR_API_SOURCE,
                    "reason": reason,
                    "operation_id": operation.id,
                },
                expected_version=expected_version,
            ),
        )
        return operation

    async def request_validate_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_VALIDATE_REASON_CODE,
            requested_action=OperationType.validate.value,
            extra={"recovery_mode": "validate_only"},
        )
        if requested_tier is not None:
            payload["requested_tier"] = requested_tier
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=payload,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        current = WorkspaceStatus(workspace.status)
        if replay is not None:
            if (
                prepared.kind != _PreparedOperationKind.exact_replay
                and current not in _VALIDATE_REPLAY_STATUSES
            ):
                raise WorkspaceValidateStateError(workspace)
            return replay
        if current not in _VALIDATE_ELIGIBLE_STATUSES:
            raise WorkspaceValidateStateError(workspace)
        if not workspace.pr_url:
            raise WorkspaceValidateMissingPrUrlError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        validate_event_payload: dict[str, object | None] = {
            "source": _OPERATOR_API_SOURCE,
            "reason": reason,
            "operation_id": operation.id,
            "recovery_mode": "validate_only",
        }
        if requested_tier is not None:
            validate_event_payload["requested_tier"] = requested_tier
        validate_event_payload = _event_payload(
            validate_event_payload,
            expected_version=expected_version,
        )
        await repo.add_event(
            workspace,
            event_type="workspace.validate_requested",
            reason_code=_OPERATOR_VALIDATE_REASON_CODE,
            payload=validate_event_payload,
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code=_OPERATOR_VALIDATE_REASON_CODE,
            payload=validate_event_payload,
        )
        return operation

    async def request_rebase_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        base_payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_REBASE_REASON_CODE,
            requested_action=OperationType.rebase.value,
            extra={"recovery_mode": "rebase_only"},
        )
        idempotency_payload = _operation_payload(
            base_payload,
            expected_version=expected_version,
        )
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            payload=idempotency_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=base_payload,
            idempotency_payload_identity=idempotency_payload,
            idempotency_identity_keys=frozenset({*base_payload.keys(), "expected_version"}),
        )
        workspace = prepared.workspace
        replay = prepared.replay
        current = WorkspaceStatus(workspace.status)
        if replay is not None:
            if (
                prepared.kind != _PreparedOperationKind.exact_replay
                and current not in _REBASE_ELIGIBLE_STATUSES
            ):
                raise WorkspaceRebaseStateError(workspace)
            return replay

        destructive_conflict = await _find_active_operation(
            operations,
            workspace_id=workspace_id,
            operation_types=_REBASE_DESTRUCTIVE_CONFLICT_TYPES,
        )
        if destructive_conflict is not None:
            raise WorkspaceRebaseActiveConflictError(
                destructive_conflict,
                error_code="WORKSPACE_OPERATION_CONFLICT",
                message=("Workspace rebase conflicts with an active destructive operation."),
            )

        active_rebase = await _find_active_operation(
            operations,
            workspace_id=workspace_id,
            operation_types={OperationType.rebase.value},
        )
        if active_rebase is not None:
            raise WorkspaceRebaseActiveConflictError(active_rebase)

        if current not in _REBASE_ELIGIBLE_STATUSES:
            raise WorkspaceRebaseStateError(workspace)
        if not workspace.pr_url:
            raise WorkspaceRebaseMissingPrUrlError(workspace)

        candidate = await MergeCandidateRepository(
            self._session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        if candidate is None:
            raise WorkspaceRebaseMissingCandidateError(workspace)

        rebase_payload = _operation_payload(
            {
                **base_payload,
                **_workspace_rebase_operation_context(workspace, candidate),
            },
            expected_version=expected_version,
        )
        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            status=OperationStatus.pending,
            payload=rebase_payload,
            idempotency_key=prepared.idempotency_key,
        )
        rebase_event_payload = _event_payload(
            {
                "source": _OPERATOR_API_SOURCE,
                "reason": reason,
                "operation_id": operation.id,
                "recovery_mode": "rebase_only",
                "candidate_id": candidate.id,
            },
            expected_version=expected_version,
        )
        await repo.add_event(
            workspace,
            event_type="workspace.rebase_requested",
            reason_code=_OPERATOR_REBASE_REASON_CODE,
            payload=rebase_event_payload,
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code=_OPERATOR_REBASE_REASON_CODE,
            payload=rebase_event_payload,
        )
        return operation

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=None,
            reason_code=_OPERATOR_DESTROY_REASON_CODE,
            requested_action=OperationType.destroy.value,
            extra={
                "force": force,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            },
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        event_payload = _event_payload(
            {
                "force": force,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            },
            expected_version=expected_version,
        )
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.destroy,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            message = (
                "workspace already destroyed"
                if workspace.status == WorkspaceStatus.destroyed.value
                else "workspace destroy requested"
            )
            return _control_response(workspace=workspace, operation=replay, message=message)

        current = WorkspaceStatus(workspace.status)
        if _is_active(current) and not force:
            raise ActiveWorkspaceDestroyError()

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.destroy,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        secret_lease_summary = await self._revoke_destroy_secret_leases(workspace)
        if current == WorkspaceStatus.destroyed:
            await ResourceReservationRepository(self._session).release_active_for_workspace(
                workspace_id
            )
            cleanup_result = WorkspaceCleanupResult.skipped(
                reason_code="WORKSPACE_ALREADY_DESTROYED"
            )
            cleanup_payload = cleanup_result.to_dict()
            operation_result = _with_secret_lease_result(
                {
                    "status": WorkspaceStatus.destroyed.value,
                    "cleanup": cleanup_payload,
                },
                secret_lease_summary,
            )
            await operations.finish(
                operation,
                status=OperationStatus.succeeded,
                result=operation_result,
            )
            audit_evidence = _with_secret_lease_evidence(
                {"cleanup": cleanup_payload},
                secret_lease_summary,
            )
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="skipped",
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            return _control_response(
                workspace=workspace,
                operation=operation,
                message="workspace already destroyed",
            )

        if _is_active(current) and WorkspaceStateMachine.can_transition(
            current, WorkspaceStatus.cancelled
        ):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                payload=event_payload,
            )
            current = WorkspaceStatus.cancelled
        if WorkspaceStateMachine.can_transition(current, WorkspaceStatus.destroying):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroying,
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                payload=event_payload,
            )

        await self._session.flush()
        cleaner = self._cleaner_factory()
        cleanup_result = _normalize_cleanup_result(
            await cleaner.cleanup(
                workspace_id=workspace_id,
                repo_url=workspace.repo_url,
                compose_project_name=workspace.compose_project_name,
                compose_file_path=(
                    Path(workspace.compose_file_path) if workspace.compose_file_path else None
                ),
                worktree_host_path=None,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        cleanup_payload = cleanup_result.to_dict()
        await self._session.refresh(workspace)
        requested_status = (
            WorkspaceStatus.failed if not cleanup_result.ok else WorkspaceStatus.destroyed
        )
        if (
            workspace.status != WorkspaceStatus.destroying.value
            and WorkspaceStateMachine.is_callback_terminal(WorkspaceStatus(workspace.status))
        ):
            ignored_event = await repo.record_ignored_stale_callback(
                workspace,
                callback_source="service.controls",
                callback_action="destroy_cleanup",
                expected_status=WorkspaceStatus.destroying,
                requested_status=requested_status,
                operation_id=operation.id,
                reason_code="STALE_CALLBACK_IGNORED",
            )
            ignored_payload = dict(ignored_event.payload or {})
            operation_result = _with_secret_lease_result(
                {
                    "status": workspace.status,
                    "cleanup": cleanup_payload,
                    "ignored_callback": ignored_payload,
                },
                secret_lease_summary,
            )
            await operations.finish(
                operation,
                status=OperationStatus.cancelled,
                result=operation_result,
            )
            audit_evidence = _with_secret_lease_evidence(
                {
                    "cleanup": cleanup_payload,
                    "ignored_callback": ignored_payload,
                },
                secret_lease_summary,
            )
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="skipped",
                reason_code="STALE_CALLBACK_IGNORED",
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            return _control_response(
                workspace=workspace,
                operation=operation,
                message="workspace destroy callback ignored",
            )
        cleanup_event_payload = _event_payload(
            {
                **event_payload,
                "cleanup": cleanup_payload,
            },
            expected_version=None,
        )
        if not cleanup_result.ok:
            cleanup_message = _cleanup_failure_message(cleanup_result)
            bounded_cleanup_message = _bounded_operation_error_message(cleanup_message)
            primary_failure = await load_primary_failure_snapshot(self._session, workspace)
            secondary_failure = {
                "failure_reason": "cleanup_failure",
                "reason_code": "CLEANUP_FAILED",
                "message": bounded_cleanup_message,
                "cleanup": cleanup_payload,
            }
            failed_transition_payload = (
                build_preserved_failure_payload(
                    primary_failure,
                    secondary_failure=secondary_failure,
                    extra=cleanup_event_payload,
                )
                if primary_failure is not None
                else cleanup_event_payload
            )
            if primary_failure is None:
                workspace.failure_reason = "cleanup_failure"
                workspace.failure_message = bounded_cleanup_message
            if WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status), WorkspaceStatus.failed
            ):
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.failed,
                    reason_code=(
                        str(primary_failure.get("reason_code"))
                        if primary_failure is not None and primary_failure.get("reason_code")
                        else "CLEANUP_FAILED"
                    ),
                    payload=failed_transition_payload,
                )
            result_payload: dict[str, Any] = {
                "status": workspace.status,
                "cleanup": cleanup_payload,
            }
            if primary_failure is not None:
                result_payload["primary_failure"] = primary_failure
                result_payload["secondary_failure"] = secondary_failure
            operation_result = _with_secret_lease_result(result_payload, secret_lease_summary)
            await operations.finish(
                operation,
                status=OperationStatus.failed,
                error_code="CLEANUP_FAILED",
                error_message=bounded_cleanup_message,
                result=operation_result,
            )
            audit_payload: dict[str, Any] = {
                "cleanup": cleanup_payload,
                "error_message": bounded_cleanup_message,
            }
            if primary_failure is not None:
                audit_payload["primary_failure"] = primary_failure
                audit_payload["secondary_failure"] = secondary_failure
            audit_evidence = _with_secret_lease_evidence(audit_payload, secret_lease_summary)
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="failed",
                reason_code="CLEANUP_FAILED",
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            message = "workspace cleanup failed"
        else:
            if WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status), WorkspaceStatus.destroyed
            ):
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.destroyed,
                    reason_code="DESTROYED",
                    payload=cleanup_event_payload,
                )
            operation_result = _with_secret_lease_result(
                {"status": workspace.status, "cleanup": cleanup_payload},
                secret_lease_summary,
            )
            await operations.finish(
                operation,
                status=OperationStatus.succeeded,
                result=operation_result,
            )
            audit_evidence = _with_secret_lease_evidence(
                {"cleanup": cleanup_payload},
                secret_lease_summary,
            )
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="succeeded",
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            message = "workspace destroyed"

        return _control_response(
            workspace=workspace,
            operation=operation,
            message=message,
        )

    async def _require_workspace(
        self,
        repo: WorkspaceRepository,
        workspace_id: str,
    ) -> Workspace:
        workspace = await repo.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def _revoke_destroy_secret_leases(
        self,
        workspace: Workspace,
    ) -> dict[str, Any] | None:
        revoked = await SecretLeaseService(self._session).revoke_workspace_secret_leases(
            workspace,
            now=datetime.now(UTC),
            reason_code=TERMINAL_CLEANUP_REVOKE_REASON,
        )
        if not revoked:
            return None
        return secret_lease_revocation_summary(
            revoked,
            reason_code=TERMINAL_CLEANUP_REVOKE_REASON,
        )

    async def _require_workspace_for_update(
        self,
        repo: WorkspaceRepository,
        workspace_id: str,
    ) -> Workspace:
        workspace = await repo.get_for_update(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def _prepare_operation(
        self,
        repo: WorkspaceRepository,
        operations: OperationRepository,
        *,
        workspace_id: str,
        operation_type: OperationType,
        payload: dict[str, object | None],
        idempotency_key: str | None,
        expected_version: int | None,
        active_payload_identity: dict[str, object | None] | None = None,
        idempotency_payload_identity: dict[str, object | None] | None = None,
        idempotency_identity_keys: frozenset[str] | None = None,
    ) -> _PreparedOperation:
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip() or None
        if idempotency_key is not None:
            await operations.acquire_idempotency_key_lock(idempotency_key)
            existing = await operations.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                payload_matches = (
                    _payload_matches_idempotency_identity(
                        existing.payload,
                        identity=idempotency_payload_identity,
                        identity_keys=idempotency_identity_keys,
                    )
                    if idempotency_payload_identity is not None
                    else existing.payload == payload
                )
                if (
                    existing.workspace_id != workspace_id
                    or existing.type != operation_type.value
                    or not payload_matches
                ):
                    raise IdempotencyConflictError()
                workspace = await self._require_workspace(repo, workspace_id)
                return _PreparedOperation(
                    workspace=workspace,
                    replay=existing,
                    kind=_PreparedOperationKind.exact_replay,
                    idempotency_key=idempotency_key,
                )

        workspace = await self._require_workspace_for_update(repo, workspace_id)
        if expected_version is not None and workspace.version != expected_version:
            raise VersionConflictError(
                expected_version=expected_version,
                actual_version=workspace.version,
            )
        if active_payload_identity is not None:
            active = await operations.find_active_matching_payload(
                workspace_id=workspace_id,
                operation_type=operation_type,
                payload_identity=active_payload_identity,
            )
            if active is not None:
                return _PreparedOperation(
                    workspace=workspace,
                    replay=active,
                    kind=_PreparedOperationKind.active_coalesce,
                    idempotency_key=idempotency_key,
                )
        return _PreparedOperation(workspace=workspace, idempotency_key=idempotency_key)


async def stop_project_containers(compose_project_name: str | None) -> None:
    if not compose_project_name:
        return
    proc = await _docker_process(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={compose_project_name}",
        operation="ps",
    )
    stdout, _stderr = await _communicate(proc, operation="ps")
    ids = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not ids:
        return
    stop = await _docker_process(
        "stop",
        *ids,
        operation="stop",
    )
    await _communicate(stop, operation="stop")


async def _docker_process(*args: str, operation: str) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise WorkspaceStackStopError(
            operation=operation,
            returncode=127,
            stdout="",
            stderr=f"docker executable is not available: {exc}",
        ) from exc
    except OSError as exc:
        raise WorkspaceStackStopError(
            operation=operation,
            returncode=1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        ) from exc


async def _communicate(
    proc: asyncio.subprocess.Process,
    *,
    operation: str,
) -> tuple[str, str]:
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    assert proc.returncode is not None
    if proc.returncode != 0:
        raise WorkspaceStackStopError(
            operation=operation,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return stdout, stderr


def default_cleaner() -> WorkspaceCleaner:
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    template = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
    return WorkspaceCleaner(
        git=GitManager(work_dir / "git"),
        compose=ComposeManager(work_dir=work_dir, template_path=template),
    )


async def _reset_failed_workspace_for_remonitor(
    session: AsyncSession,
    workspace: Workspace,
) -> dict[str, object] | None:
    if workspace.status != WorkspaceStatus.failed.value:
        return None

    old_status = workspace.status
    old_iter_count = workspace.monitor_iter_count
    workspace.status = WorkspaceStatus.monitoring_pr.value
    workspace.failure_reason = None
    workspace.failure_message = None
    workspace.monitor_iter_count = 0

    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    candidate_reopened = False
    if attempt is not None:
        attempt.status = workspace.status
        candidate_repo = MergeCandidateRepository(session)
        candidate = await candidate_repo.get_by_attempt_id(attempt.id)
        if candidate is not None:
            await candidate_repo.create_or_update_open_for_attempt(
                task=candidate.task,
                attempt=candidate.attempt,
                workspace=workspace,
                head_sha=workspace.monitor_last_commit_sha,
                base_sha=workspace.base_commit,
            )
            candidate_reopened = True

    return {
        "from": old_status,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": old_iter_count,
        "candidate_reopened": candidate_reopened,
    }


async def _cancel_stale_pr_monitor_recovery_operations(
    operations: OperationRepository,
    *,
    workspace_id: str,
) -> list[dict[str, object]]:
    cancelled: list[dict[str, object]] = []
    active: list[Operation] = []
    for status in (OperationStatus.pending, OperationStatus.running):
        active.extend(
            await operations.list_for_workspace(
                workspace_id,
                status=status,
                limit=100,
            )
        )

    for operation in active:
        if not _is_pr_monitor_recovery_operation(operation):
            continue
        cancelled.append(_operation_conflict_detail(operation))
        await operations.finish(
            operation,
            status=OperationStatus.cancelled,
            result={
                "status": OperationStatus.cancelled.value,
                "reason_code": _OPERATOR_REMONITOR_REASON_CODE,
                "requested_action": OperationType.remonitor.value,
            },
            error_code=_OPERATOR_REMONITOR_REASON_CODE,
            error_message=(
                "Cancelled stale PR monitor recovery operation before operator remonitor."
            ),
        )
    return cancelled


def _is_pr_monitor_recovery_operation(operation: Operation) -> bool:
    if operation.type not in {
        OperationType.validate.value,
        OperationType.rebase.value,
    }:
        return False
    payload = operation.payload
    if not isinstance(payload, Mapping):
        return False
    return payload.get("source") == "pr_monitor" and payload.get("recovery_mode") in {
        "validate_only",
        "rebase_only",
    }


def _control_response(
    *,
    workspace: Workspace,
    operation: Operation,
    message: str,
) -> WorkspaceControlResponse:
    return WorkspaceControlResponse(
        workspace_id=workspace.id,
        operation_id=operation.id,
        operation_status=operation.status,
        status=WorkspaceStatus(workspace.status),
        message=message,
    )


def _operator_operation_payload(
    *,
    reason: str | None,
    reason_code: str,
    requested_action: str,
    extra: dict[str, object | None] | None = None,
) -> dict[str, object | None]:
    payload: dict[str, object | None] = {
        "owner": _OPERATOR_API_SOURCE,
        "source": _OPERATOR_API_SOURCE,
        "reason": reason,
        "reason_code": reason_code,
        "requested_action": requested_action,
    }
    if extra is not None:
        payload.update(extra)
    return payload


def _operation_payload(
    payload: dict[str, object | None],
    *,
    expected_version: int | None,
) -> dict[str, object | None]:
    operation_payload = dict(payload)
    if expected_version is not None:
        operation_payload["expected_version"] = expected_version
    return operation_payload


def _with_secret_lease_result(
    result: dict[str, Any],
    secret_lease_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if secret_lease_summary is None:
        return result
    return {**result, "secret_leases": secret_lease_summary}


def _with_secret_lease_evidence(
    evidence: dict[str, Any],
    secret_lease_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if secret_lease_summary is None:
        return evidence
    return {**evidence, "lease_revocations": secret_lease_summary}


def _workspace_pr_operation_context(workspace: Workspace) -> dict[str, object | None]:
    return {
        key: value
        for key, value in {
            "pr_number": workspace.pr_number,
            "pr_url": workspace.pr_url,
            "source_head_sha": workspace.monitor_last_commit_sha,
            "source_base_sha": workspace.base_commit,
        }.items()
        if value is not None
    }


def _workspace_rebase_operation_context(
    workspace: Workspace,
    candidate: object,
) -> dict[str, object | None]:
    candidate_head_sha = getattr(candidate, "head_sha", None)
    candidate_base_sha = getattr(candidate, "base_sha", None)
    return {
        key: value
        for key, value in {
            "candidate_id": getattr(candidate, "id", None),
            "attempt_id": getattr(candidate, "attempt_id", None),
            "task_id": getattr(candidate, "task_id", None),
            "pr_number": getattr(candidate, "pr_number", None) or workspace.pr_number,
            "pr_url": getattr(candidate, "pr_url", None) or workspace.pr_url,
            "source_head_sha": candidate_head_sha or workspace.monitor_last_commit_sha,
            "source_base_sha": candidate_base_sha or workspace.base_commit,
            "target_branch": workspace.branch_base,
            "remote_branch": workspace.remote_push_branch or workspace.branch_name,
        }.items()
        if value is not None
    }


def _event_payload(
    payload: dict[str, object | None],
    *,
    expected_version: int | None,
) -> dict[str, object | None]:
    event_payload = dict(payload)
    if expected_version is not None:
        event_payload["expected_version"] = expected_version
    return event_payload


def _normalize_cleanup_result(result: CleanupResultLike) -> WorkspaceCleanupResult:
    if isinstance(result, WorkspaceCleanupResult):
        return result
    if isinstance(result, Mapping):
        return _cleanup_result_from_mapping(result)
    failures = [str(item) for item in result]
    if not failures:
        return WorkspaceCleanupResult.from_steps([])
    return WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name=failure,
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error=failure,
            )
            for failure in failures
        ]
    )


def _cleanup_result_from_mapping(result: Mapping[str, object]) -> WorkspaceCleanupResult:
    status = _cleanup_status(result.get("status"))
    reason_code = _cleanup_reason_code(result.get("reason_code"), status=status)
    steps = tuple(_cleanup_steps_from_mapping(result))
    return WorkspaceCleanupResult(
        status=status,
        reason_code=reason_code,
        steps=steps,
    )


def _cleanup_steps_from_mapping(
    result: Mapping[str, object],
) -> list[WorkspaceCleanupStepResult]:
    raw_steps = result.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str):
        raw_failed_steps = result.get("failed_steps")
        raw_completed_steps = result.get("completed_steps")
        failed_steps = (
            raw_failed_steps
            if isinstance(raw_failed_steps, Sequence) and not isinstance(raw_failed_steps, str)
            else ()
        )
        completed_steps = (
            raw_completed_steps
            if isinstance(raw_completed_steps, Sequence)
            and not isinstance(raw_completed_steps, str)
            else ()
        )
        raw_steps = (*completed_steps, *failed_steps)
    steps: list[WorkspaceCleanupStepResult] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        step_status = _cleanup_step_status(raw_step.get("status"))
        steps.append(
            WorkspaceCleanupStepResult(
                name=_cleanup_string(raw_step.get("name"), fallback=f"cleanup_step_{index + 1}"),
                status=step_status,
                reason_code=_cleanup_string(
                    raw_step.get("reason_code"),
                    fallback=(
                        "CLEANUP_STEP_FAILED"
                        if step_status == "failed"
                        else "CLEANUP_STEP_SUCCEEDED"
                    ),
                ),
                error=_cleanup_optional_string(raw_step.get("error")),
            )
        )
    return steps


def _cleanup_status(value: object) -> WorkspaceCleanupStatus:
    if value in {"succeeded", "partial", "skipped"}:
        return cast(WorkspaceCleanupStatus, value)
    return "partial"


def _cleanup_step_status(value: object) -> WorkspaceCleanupStepStatus:
    if value in {"succeeded", "failed", "skipped"}:
        return cast(WorkspaceCleanupStepStatus, value)
    return "failed"


def _cleanup_reason_code(value: object, *, status: str) -> str:
    if isinstance(value, str) and value:
        return value
    if status == "succeeded":
        return "CLEANUP_SUCCEEDED"
    if status == "skipped":
        return "CLEANUP_SKIPPED"
    return "CLEANUP_PARTIAL"


def _cleanup_string(value: object, *, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _cleanup_optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _cleanup_failure_message(cleanup_result: WorkspaceCleanupResult) -> str:
    failures = cleanup_result.failure_messages
    if failures:
        return ", ".join(failures)
    return cleanup_result.reason_code


async def _finish_stack_stop_failed_operation(
    session: AsyncSession,
    operations: OperationRepository,
    operation: Operation,
    *,
    workspace: Workspace,
    exc: WorkspaceStackStopError,
) -> None:
    repo = WorkspaceRepository(session)
    operation_payload = operation.payload if isinstance(operation.payload, dict) else {}
    await operations.finish(
        operation,
        status=OperationStatus.failed,
        result={"status": workspace.status},
        error_code=exc.error_code,
        error_message=_bounded_operation_error_message(exc.message),
    )
    await _add_control_audit_event(
        repo,
        workspace,
        operation=operation,
        action=operation.type,
        outcome="failed",
        reason_code=exc.error_code,
        extra={
            "stop_stack": operation_payload.get("stop_stack"),
            "expected_version": operation_payload.get("expected_version"),
        },
        evidence={
            "operation": f"docker {exc.operation}",
            "returncode": exc.returncode,
            "error_message": _bounded_operation_error_message(exc.message),
        },
    )
    await session.commit()


def _bounded_operation_error_message(message: str) -> str:
    return message[:_OPERATION_ERROR_MESSAGE_MAX_LENGTH]


async def _add_control_audit_event(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    operation: Operation,
    action: str,
    outcome: str,
    reason_code: str,
    extra: Mapping[str, object | None] | None = None,
    evidence: Mapping[str, object | None] | None = None,
) -> None:
    await repo.add_audit_event(
        workspace,
        event_type=_AUDIT_CONTROL_OPERATION_EVENT,
        actor=_OPERATOR_API_SOURCE,
        source=_OPERATOR_API_SOURCE,
        action=action,
        outcome=outcome,
        reason_code=reason_code,
        operation_id=operation.id,
        operation_type=operation.type,
        extra=extra,
        evidence=evidence,
    )


def _claim_reset_snapshot(workspace: Workspace) -> dict[str, str | None]:
    return {
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": _json_datetime(workspace.monitor_claim_expires_at),
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": _json_datetime(workspace.execution_claim_expires_at),
    }


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


async def _find_active_operation(
    operations: OperationRepository,
    *,
    workspace_id: str,
    operation_types: frozenset[str] | set[str],
) -> Operation | None:
    active: list[Operation] = []
    for status in (OperationStatus.pending, OperationStatus.running):
        active.extend(
            await operations.list_for_workspace(
                workspace_id,
                status=status,
                limit=100,
            )
        )
    active.sort(key=lambda operation: (operation.created_at, operation.id))
    for operation in active:
        if operation.type in operation_types:
            return operation
    return None


def _operation_conflict_detail(operation: Operation) -> dict[str, object]:
    return {
        "operation_id": operation.id,
        "operation_type": operation.type,
        "operation_status": operation.status,
    }


def _payload_matches_idempotency_identity(
    payload: object,
    *,
    identity: dict[str, object | None] | None,
    identity_keys: frozenset[str] | None,
) -> bool:
    if identity is None:
        return True
    if not isinstance(payload, dict):
        return False
    keys = identity_keys if identity_keys is not None else frozenset(identity)
    for key in keys:
        if key not in identity:
            continue
        if key not in payload or payload[key] != identity[key]:
            return False
    return True


def _is_active(status_value: WorkspaceStatus) -> bool:
    return status_value in {
        WorkspaceStatus.requested,
        WorkspaceStatus.provisioning,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.monitoring_pr,
    }
