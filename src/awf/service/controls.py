"""Shared workspace control operations for REST and MCP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceControlResponse
from awf.common.config import get_settings
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.node.cleanup import WorkspaceCleaner
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager

ProjectStopper = Callable[[str | None], Awaitable[None]]
CleanerFactory = Callable[[], "WorkspaceCleanerProtocol"]


class WorkspaceCleanerProtocol(Protocol):
    async def cleanup(
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

    def __init__(self, *, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
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
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        workspace = await self._require_workspace(repo, workspace_id)
        payload = {"reason": reason, "stop_stack": stop_stack}
        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.cancel,
            status=OperationStatus.running,
            payload=payload,
        )
        if stop_stack:
            await self._project_stopper(workspace.compose_project_name)
        if (
            workspace.status != WorkspaceStatus.cancelled.value
            and WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status),
                WorkspaceStatus.cancelled,
            )
        ):
            await repo.transition(
                workspace, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_CANCEL"
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.cancel_requested",
                reason_code="OPERATOR_CANCEL",
                payload=payload,
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
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        workspace = await self._require_workspace(repo, workspace_id)
        payload = {"reason": reason}
        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.stop,
            status=OperationStatus.running,
            payload=payload,
        )
        await self._project_stopper(workspace.compose_project_name)
        if _is_active(WorkspaceStatus(workspace.status)) and WorkspaceStateMachine.can_transition(
            WorkspaceStatus(workspace.status),
            WorkspaceStatus.cancelled,
        ):
            await repo.transition(
                workspace, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_STOP"
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.stack_stopped",
                reason_code="OPERATOR_STOP",
                payload=payload,
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

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
    ) -> WorkspaceControlResponse:
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        workspace = await self._require_workspace(repo, workspace_id)
        current = WorkspaceStatus(workspace.status)
        if _is_active(current) and not force:
            raise ActiveWorkspaceDestroyError()

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.destroy,
            status=OperationStatus.running,
            payload={
                "force": force,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            },
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
                workspace, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_DESTROY"
            )
            current = WorkspaceStatus.cancelled
        if WorkspaceStateMachine.can_transition(current, WorkspaceStatus.destroying):
            await repo.transition(
                workspace, to=WorkspaceStatus.destroying, reason_code="OPERATOR_DESTROY"
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
                    workspace, to=WorkspaceStatus.failed, reason_code="CLEANUP_FAILED"
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
                    workspace, to=WorkspaceStatus.destroyed, reason_code="DESTROYED"
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
        compose=ComposeManager(work_dir=work_dir / "compose", template_path=template),
    )


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
