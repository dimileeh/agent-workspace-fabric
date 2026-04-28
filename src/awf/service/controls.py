"""Shared workspace control operations for REST and MCP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

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
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.node.cleanup import WorkspaceCleaner
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager

ProjectStopper = Callable[[str | None], Awaitable[None]]
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
_DESTROYING_OR_DESTROYED_STATUSES = frozenset(
    {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}
)
_OPERATOR_API_SOURCE = "operator_api"
_OPERATOR_CANCEL_REASON_CODE = "OPERATOR_CANCEL"
_OPERATOR_STOP_REASON_CODE = "OPERATOR_STOP"
_OPERATOR_REMONITOR_REASON_CODE = "OPERATOR_REMONITOR"
_OPERATOR_REFRESH_REASON_CODE = "OPERATOR_REFRESH"
_OPERATOR_VALIDATE_REASON_CODE = "OPERATOR_VALIDATE"
_OPERATOR_DESTROY_REASON_CODE = "OPERATOR_DESTROY"
_OPERATION_ERROR_MESSAGE_MAX_LENGTH = 2048


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
    ) -> list[str]: ...


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
                "eligible_statuses": [
                    status.value for status in _REMONITOR_ELIGIBLE_STATUSES
                ],
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
                "eligible_statuses": [
                    status.value for status in _VALIDATE_ELIGIBLE_STATUSES
                ],
            },
        )


class WorkspaceValidateMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace validate requires an existing PR URL.",
            detail={"status": workspace.status},
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
        workspace, replay = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.cancel,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
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
            idempotency_key=idempotency_key,
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
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id=operation.id,
            status=WorkspaceStatus(workspace.status),
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
        workspace, replay = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.stop,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
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
            idempotency_key=idempotency_key,
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
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id=operation.id,
            status=WorkspaceStatus(workspace.status),
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
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_REMONITOR_REASON_CODE,
            requested_action=OperationType.remonitor.value,
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        workspace, replay = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
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
            idempotency_key=idempotency_key,
        )
        claims_reset = _claim_reset_snapshot(workspace)
        state_reset = await _reset_failed_workspace_for_remonitor(
            self._session,
            workspace,
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
        }
        if state_reset is not None:
            result["state_reset"] = state_reset
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result=result,
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id=operation.id,
            status=WorkspaceStatus(workspace.status),
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
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_REFRESH_REASON_CODE,
            requested_action=OperationType.refresh.value,
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        workspace, replay = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=payload,
        )
        if replay is not None:
            return replay
        if WorkspaceStatus(workspace.status) in _DESTROYING_OR_DESTROYED_STATUSES:
            raise WorkspaceRefreshStateError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.pending,
            payload=operation_payload,
            idempotency_key=idempotency_key,
        )
        await repo.add_event(
            workspace,
            event_type="workspace.refresh_requested",
            reason_code=_OPERATOR_REFRESH_REASON_CODE,
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
        workspace, replay = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=payload,
        )
        current = WorkspaceStatus(workspace.status)
        if replay is not None:
            if current not in _VALIDATE_REPLAY_STATUSES:
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
            idempotency_key=idempotency_key,
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
        workspace, replay = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.destroy,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
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
            idempotency_key=idempotency_key,
        )
        if current == WorkspaceStatus.destroyed:
            await operations.finish(
                operation,
                status=OperationStatus.succeeded,
                result={"status": WorkspaceStatus.destroyed.value},
            )
            return WorkspaceControlResponse(
                workspace_id=workspace_id,
                operation_id=operation.id,
                status=WorkspaceStatus.destroyed,
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
        failures = await cleaner.cleanup(
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
        if failures:
            workspace.failure_reason = "cleanup_failure"
            workspace.failure_message = ", ".join(failures)
            if WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status), WorkspaceStatus.failed
            ):
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.failed,
                    reason_code="CLEANUP_FAILED",
                    payload=event_payload,
                )
            await operations.finish(
                operation,
                status=OperationStatus.failed,
                error_code="CLEANUP_FAILED",
                error_message=", ".join(failures),
                result={"status": workspace.status},
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
                    payload=event_payload,
                )
            await operations.finish(
                operation,
                status=OperationStatus.succeeded,
                result={"status": workspace.status},
            )
            message = "workspace destroyed"

        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id=operation.id,
            status=WorkspaceStatus(workspace.status),
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
    ) -> tuple[Workspace, Operation | None]:
        if idempotency_key is not None:
            await operations.acquire_idempotency_key_lock(idempotency_key)
            existing = await operations.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.workspace_id != workspace_id
                    or existing.type != operation_type.value
                    or existing.payload != payload
                ):
                    raise IdempotencyConflictError()
                workspace = await self._require_workspace(repo, workspace_id)
                return workspace, existing

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
                return workspace, active
        return workspace, None


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


def _control_response(
    *,
    workspace: Workspace,
    operation: Operation,
    message: str,
) -> WorkspaceControlResponse:
    return WorkspaceControlResponse(
        workspace_id=workspace.id,
        operation_id=operation.id,
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


def _event_payload(
    payload: dict[str, object | None],
    *,
    expected_version: int | None,
) -> dict[str, object | None]:
    event_payload = dict(payload)
    if expected_version is not None:
        event_payload["expected_version"] = expected_version
    return event_payload


async def _finish_stack_stop_failed_operation(
    session: AsyncSession,
    operations: OperationRepository,
    operation: Operation,
    *,
    workspace: Workspace,
    exc: WorkspaceStackStopError,
) -> None:
    await operations.finish(
        operation,
        status=OperationStatus.failed,
        result={"status": workspace.status},
        error_code=exc.error_code,
        error_message=_bounded_operation_error_message(exc.message),
    )
    await session.commit()


def _bounded_operation_error_message(message: str) -> str:
    return message[:_OPERATION_ERROR_MESSAGE_MAX_LENGTH]


def _claim_reset_snapshot(workspace: Workspace) -> dict[str, str | None]:
    return {
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": _json_datetime(workspace.monitor_claim_expires_at),
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": _json_datetime(
            workspace.execution_claim_expires_at
        ),
    }


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


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
