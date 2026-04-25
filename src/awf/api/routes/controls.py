"""Sensitive workspace control endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.schemas import WorkspaceControlRequest, WorkspaceControlResponse
from awf.common.config import get_settings
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.node.cleanup import WorkspaceCleaner
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}",
    tags=["workspace-controls"],
    dependencies=[Depends(require_api_token)],
)


@router.post("/cancel", response_model=WorkspaceControlResponse)
async def cancel_workspace(
    workspace_id: str,
    payload: WorkspaceControlRequest,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    repo = WorkspaceRepository(session)
    operations = OperationRepository(session)
    workspace = await _require_workspace(repo, workspace_id)
    operation = await operations.create(
        workspace_id=workspace_id,
        operation_type=OperationType.cancel,
        status=OperationStatus.running,
        payload=payload.model_dump(),
    )
    if payload.stop_stack:
        await _stop_project(workspace.compose_project_name)
    if workspace.status != WorkspaceStatus.cancelled.value and WorkspaceStateMachine.can_transition(
        WorkspaceStatus(workspace.status),
        WorkspaceStatus.cancelled,
    ):
        await repo.transition(
            workspace, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_CANCEL"
        )
    else:
        await repo.add_event(
            workspace,
            event_type="workspace.cancel_requested",
            reason_code="OPERATOR_CANCEL",
            payload=payload.model_dump(),
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


@router.post("/stop", response_model=WorkspaceControlResponse)
async def stop_workspace(
    workspace_id: str,
    payload: WorkspaceControlRequest,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    repo = WorkspaceRepository(session)
    operations = OperationRepository(session)
    workspace = await _require_workspace(repo, workspace_id)
    operation = await operations.create(
        workspace_id=workspace_id,
        operation_type=OperationType.stop,
        status=OperationStatus.running,
        payload=payload.model_dump(),
    )
    await _stop_project(workspace.compose_project_name)
    if _is_active(WorkspaceStatus(workspace.status)) and WorkspaceStateMachine.can_transition(
        WorkspaceStatus(workspace.status),
        WorkspaceStatus.cancelled,
    ):
        await repo.transition(workspace, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_STOP")
    else:
        await repo.add_event(
            workspace,
            event_type="workspace.stack_stopped",
            reason_code="OPERATOR_STOP",
            payload=payload.model_dump(),
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


@router.delete("", response_model=WorkspaceControlResponse)
async def destroy_workspace(
    workspace_id: str,
    force: bool = False,
    remove_volumes: bool = True,
    remove_worktree: bool = True,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    del (
        remove_volumes,
        remove_worktree,
    )  # cleanup currently removes both; flags reserve the API contract.
    repo = WorkspaceRepository(session)
    operations = OperationRepository(session)
    workspace = await _require_workspace(repo, workspace_id)
    current = WorkspaceStatus(workspace.status)
    if _is_active(current) and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "WORKSPACE_ACTIVE",
                "message": "Active workspaces require force=true before destroy.",
            },
        )
    operation = await operations.create(
        workspace_id=workspace_id,
        operation_type=OperationType.destroy,
        status=OperationStatus.running,
        payload={"force": force},
    )
    if current == WorkspaceStatus.destroyed:
        await operations.finish(operation, status=OperationStatus.succeeded)
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
    await session.flush()
    cleaner = _cleaner()
    failures = await cleaner.cleanup(
        workspace_id=workspace_id,
        repo_url=workspace.repo_url,
        worktree_host_path=None,
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
        )
    else:
        if WorkspaceStateMachine.can_transition(
            WorkspaceStatus(workspace.status), WorkspaceStatus.destroyed
        ):
            await repo.transition(workspace, to=WorkspaceStatus.destroyed, reason_code="DESTROYED")
        await operations.finish(operation, status=OperationStatus.succeeded)
    return WorkspaceControlResponse(
        workspace_id=workspace_id,
        operation_id=operation.id,
        status=WorkspaceStatus(workspace.status),
        message="workspace destroyed" if not failures else "workspace cleanup failed",
    )


async def _require_workspace(repo: WorkspaceRepository, workspace_id: str) -> Workspace:
    workspace = await repo.get(workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return workspace


async def _stop_project(compose_project_name: str | None) -> None:
    if not compose_project_name:
        return
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={compose_project_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    ids = [line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines() if line]
    if not ids:
        return
    stop = await asyncio.create_subprocess_exec(
        "docker",
        "stop",
        *ids,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await stop.communicate()


def _cleaner() -> WorkspaceCleaner:
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    template = Path(__file__).resolve().parents[4] / "docker" / "compose" / "workspace.base.yml.j2"
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
