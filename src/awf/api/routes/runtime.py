"""Workspace runtime inspection endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import RuntimeServiceResponse, WorkspaceRuntimeResponse
from awf.db.repositories import WorkspaceRepository
from awf.runtime.inspection import RuntimeInspector

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/runtime", tags=["runtime"])


@router.get("", response_model=WorkspaceRuntimeResponse)
async def get_workspace_runtime(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceRuntimeResponse:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    snapshot = await RuntimeInspector().inspect(workspace.compose_project_name)
    return WorkspaceRuntimeResponse(
        workspace_id=workspace_id,
        compose_project_name=workspace.compose_project_name,
        stack_state=snapshot.stack_state,
        services=[
            RuntimeServiceResponse(
                name=s.name,
                container_id=s.container_id,
                image=s.image,
                state=s.state,
                status=s.status,
                health=s.health,
                ports=s.ports,
                started_at=s.started_at,
            )
            for s in snapshot.services
        ],
        logs_available=True,
        control_available=True,
        reason=snapshot.reason,
    )
