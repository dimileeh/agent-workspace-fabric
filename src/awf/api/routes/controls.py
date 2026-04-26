"""Sensitive workspace control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.schemas import WorkspaceControlRequest, WorkspaceControlResponse
from awf.service.controls import (
    ActiveWorkspaceDestroyError,
    WorkspaceControlError,
    WorkspaceControlService,
    WorkspaceNotFoundError,
    default_cleaner,
    stop_project_containers,
)

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
    try:
        return await _controls(session).cancel_workspace(
            workspace_id,
            reason=payload.reason,
            stop_stack=payload.stop_stack,
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post("/stop", response_model=WorkspaceControlResponse)
async def stop_workspace(
    workspace_id: str,
    payload: WorkspaceControlRequest,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    try:
        return await _controls(session).stop_workspace(
            workspace_id,
            reason=payload.reason,
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.delete("", response_model=WorkspaceControlResponse)
async def destroy_workspace(
    workspace_id: str,
    force: bool = False,
    remove_volumes: bool = True,
    remove_worktree: bool = True,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    try:
        return await _controls(session).destroy_workspace(
            workspace_id,
            force=force,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


def _controls(session: AsyncSession) -> WorkspaceControlService:
    return WorkspaceControlService(
        session,
        project_stopper=_stop_project,
        cleaner_factory=_cleaner,
    )


def _http_error(exc: WorkspaceControlError) -> HTTPException:
    if isinstance(exc, WorkspaceNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, ActiveWorkspaceDestroyError):
        status_code = status.HTTP_409_CONFLICT
    else:  # pragma: no cover - future control error subclasses
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=status_code,
        detail={"error_code": exc.error_code, "message": exc.message},
    )


_stop_project = stop_project_containers
_cleaner = default_cleaner
