"""Shared workspace control operations for REST and MCP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar, cast
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceControlResponse
from awf.common.audit import redact_audit_text, redact_audit_value
from awf.common.config import Settings, get_settings
from awf.common.ids import new_event_id
from awf.common.logging import get_logger
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    EXTERNAL_RUNTIME_TEARDOWN_OPERATION_TIMEOUT_SECONDS,
    MergeCandidateRepository,
    OperationRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
    WorkspaceTransitionBlockedByActiveOperationError,
    WorkspaceTransitionStaleError,
    external_runtime_teardown_operation_blocks_controls,
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
from awf.service.secret_leases import (
    TERMINAL_CLEANUP_REVOKE_REASON,
    SecretLeaseService,
    secret_lease_revocation_summary,
)
from awf.service.terminal_runtime import (
    TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX,
    TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS,
    TERMINAL_RUNTIME_RELEASE_EXCEPTION_REASON_CODE,
    record_terminal_runtime_release_event,
    terminal_runtime_release_claim_active,
)
from awf.service.workspace_runtime_health import (
    OPERATOR_REFRESH_EVENT_TYPE,
    OPERATOR_REFRESH_REASON_CODE,
)

# Fast pre-cleanup stop hook. Implementations must be non-destructive: terminal
# runtime cleanup is responsible for removing containers/networks while preserving
# volumes, worktrees, logs, and other salvage evidence.
ProjectStopper = Callable[[str | None], Awaitable[None]]
CleanupResultLike = WorkspaceCleanupResult | Sequence[str] | Mapping[str, object]
CleanerFactory = Callable[[], "WorkspaceCleanerProtocol"]
ControlSessionFactory = async_sessionmaker[AsyncSession]
_log = get_logger(__name__)
_T = TypeVar("_T")
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
_RESOURCE_RESERVATION_RELEASE_STATUSES = frozenset(
    {
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    }
)
_RUNTIME_TEARDOWN_OPERATION_TYPES: Final[frozenset[str]] = frozenset(
    {
        OperationType.cancel.value,
        OperationType.stop.value,
        OperationType.destroy.value,
    }
)
_REBASE_DESTRUCTIVE_CONFLICT_TYPES: Final[frozenset[str]] = _RUNTIME_TEARDOWN_OPERATION_TYPES
_OPERATOR_API_SOURCE = "operator_api"
_OPERATOR_CANCEL_REASON_CODE = "OPERATOR_CANCEL"
_OPERATOR_STOP_REASON_CODE = "OPERATOR_STOP"
_OPERATOR_REMONITOR_REASON_CODE = "OPERATOR_REMONITOR"
_OPERATOR_VALIDATE_REASON_CODE = "OPERATOR_VALIDATE"
_OPERATOR_REBASE_REASON_CODE = "OPERATOR_REBASE"
_OPERATOR_DESTROY_REASON_CODE = "OPERATOR_DESTROY"
_AUDIT_CONTROL_OPERATION_EVENT = "workspace.audit.control_operation"
_CONTROL_OPERATION_FAILED_REASON_CODE = "CONTROL_OPERATION_FAILED"
_OPERATION_ERROR_MESSAGE_MAX_LENGTH = 2048
_TEARDOWN_OPERATION_HEARTBEAT_STOPPED_MESSAGE = (
    "teardown operation lease heartbeat stopped before external runtime work completed"
)
_RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS: Final = (
    EXTERNAL_RUNTIME_TEARDOWN_OPERATION_TIMEOUT_SECONDS / 3
)
_STALE_ACTIVE_EXECUTION_CLEANUP_CLAIM_OWNER_PREFIX: Final = "stale-cleanup:"


class _PreparedOperationKind(StrEnum):
    exact_replay = "exact_replay"
    active_coalesce = "active_coalesce"


@dataclass(frozen=True)
class _PreparedOperation:
    workspace: Workspace
    replay: Operation | None = None
    resume: Operation | None = None
    kind: _PreparedOperationKind | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class _ControlTerminalRuntimeCleanup:
    cleanup: WorkspaceCleanupResult
    preserved_worktree_host_path: Path | None
    claim_owner_id: str | None = None


@dataclass(frozen=True)
class _ControlExternalRuntimeStop:
    cleanup: _ControlTerminalRuntimeCleanup | None
    stop_error: Exception | None = None


@dataclass(frozen=True)
class _ControlTerminalRuntimeWorkspaceSnapshot:
    workspace_id: str
    repo_url: str
    compose_project_name: str | None
    compose_file_path: str | None


@dataclass(frozen=True)
class _ControlTerminalRuntimeReleaseClaimFailure:
    reason_code: str
    error: str | None = None


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


class WorkspaceRemonitorTerminalRuntimeReleaseInProgressError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_TERMINAL_RUNTIME_RELEASE_IN_PROGRESS",
            message="Workspace terminal runtime release is still cleaning up.",
            detail={"status": workspace.status},
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


class WorkspaceActiveOperationConflictError(WorkspaceControlError):
    def __init__(self, operation: Operation | None) -> None:
        super().__init__(
            error_code="WORKSPACE_OPERATION_CONFLICT",
            message="Workspace operation conflicts with active runtime teardown.",
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
        worktrees_root: Path | None = None,
        session_factory: ControlSessionFactory | None = None,
    ) -> None:
        self._session = session
        self._project_stopper = project_stopper
        self._cleaner_factory = cleaner_factory
        self._worktrees_root = worktrees_root
        self._session_factory = session_factory

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

        operation = prepared.resume
        if operation is None:
            operation = await operations.create(
                workspace_id=workspace_id,
                operation_type=OperationType.cancel,
                status=OperationStatus.running,
                payload=operation_payload,
                idempotency_key=prepared.idempotency_key,
            )
        operation_id = operation.id
        terminal_runtime_snapshot = (
            _snapshot_terminal_runtime_workspace_for_control(workspace) if stop_stack else None
        )
        committed_before_external_io = False
        terminal_runtime_cleanup: _ControlTerminalRuntimeCleanup | None = None
        if stop_stack:
            assert terminal_runtime_snapshot is not None
            await self._commit_before_external_runtime_io()
            committed_before_external_io = True
            terminal_runtime_stop_error: Exception | None = None
            try:
                terminal_runtime_stop = await self._run_with_teardown_operation_heartbeat(
                    operation_id,
                    self._stop_external_runtime_for_control(terminal_runtime_snapshot),
                )
                terminal_runtime_cleanup = terminal_runtime_stop.cleanup
                terminal_runtime_stop_error = terminal_runtime_stop.stop_error
            except WorkspaceStackStopError as exc:
                operation = await _require_control_operation(operations, operation_id)
                workspace = await self._require_workspace(repo, workspace_id)
                await _finish_stack_stop_failed_operation(
                    self._session,
                    operations,
                    operation,
                    workspace=workspace,
                    exc=exc,
                )
                raise
            except asyncio.CancelledError:
                await self._preserve_precommitted_cancelled_operation(operation_id)
                raise
            except Exception as exc:
                await _finish_precommitted_control_operation_failed(
                    self._session,
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    exc=exc,
                )
                raise
            if terminal_runtime_stop_error is not None:
                await self._finish_external_runtime_stop_error_for_control(
                    operations,
                    repo,
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    exc=terminal_runtime_stop_error,
                    terminal_runtime_cleanup=terminal_runtime_cleanup,
                )
                raise terminal_runtime_stop_error
        response: WorkspaceControlResponse | None = None
        try:
            operation = await _require_control_operation(operations, operation_id)
            if stop_stack:
                workspace = await self._require_workspace_for_update(repo, workspace_id)
                if conflict := _workspace_version_conflict(workspace, expected_version):
                    if terminal_runtime_cleanup is not None:
                        await self._record_terminal_runtime_release_for_control(
                            workspace,
                            release=terminal_runtime_cleanup,
                            source="service.controls.cancel",
                            require_terminal_status=False,
                        )
                        await self._release_terminal_runtime_claim_for_control(
                            workspace,
                            release=terminal_runtime_cleanup,
                        )
                    await _finish_version_conflict_operation(
                        self._session,
                        operations,
                        operation,
                        workspace=workspace,
                        exc=conflict,
                    )
                    raise conflict
            if (
                workspace.status != WorkspaceStatus.cancelled.value
                and WorkspaceStateMachine.can_transition(
                    WorkspaceStatus(workspace.status),
                    WorkspaceStatus.cancelled,
                )
            ):
                await _transition_workspace_for_control(
                    repo,
                    workspace,
                    to=WorkspaceStatus.cancelled,
                    reason_code=_OPERATOR_CANCEL_REASON_CODE,
                    payload=event_payload,
                    allow_active_operation_id=operation_id,
                )
            else:
                await repo.add_event(
                    workspace,
                    event_type="workspace.cancel_requested",
                    reason_code=_OPERATOR_CANCEL_REASON_CODE,
                    payload=event_payload,
                )
                await _release_active_resource_reservation_for_control(
                    self._session,
                    workspace,
                )
            if terminal_runtime_cleanup is not None:
                await self._record_terminal_runtime_release_for_control(
                    workspace,
                    release=terminal_runtime_cleanup,
                    source="service.controls.cancel",
                )
                await self._release_terminal_runtime_claim_for_control(
                    workspace,
                    release=terminal_runtime_cleanup,
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
            response = _control_response(
                workspace=workspace,
                operation=operation,
                message="workspace cancellation requested",
            )
            if committed_before_external_io:
                await self._session.commit()
        except asyncio.CancelledError as exc:
            if committed_before_external_io:
                await self._finish_precommitted_cancelled_control_operation_failed(
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    exc=exc,
                    terminal_runtime_cleanup=terminal_runtime_cleanup,
                    terminal_runtime_release_claim_owner_id=(
                        terminal_runtime_cleanup.claim_owner_id
                        if terminal_runtime_cleanup is not None
                        else None
                    ),
                )
            raise
        except VersionConflictError:
            raise
        except Exception as exc:
            if committed_before_external_io:
                await _finish_precommitted_control_operation_failed(
                    self._session,
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    exc=exc,
                    terminal_runtime_cleanup=terminal_runtime_cleanup,
                    terminal_runtime_release_claim_owner_id=(
                        terminal_runtime_cleanup.claim_owner_id
                        if terminal_runtime_cleanup is not None
                        else None
                    ),
                )
            raise
        assert response is not None
        return response

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

        operation = prepared.resume
        if operation is None:
            operation = await operations.create(
                workspace_id=workspace_id,
                operation_type=OperationType.stop,
                status=OperationStatus.running,
                payload=operation_payload,
                idempotency_key=prepared.idempotency_key,
            )
        operation_id = operation.id
        terminal_runtime_snapshot = _snapshot_terminal_runtime_workspace_for_control(workspace)
        await self._commit_before_external_runtime_io()
        terminal_runtime_cleanup: _ControlTerminalRuntimeCleanup | None = None
        terminal_runtime_stop_error: Exception | None = None
        try:
            terminal_runtime_stop = await self._run_with_teardown_operation_heartbeat(
                operation_id,
                self._stop_external_runtime_for_control(terminal_runtime_snapshot),
            )
            terminal_runtime_cleanup = terminal_runtime_stop.cleanup
            terminal_runtime_stop_error = terminal_runtime_stop.stop_error
        except WorkspaceStackStopError as exc:
            operation = await _require_control_operation(operations, operation_id)
            workspace = await self._require_workspace(repo, workspace_id)
            await _finish_stack_stop_failed_operation(
                self._session,
                operations,
                operation,
                workspace=workspace,
                exc=exc,
            )
            raise
        except asyncio.CancelledError:
            await self._preserve_precommitted_cancelled_operation(operation_id)
            raise
        except Exception as exc:
            await _finish_precommitted_control_operation_failed(
                self._session,
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=exc,
            )
            raise
        if terminal_runtime_stop_error is not None:
            await self._finish_external_runtime_stop_error_for_control(
                operations,
                repo,
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=terminal_runtime_stop_error,
                terminal_runtime_cleanup=terminal_runtime_cleanup,
            )
            raise terminal_runtime_stop_error
        response: WorkspaceControlResponse | None = None
        try:
            operation = await _require_control_operation(operations, operation_id)
            workspace = await self._require_workspace_for_update(repo, workspace_id)
            if conflict := _workspace_version_conflict(workspace, expected_version):
                if terminal_runtime_cleanup is not None:
                    await self._record_terminal_runtime_release_for_control(
                        workspace,
                        release=terminal_runtime_cleanup,
                        source="service.controls.stop",
                        require_terminal_status=False,
                    )
                    await self._release_terminal_runtime_claim_for_control(
                        workspace,
                        release=terminal_runtime_cleanup,
                    )
                await _finish_version_conflict_operation(
                    self._session,
                    operations,
                    operation,
                    workspace=workspace,
                    exc=conflict,
                )
                raise conflict
            if _is_active(
                WorkspaceStatus(workspace.status)
            ) and WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status),
                WorkspaceStatus.cancelled,
            ):
                await _transition_workspace_for_control(
                    repo,
                    workspace,
                    to=WorkspaceStatus.cancelled,
                    reason_code=_OPERATOR_STOP_REASON_CODE,
                    payload=event_payload,
                    allow_active_operation_id=operation_id,
                )
            else:
                await repo.add_event(
                    workspace,
                    event_type="workspace.stack_stopped",
                    reason_code=_OPERATOR_STOP_REASON_CODE,
                    payload=event_payload,
                )
                await _release_active_resource_reservation_for_control(
                    self._session,
                    workspace,
                )
            if terminal_runtime_cleanup is not None:
                await self._record_terminal_runtime_release_for_control(
                    workspace,
                    release=terminal_runtime_cleanup,
                    source="service.controls.stop",
                )
                await self._release_terminal_runtime_claim_for_control(
                    workspace,
                    release=terminal_runtime_cleanup,
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
            response = _control_response(
                workspace=workspace,
                operation=operation,
                message="workspace stack stopped",
            )
            await self._session.commit()
        except asyncio.CancelledError as exc:
            await self._finish_precommitted_cancelled_control_operation_failed(
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=exc,
                terminal_runtime_cleanup=terminal_runtime_cleanup,
                terminal_runtime_release_claim_owner_id=(
                    terminal_runtime_cleanup.claim_owner_id
                    if terminal_runtime_cleanup is not None
                    else None
                ),
            )
            raise
        except VersionConflictError:
            raise
        except Exception as exc:
            await _finish_precommitted_control_operation_failed(
                self._session,
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=exc,
                terminal_runtime_cleanup=terminal_runtime_cleanup,
                terminal_runtime_release_claim_owner_id=(
                    terminal_runtime_cleanup.claim_owner_id
                    if terminal_runtime_cleanup is not None
                    else None
                ),
            )
            raise
        assert response is not None
        return response

    async def _stop_external_runtime_for_control(
        self,
        workspace: _ControlTerminalRuntimeWorkspaceSnapshot,
    ) -> _ControlExternalRuntimeStop:
        stop_error: Exception | None = None
        try:
            await self._project_stopper(workspace.compose_project_name)
        except Exception as exc:
            stop_error = exc
        cleanup = await self._cleanup_terminal_runtime_for_control(workspace)
        return _ControlExternalRuntimeStop(cleanup=cleanup, stop_error=stop_error)

    async def _finish_external_runtime_stop_error_for_control(
        self,
        operations: OperationRepository,
        repo: WorkspaceRepository,
        *,
        operation_id: str,
        workspace_id: str,
        exc: Exception,
        terminal_runtime_cleanup: _ControlTerminalRuntimeCleanup | None,
    ) -> None:
        if isinstance(exc, WorkspaceStackStopError):
            operation = await _require_control_operation(operations, operation_id)
            workspace = await self._require_workspace(repo, workspace_id)
            await _finish_stack_stop_failed_operation(
                self._session,
                operations,
                operation,
                workspace=workspace,
                exc=exc,
                terminal_runtime_cleanup=terminal_runtime_cleanup,
                terminal_runtime_release_claim_owner_id=(
                    terminal_runtime_cleanup.claim_owner_id
                    if terminal_runtime_cleanup is not None
                    else None
                ),
            )
            return
        await _finish_precommitted_control_operation_failed(
            self._session,
            operation_id=operation_id,
            workspace_id=workspace_id,
            exc=exc,
            terminal_runtime_cleanup=terminal_runtime_cleanup,
            terminal_runtime_release_claim_owner_id=(
                terminal_runtime_cleanup.claim_owner_id
                if terminal_runtime_cleanup is not None
                else None
            ),
        )

    async def _run_with_teardown_operation_heartbeat(
        self,
        operation_id: str,
        work: Coroutine[Any, Any, _T],
    ) -> _T:
        if self._session_factory is None:
            work.close()
            raise RuntimeError(
                "WorkspaceControlService requires session_factory for teardown operation heartbeats"
            )

        work_task: asyncio.Task[_T] = asyncio.create_task(
            work,
            name=f"awf-teardown-operation-work-{operation_id}",
        )
        heartbeat_task: asyncio.Task[str] = asyncio.create_task(
            _renew_runtime_teardown_operation_lease_loop(
                self._session_factory,
                operation_id=operation_id,
                interval_seconds=_RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS,
            ),
            name=f"awf-teardown-operation-lease-{operation_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {work_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                if work_task in done:
                    return work_task.result()
                heartbeat_stop_reason = heartbeat_task.result()
                work_task.cancel()
                with suppress(asyncio.CancelledError):
                    await work_task
                raise RuntimeError(
                    f"{_TEARDOWN_OPERATION_HEARTBEAT_STOPPED_MESSAGE}: {heartbeat_stop_reason}"
                )
            return work_task.result()
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            if not work_task.done():
                work_task.cancel()
                with suppress(asyncio.CancelledError):
                    await work_task

    async def _cleanup_terminal_runtime_for_control(
        self,
        workspace: _ControlTerminalRuntimeWorkspaceSnapshot,
    ) -> _ControlTerminalRuntimeCleanup | None:
        (
            claim_active,
            claim_owner_id,
        ) = await self._terminal_runtime_release_claim_active_for_control(workspace.workspace_id)
        if claim_active:
            return None

        cleanup_worktree_host_path: Path | None = None
        preserved_worktree_host_path: Path | None = None
        if self._worktrees_root is not None:
            candidate_worktree_path = self._worktrees_root / workspace.workspace_id
            if await asyncio.to_thread(candidate_worktree_path.exists):
                cleanup_worktree_host_path = candidate_worktree_path
                preserved_worktree_host_path = candidate_worktree_path
        cleaner = self._cleaner_factory()
        try:
            cleanup = await self._run_with_terminal_runtime_release_claim_heartbeat(
                workspace.workspace_id,
                owner_id=claim_owner_id,
                work=cleaner.cleanup(
                    workspace_id=workspace.workspace_id,
                    repo_url=workspace.repo_url,
                    compose_project_name=workspace.compose_project_name,
                    compose_file_path=(
                        Path(workspace.compose_file_path) if workspace.compose_file_path else None
                    ),
                    worktree_host_path=cleanup_worktree_host_path,
                    remove_volumes=False,
                    remove_worktree=False,
                ),
            )
        except asyncio.CancelledError:
            if claim_owner_id is not None:
                await self._release_terminal_runtime_claim_for_control_now(
                    workspace.workspace_id,
                    owner_id=claim_owner_id,
                )
            raise
        except Exception as exc:
            cleanup_result = _terminal_runtime_release_exception_result(exc)
        else:
            cleanup_result = _normalize_cleanup_result(cleanup)
        return _ControlTerminalRuntimeCleanup(
            cleanup=cleanup_result,
            preserved_worktree_host_path=preserved_worktree_host_path,
            claim_owner_id=claim_owner_id,
        )

    async def _run_with_terminal_runtime_release_claim_heartbeat(
        self,
        workspace_id: str,
        *,
        owner_id: str | None,
        work: Coroutine[Any, Any, _T],
    ) -> _T:
        if owner_id is None:
            return await work
        if self._session_factory is None:
            work.close()
            raise RuntimeError(
                "WorkspaceControlService requires session_factory for terminal runtime "
                "release claim heartbeats"
            )

        work_task: asyncio.Task[_T] = asyncio.create_task(
            work,
            name=f"awf-control-terminal-runtime-cleanup-{workspace_id}",
        )
        heartbeat_task: asyncio.Task[_ControlTerminalRuntimeReleaseClaimFailure] = (
            asyncio.create_task(
                _refresh_terminal_runtime_release_claim_loop(
                    self._session_factory,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                ),
                name=f"awf-control-terminal-runtime-claim-{workspace_id}",
            )
        )
        try:
            done, _pending = await asyncio.wait(
                {work_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                if work_task in done:
                    return work_task.result()
                claim_failure = heartbeat_task.result()
                work_task.cancel()
                with suppress(asyncio.CancelledError):
                    await work_task
                raise RuntimeError(
                    "terminal runtime release claim heartbeat stopped before cleanup completed: "
                    f"{claim_failure.reason_code}"
                )
            return work_task.result()
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            if not work_task.done():
                work_task.cancel()
                with suppress(asyncio.CancelledError):
                    await work_task

    async def _terminal_runtime_release_claim_active_for_control(
        self,
        workspace_id: str,
    ) -> tuple[bool, str | None]:
        repo = WorkspaceRepository(self._session)
        locked_workspace = await repo.get_for_update(workspace_id)
        if locked_workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        now = datetime.now(UTC)
        claim_owner_id: str | None = None
        claim_required = _control_terminal_runtime_release_claim_required(locked_workspace)
        active = terminal_runtime_release_claim_active(locked_workspace, now=now) or (
            claim_required
            and _stale_active_execution_cleanup_claim_active_for_control(
                locked_workspace,
                now=now,
            )
        )
        if not claim_required or active:
            await self._session.commit()
            return active, claim_owner_id

        claim_owner_id = f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}control:{uuid4().hex}"
        if _execution_claim_active_for_control(locked_workspace, now=now):
            locked_workspace.execution_claimed_by = claim_owner_id
            locked_workspace.execution_claim_expires_at = (
                _terminal_runtime_release_claim_expires_at()
            )
            await self._session.commit()
            return False, claim_owner_id

        claimed = await repo.claim_execution_if_available(
            locked_workspace.id,
            owner_id=claim_owner_id,
            lease_expires_at=_terminal_runtime_release_claim_expires_at(),
            statuses=(locked_workspace.status,),
            claim_cutoff=now,
        )
        if claimed is None:
            active = True
            claim_owner_id = None
        else:
            active = False
        await self._session.commit()
        return active, claim_owner_id

    async def _release_terminal_runtime_claim_for_control_now(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> None:
        try:
            await WorkspaceRepository(self._session).release_execution_claim(
                workspace_id,
                owner_id=owner_id,
            )
            await self._session.commit()
        except Exception as exc:
            with suppress(Exception):
                await self._session.rollback()
            _log.warning(
                "controls.terminal_runtime_release_claim_clear_failed",
                workspace_id=workspace_id,
                owner_id=owner_id,
                error=redact_audit_text(repr(exc), limit=400),
            )

    async def _release_terminal_runtime_claim_for_control(
        self,
        workspace: Workspace,
        *,
        release: _ControlTerminalRuntimeCleanup,
    ) -> None:
        if release.claim_owner_id is None:
            return
        released = await WorkspaceRepository(self._session).release_execution_claim(
            workspace.id,
            owner_id=release.claim_owner_id,
        )
        if released:
            workspace.execution_claimed_by = None
            workspace.execution_claim_expires_at = None

    async def _record_terminal_runtime_release_for_control(
        self,
        workspace: Workspace,
        *,
        release: _ControlTerminalRuntimeCleanup,
        source: str,
        require_terminal_status: bool = True,
    ) -> None:
        if require_terminal_status and workspace.status not in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.failed.value,
            WorkspaceStatus.cancelled.value,
            WorkspaceStatus.destroyed.value,
        }:
            return
        pending_control_state = _session_has_pending_state(self._session)
        try:
            await self._session.flush()
        except Exception as exc:
            if pending_control_state:
                # This flush covers the control transition and related state that must
                # commit before the optional release audit event can be isolated.
                raise
            _log.warning(
                "controls.terminal_runtime_release_event_record_failed",
                workspace_id=workspace.id,
                source=source,
                error=redact_audit_text(repr(exc), limit=400),
            )
            return
        try:
            async with self._session.begin_nested():
                await record_terminal_runtime_release_event(
                    self._session,
                    workspace_id=workspace.id,
                    cleanup=release.cleanup,
                    source=source,
                    worktree_host_path=release.preserved_worktree_host_path,
                    allow_non_terminal=not require_terminal_status,
                )
        except Exception as exc:
            _log.warning(
                "controls.terminal_runtime_release_event_record_failed",
                workspace_id=workspace.id,
                source=source,
                error=redact_audit_text(repr(exc), limit=400),
            )

    async def _commit_before_external_runtime_io(self) -> None:
        await self._session.flush()
        await self._session.commit()

    async def _preserve_precommitted_cancelled_operation(self, operation_id: str) -> None:
        preserve = self._preserve_precommitted_cancelled_operation_unshielded
        if self._session_factory is None:
            await preserve(operation_id, session=self._session)
            return
        preserve_task = asyncio.create_task(preserve(operation_id))
        while not preserve_task.done():
            try:
                await asyncio.shield(preserve_task)
            except asyncio.CancelledError:
                # The caller is already in a cancellation handler and will re-raise.
                continue
        with suppress(Exception, asyncio.CancelledError):
            preserve_task.result()

    async def _finish_precommitted_cancelled_control_operation_failed(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        exc: BaseException,
        terminal_runtime_cleanup: _ControlTerminalRuntimeCleanup | None = None,
        terminal_runtime_release_claim_owner_id: str | None = None,
    ) -> None:
        if self._session_factory is None:
            await _finish_precommitted_control_operation_failed(
                self._session,
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=exc,
                terminal_runtime_cleanup=terminal_runtime_cleanup,
                terminal_runtime_release_claim_owner_id=terminal_runtime_release_claim_owner_id,
            )
            return

        rollback_task = asyncio.create_task(self._session.rollback())
        while not rollback_task.done():
            try:
                await asyncio.shield(rollback_task)
            except asyncio.CancelledError:
                continue
        with suppress(Exception, asyncio.CancelledError):
            rollback_task.result()

        async def _finish_in_recovery_session() -> None:
            assert self._session_factory is not None
            async with self._session_factory() as recovery_session:
                await _finish_precommitted_control_operation_failed(
                    recovery_session,
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    exc=exc,
                    terminal_runtime_cleanup=terminal_runtime_cleanup,
                    terminal_runtime_release_claim_owner_id=(
                        terminal_runtime_release_claim_owner_id
                    ),
                )

        finish_task = asyncio.create_task(_finish_in_recovery_session())
        while not finish_task.done():
            try:
                await asyncio.shield(finish_task)
            except asyncio.CancelledError:
                # Preserve the failure record even if the caller is cancelled again.
                continue
        with suppress(Exception, asyncio.CancelledError):
            finish_task.result()

    async def _preserve_precommitted_cancelled_operation_unshielded(
        self,
        operation_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> None:
        with suppress(Exception):
            if session is not None:
                await _preserve_precommitted_running_operation(session, operation_id)
                return
            if self._session_factory is None:
                return
            async with self._session_factory() as preserve_session:
                await _preserve_precommitted_running_operation(
                    preserve_session,
                    operation_id,
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

        if terminal_runtime_release_claim_active(workspace):
            raise WorkspaceRemonitorTerminalRuntimeReleaseInProgressError(workspace)

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
        await _transition_workspace_for_control(
            repo,
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
        await _transition_workspace_for_control(
            repo,
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

        operation = prepared.resume
        if operation is None:
            operation = await operations.create(
                workspace_id=workspace_id,
                operation_type=OperationType.destroy,
                status=OperationStatus.running,
                payload=operation_payload,
                idempotency_key=prepared.idempotency_key,
            )
        operation_id = operation.id
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
            await _transition_workspace_for_control(
                repo,
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                payload=event_payload,
                allow_active_operation_id=operation_id,
            )
            current = WorkspaceStatus.cancelled
        if WorkspaceStateMachine.can_transition(current, WorkspaceStatus.destroying):
            await _transition_workspace_for_control(
                repo,
                workspace,
                to=WorkspaceStatus.destroying,
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                payload=event_payload,
                allow_active_operation_id=operation_id,
            )

        await self._commit_before_external_runtime_io()
        response: WorkspaceControlResponse | None = None
        try:
            cleaner = self._cleaner_factory()
            cleanup_result = _normalize_cleanup_result(
                await self._run_with_teardown_operation_heartbeat(
                    operation_id,
                    cleaner.cleanup(
                        workspace_id=workspace_id,
                        repo_url=workspace.repo_url,
                        compose_project_name=workspace.compose_project_name,
                        compose_file_path=(
                            Path(workspace.compose_file_path)
                            if workspace.compose_file_path
                            else None
                        ),
                        worktree_host_path=None,
                        remove_volumes=remove_volumes,
                        remove_worktree=remove_worktree,
                    ),
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
                response = _control_response(
                    workspace=workspace,
                    operation=operation,
                    message="workspace destroy callback ignored",
                )
                await self._session.commit()
                return response
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
                workspace.failure_reason = "cleanup_failure"
                workspace.failure_message = bounded_cleanup_message
                if WorkspaceStateMachine.can_transition(
                    WorkspaceStatus(workspace.status), WorkspaceStatus.failed
                ):
                    await _transition_workspace_for_control(
                        repo,
                        workspace,
                        to=WorkspaceStatus.failed,
                        reason_code="CLEANUP_FAILED",
                        payload=cleanup_event_payload,
                        allow_active_operation_id=operation_id,
                    )
                operation_result = _with_secret_lease_result(
                    {"status": workspace.status, "cleanup": cleanup_payload},
                    secret_lease_summary,
                )
                await operations.finish(
                    operation,
                    status=OperationStatus.failed,
                    error_code="CLEANUP_FAILED",
                    error_message=bounded_cleanup_message,
                    result=operation_result,
                )
                audit_evidence = _with_secret_lease_evidence(
                    {
                        "cleanup": cleanup_payload,
                        "error_message": bounded_cleanup_message,
                    },
                    secret_lease_summary,
                )
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
                    await _transition_workspace_for_control(
                        repo,
                        workspace,
                        to=WorkspaceStatus.destroyed,
                        reason_code="DESTROYED",
                        payload=cleanup_event_payload,
                        allow_active_operation_id=operation_id,
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

            response = _control_response(
                workspace=workspace,
                operation=operation,
                message=message,
            )
            await self._session.commit()
        except asyncio.CancelledError as exc:
            await self._finish_precommitted_cancelled_control_operation_failed(
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=exc,
            )
            raise
        except Exception as exc:
            await _finish_precommitted_control_operation_failed(
                self._session,
                operation_id=operation_id,
                workspace_id=workspace_id,
                exc=exc,
            )
            raise
        assert response is not None
        return response

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
                if _can_resume_expired_runtime_teardown_operation(existing):
                    workspace = await self._require_workspace_for_update(repo, workspace_id)
                    if conflict := _workspace_version_conflict(workspace, expected_version):
                        raise conflict
                    active_teardown = await _find_active_operation(
                        operations,
                        workspace_id=workspace_id,
                        operation_types=_RUNTIME_TEARDOWN_OPERATION_TYPES,
                    )
                    if active_teardown is not None:
                        raise WorkspaceActiveOperationConflictError(active_teardown)
                    _renew_runtime_teardown_operation(existing)
                    return _PreparedOperation(
                        workspace=workspace,
                        resume=existing,
                        idempotency_key=idempotency_key,
                    )
                workspace = await self._require_workspace(repo, workspace_id)
                return _PreparedOperation(
                    workspace=workspace,
                    replay=existing,
                    kind=_PreparedOperationKind.exact_replay,
                    idempotency_key=idempotency_key,
                )

        workspace = await self._require_workspace_for_update(repo, workspace_id)
        if conflict := _workspace_version_conflict(workspace, expected_version):
            raise conflict
        active_teardown = await _find_active_operation(
            operations,
            workspace_id=workspace_id,
            operation_types=_RUNTIME_TEARDOWN_OPERATION_TYPES,
        )
        if active_teardown is not None:
            raise WorkspaceActiveOperationConflictError(active_teardown)
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


async def _transition_workspace_for_control(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    to: WorkspaceStatus,
    reason_code: str,
    payload: dict[str, Any] | None = None,
    allow_active_operation_id: str | None = None,
) -> Workspace:
    try:
        return await repo.transition(
            workspace,
            to=to,
            reason_code=reason_code,
            payload=payload,
            allow_active_operation_id=allow_active_operation_id,
        )
    except WorkspaceTransitionBlockedByActiveOperationError as exc:
        raise WorkspaceActiveOperationConflictError(exc.operation) from exc
    except WorkspaceTransitionStaleError as exc:
        if exc.actual_version is None:
            raise WorkspaceNotFoundError(exc.workspace_id) from exc
        raise VersionConflictError(
            expected_version=exc.expected_version,
            actual_version=exc.actual_version,
        ) from exc


async def stop_project_containers(compose_project_name: str | None) -> None:
    """Stop running compose containers without removing salvage evidence."""
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
    try:
        await _communicate(stop, operation="stop")
    except WorkspaceStackStopError as exc:
        if _docker_stop_failed_only_for_missing_containers(exc.stderr):
            return
        raise


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


def _docker_stop_failed_only_for_missing_containers(stderr: str) -> bool:
    lines = [line.strip().lower() for line in stderr.splitlines() if line.strip()]
    return bool(lines) and all("no such container" in line for line in lines)


def default_cleaner() -> WorkspaceCleaner:
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    template = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
    return WorkspaceCleaner(
        git=GitManager(work_dir / "git"),
        compose=ComposeManager(work_dir=work_dir, template_path=template),
    )


def default_worktrees_root(settings: Settings | None = None) -> Path:
    resolved = settings or get_settings()
    return Path(resolved.work_dir) / "git" / "worktrees"


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


async def _require_control_operation(
    operations: OperationRepository,
    operation_id: str,
) -> Operation:
    operation = await operations.get(operation_id)
    if operation is None:
        raise RuntimeError(f"Control operation {operation_id} disappeared")
    return operation


def _snapshot_terminal_runtime_workspace_for_control(
    workspace: Workspace,
) -> _ControlTerminalRuntimeWorkspaceSnapshot:
    return _ControlTerminalRuntimeWorkspaceSnapshot(
        workspace_id=workspace.id,
        repo_url=workspace.repo_url,
        compose_project_name=workspace.compose_project_name,
        compose_file_path=workspace.compose_file_path,
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


def _terminal_runtime_release_exception_result(exc: BaseException) -> WorkspaceCleanupResult:
    return WorkspaceCleanupResult(
        status="partial",
        reason_code=TERMINAL_RUNTIME_RELEASE_EXCEPTION_REASON_CODE,
        steps=(
            WorkspaceCleanupStepResult(
                name="terminal_runtime_release",
                status="failed",
                reason_code=TERMINAL_RUNTIME_RELEASE_EXCEPTION_REASON_CODE,
                error=redact_audit_text(
                    f"{type(exc).__name__}: {exc}",
                    limit=1000,
                ),
            ),
        ),
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


async def _renew_runtime_teardown_operation_lease_loop(
    session_factory: ControlSessionFactory,
    *,
    operation_id: str,
    interval_seconds: float = _RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS,
) -> str:
    interval = max(float(interval_seconds), 0.001)
    lease_timeout_seconds = max(float(EXTERNAL_RUNTIME_TEARDOWN_OPERATION_TIMEOUT_SECONDS), 0.001)
    loop = asyncio.get_running_loop()
    last_lease_renewed_at = loop.time()
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                renewed = await OperationRepository(session).renew_teardown_lease(operation_id)
                await session.commit()
        except Exception as exc:
            elapsed_since_lease_renewal = loop.time() - last_lease_renewed_at
            _log.warning(
                "controls.teardown_operation_lease_renew_failed",
                operation_id=operation_id,
                error=redact_audit_text(repr(exc), limit=400),
                elapsed_since_lease_renewal_seconds=round(elapsed_since_lease_renewal, 3),
            )
            if elapsed_since_lease_renewal >= lease_timeout_seconds:
                _log.warning(
                    "controls.teardown_operation_lease_renew_abandoned",
                    operation_id=operation_id,
                    elapsed_since_lease_renewal_seconds=round(elapsed_since_lease_renewal, 3),
                    lease_timeout_seconds=round(lease_timeout_seconds, 3),
                )
                return "operation lease renewal failed for longer than teardown timeout"
            continue
        if renewed is None:
            return "operation lease is no longer active"
        last_lease_renewed_at = loop.time()


async def _refresh_terminal_runtime_release_claim_loop(
    session_factory: ControlSessionFactory,
    *,
    workspace_id: str,
    owner_id: str,
    interval_seconds: float | None = None,
) -> _ControlTerminalRuntimeReleaseClaimFailure:
    interval = (
        _terminal_runtime_release_claim_heartbeat_interval_seconds()
        if interval_seconds is None
        else interval_seconds
    )
    interval = max(float(interval), 0.001)
    claim_timeout_seconds = max(float(TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS), 0.001)
    loop = asyncio.get_running_loop()
    last_claim_renewed_at = loop.time()
    last_safe_exception: str | None = None
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                refreshed = await WorkspaceRepository(session).refresh_execution_claim(
                    workspace_id,
                    owner_id=owner_id,
                    lease_expires_at=_terminal_runtime_release_claim_expires_at(),
                )
                await session.commit()
        except Exception as exc:
            last_safe_exception = redact_audit_text(
                f"{type(exc).__name__}: {exc}",
                limit=1000,
            )
            elapsed_since_claim_renewal = loop.time() - last_claim_renewed_at
            _log.warning(
                "controls.terminal_runtime_release_claim_refresh_failed",
                workspace_id=workspace_id,
                error=redact_audit_text(repr(exc), limit=400),
                elapsed_since_claim_renewal_seconds=round(elapsed_since_claim_renewal, 3),
            )
            if elapsed_since_claim_renewal >= claim_timeout_seconds:
                _log.warning(
                    "controls.terminal_runtime_release_claim_refresh_abandoned",
                    workspace_id=workspace_id,
                    elapsed_since_claim_renewal_seconds=round(
                        elapsed_since_claim_renewal,
                        3,
                    ),
                    claim_timeout_seconds=round(claim_timeout_seconds, 3),
                )
                return _ControlTerminalRuntimeReleaseClaimFailure(
                    reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
                    error=last_safe_exception,
                )
            continue
        if not refreshed:
            _log.warning(
                "controls.terminal_runtime_release_claim_lost",
                workspace_id=workspace_id,
            )
            return _ControlTerminalRuntimeReleaseClaimFailure(
                reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
            )
        last_claim_renewed_at = loop.time()


def _terminal_runtime_release_claim_heartbeat_interval_seconds() -> float:
    return max(0.001, min(60.0, TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS / 3))


def _terminal_runtime_release_claim_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS)


async def _preserve_precommitted_running_operation(
    session: AsyncSession,
    operation_id: str,
) -> None:
    await session.rollback()
    operation = await OperationRepository(session).renew_teardown_lease(operation_id)
    if operation is None:
        return
    await session.commit()


async def _finish_stack_stop_failed_operation(
    session: AsyncSession,
    operations: OperationRepository,
    operation: Operation,
    *,
    workspace: Workspace,
    exc: WorkspaceStackStopError,
    terminal_runtime_cleanup: _ControlTerminalRuntimeCleanup | None = None,
    terminal_runtime_release_claim_owner_id: str | None = None,
) -> None:
    repo = WorkspaceRepository(session)
    operation_payload = operation.payload if isinstance(operation.payload, dict) else {}
    if (
        terminal_runtime_release_claim_owner_id is not None
        and workspace.execution_claimed_by == terminal_runtime_release_claim_owner_id
    ):
        workspace.execution_claimed_by = None
        workspace.execution_claim_expires_at = None
    terminal_runtime_release = _terminal_runtime_release_evidence(terminal_runtime_cleanup)
    operation_result: dict[str, object] = {"status": workspace.status}
    if terminal_runtime_release is not None:
        operation_result["terminal_runtime_release"] = terminal_runtime_release
    await operations.finish(
        operation,
        status=OperationStatus.failed,
        result=operation_result,
        error_code=exc.error_code,
        error_message=_bounded_operation_error_message(exc.message),
    )
    audit_extra: dict[str, object | None] = {
        "stop_stack": operation_payload.get("stop_stack"),
        "expected_version": operation_payload.get("expected_version"),
    }
    terminal_runtime_release_summary = _terminal_runtime_release_audit_summary(
        terminal_runtime_cleanup
    )
    if terminal_runtime_release_summary is not None:
        audit_extra["terminal_runtime_release"] = terminal_runtime_release_summary
    audit_evidence: dict[str, object] = {
        "operation": f"docker {exc.operation}",
        "returncode": exc.returncode,
        "error_message": _bounded_operation_error_message(exc.message),
    }
    if terminal_runtime_release is not None:
        audit_evidence["terminal_runtime_release"] = terminal_runtime_release
    await _add_control_audit_event(
        repo,
        workspace,
        operation=operation,
        action=operation.type,
        outcome="failed",
        reason_code=exc.error_code,
        extra=audit_extra,
        evidence=audit_evidence,
    )
    await session.commit()


async def _finish_version_conflict_operation(
    session: AsyncSession,
    operations: OperationRepository,
    operation: Operation,
    *,
    workspace: Workspace,
    exc: VersionConflictError,
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
        evidence=exc.detail,
    )
    await session.commit()


async def _finish_precommitted_control_operation_failed(
    session: AsyncSession,
    *,
    operation_id: str,
    workspace_id: str,
    exc: BaseException,
    terminal_runtime_cleanup: _ControlTerminalRuntimeCleanup | None = None,
    terminal_runtime_release_claim_owner_id: str | None = None,
) -> None:
    try:
        await session.rollback()
        operations = OperationRepository(session)
        repo = WorkspaceRepository(session)
        operation = await operations.get(operation_id)
        if operation is None or operation.status != OperationStatus.running.value:
            return
        workspace = await repo.get_for_update(workspace_id)
        if (
            workspace is not None
            and terminal_runtime_release_claim_owner_id is not None
            and workspace.execution_claimed_by == terminal_runtime_release_claim_owner_id
        ):
            workspace.execution_claimed_by = None
            workspace.execution_claim_expires_at = None
        operation_payload = operation.payload if isinstance(operation.payload, dict) else {}
        error_message = _control_operation_exception_message(exc)
        terminal_runtime_release = _terminal_runtime_release_evidence(terminal_runtime_cleanup)
        operation_result: dict[str, object] = {}
        if workspace is not None:
            operation_result["status"] = workspace.status
        if terminal_runtime_release is not None:
            operation_result["terminal_runtime_release"] = terminal_runtime_release
        await operations.finish(
            operation,
            status=OperationStatus.failed,
            result=operation_result or None,
            error_code=_CONTROL_OPERATION_FAILED_REASON_CODE,
            error_message=error_message,
        )
        if workspace is not None:
            try:
                audit_extra: dict[str, object | None] = {
                    "stop_stack": operation_payload.get("stop_stack"),
                    "expected_version": operation_payload.get("expected_version"),
                }
                terminal_runtime_release_summary = _terminal_runtime_release_audit_summary(
                    terminal_runtime_cleanup
                )
                if terminal_runtime_release_summary is not None:
                    audit_extra["terminal_runtime_release"] = terminal_runtime_release_summary
                audit_evidence: dict[str, object | None] = {
                    "error_type": type(exc).__name__,
                    "error_message": error_message,
                }
                if terminal_runtime_release is not None:
                    audit_evidence["terminal_runtime_release"] = terminal_runtime_release
                async with session.begin_nested():
                    await _add_control_audit_event(
                        repo,
                        workspace,
                        operation=operation,
                        action=operation.type,
                        outcome="failed",
                        reason_code=_CONTROL_OPERATION_FAILED_REASON_CODE,
                        extra=audit_extra,
                        evidence=audit_evidence,
                    )
            except Exception as audit_exc:
                _log.warning(
                    "controls.precommitted_control_operation_audit_record_failed",
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    error=redact_audit_text(repr(audit_exc), limit=400),
                )
        await session.commit()
    except Exception as recovery_exc:
        with suppress(Exception):
            await session.rollback()
        _log.warning(
            "controls.precommitted_control_operation_failed_record_failed",
            operation_id=operation_id,
            workspace_id=workspace_id,
            error=redact_audit_text(repr(recovery_exc), limit=400),
        )


def _terminal_runtime_release_evidence(
    release: _ControlTerminalRuntimeCleanup | None,
) -> dict[str, object] | None:
    if release is None:
        return None
    evidence: dict[str, object] = {
        "cleanup": _redacted_terminal_runtime_release_cleanup(release.cleanup),
        "preserved": _terminal_runtime_release_preserved_evidence(release),
    }
    if release.claim_owner_id is not None:
        evidence["claim_owner_id"] = release.claim_owner_id
    return evidence


def _redacted_terminal_runtime_release_cleanup(
    cleanup: WorkspaceCleanupResult,
) -> dict[str, object]:
    return cast(dict[str, object], redact_audit_value(cleanup.to_dict()))


def _terminal_runtime_release_audit_summary(
    release: _ControlTerminalRuntimeCleanup | None,
) -> dict[str, object] | None:
    if release is None:
        return None
    summary: dict[str, object] = {
        "cleanup_status": release.cleanup.status,
        "cleanup_reason_code": release.cleanup.reason_code,
    }
    preserved = _terminal_runtime_release_preserved_evidence(release)
    if preserved:
        summary["preserved"] = preserved
    if release.claim_owner_id is not None:
        summary["claim_owner_id"] = release.claim_owner_id
    return summary


def _terminal_runtime_release_preserved_evidence(
    release: _ControlTerminalRuntimeCleanup,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "worktree_path": (
                str(release.preserved_worktree_host_path)
                if release.preserved_worktree_host_path is not None
                else None
            ),
        }.items()
        if value is not None
    }


def _workspace_version_conflict(
    workspace: Workspace,
    expected_version: int | None,
) -> VersionConflictError | None:
    if expected_version is None or workspace.version == expected_version:
        return None
    return VersionConflictError(
        expected_version=expected_version,
        actual_version=workspace.version,
    )


def _bounded_operation_error_message(message: str) -> str:
    return message[:_OPERATION_ERROR_MESSAGE_MAX_LENGTH]


def _control_operation_exception_message(exc: BaseException) -> str:
    detail = str(exc).strip()
    if not detail:
        detail = (
            "operation was cancelled"
            if isinstance(exc, asyncio.CancelledError)
            else "operation failed without details"
        )
    return _bounded_operation_error_message(
        redact_audit_text(
            f"{type(exc).__name__}: {detail}",
            limit=_OPERATION_ERROR_MESSAGE_MAX_LENGTH,
        )
    )


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


def _control_terminal_runtime_release_claim_required(workspace: Workspace) -> bool:
    return workspace.status in {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }


def _execution_claim_active_for_control(
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> bool:
    if workspace.execution_claimed_by is None or workspace.execution_claim_expires_at is None:
        return False
    expires_at = workspace.execution_claim_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > (now or datetime.now(UTC))


def _stale_active_execution_cleanup_claim_active_for_control(
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> bool:
    owner_id = workspace.execution_claimed_by
    if owner_id is None or not owner_id.startswith(
        _STALE_ACTIVE_EXECUTION_CLEANUP_CLAIM_OWNER_PREFIX
    ):
        return False
    return _execution_claim_active_for_control(workspace, now=now)


def _control_terminal_runtime_release_claim_owner(workspace: Workspace) -> str | None:
    if not _control_terminal_runtime_release_claim_required(workspace):
        return None
    owner_id = workspace.execution_claimed_by
    if owner_id is None or not owner_id.startswith(
        f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}control:"
    ):
        return None
    if not terminal_runtime_release_claim_active(workspace):
        return None
    return owner_id


def _session_has_pending_state(session: AsyncSession) -> bool:
    return bool(session.new or session.dirty or session.deleted)


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _can_resume_expired_runtime_teardown_operation(operation: Operation) -> bool:
    return (
        operation.status == OperationStatus.running.value
        and operation.type
        in {OperationType.cancel.value, OperationType.stop.value, OperationType.destroy.value}
        and not external_runtime_teardown_operation_blocks_controls(operation)
    )


def _renew_runtime_teardown_operation(operation: Operation) -> None:
    now = datetime.now(UTC)
    operation.status = OperationStatus.running.value
    if operation.started_at is None:
        operation.started_at = now
    operation.lease_renewed_at = now
    operation.finished_at = None
    operation.error_code = None
    operation.error_message = None
    operation.result = None


async def _find_active_operation(
    operations: OperationRepository,
    *,
    workspace_id: str,
    operation_types: frozenset[str] | set[str],
) -> Operation | None:
    now = datetime.now(UTC)
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
        if operation.type not in operation_types:
            continue
        if (
            operation.type in _RUNTIME_TEARDOWN_OPERATION_TYPES
            and not external_runtime_teardown_operation_blocks_controls(operation, now=now)
        ):
            continue
        return operation
    return None


def _operation_conflict_detail(operation: Operation | None) -> dict[str, object]:
    if operation is None:
        return {
            "operation_id": None,
            "operation_type": None,
            "operation_status": None,
        }
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


async def _release_active_resource_reservation_for_control(
    session: AsyncSession,
    workspace: Workspace,
) -> None:
    if WorkspaceStatus(workspace.status) not in _RESOURCE_RESERVATION_RELEASE_STATUSES:
        return
    await ResourceReservationRepository(session).release_active_for_workspace(workspace.id)
