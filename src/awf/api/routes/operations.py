"""Operation status endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import OperationListResponse, OperationResponse
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository, WorkspaceRepository

router = APIRouter(tags=["operations"])


@router.get("/v1/operations", response_model=OperationListResponse)
async def list_operations(
    workspace_id: str | None = None,
    status: OperationStatus | None = None,
    operation_type: Annotated[OperationType | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> OperationListResponse:
    rows = await OperationRepository(session).list_all(
        workspace_id=workspace_id,
        status=status,
        operation_type=operation_type,
        limit=limit,
    )
    return OperationListResponse(
        items=[OperationResponse.model_validate(row) for row in rows],
        next_cursor=None,
        has_more=False,
    )


@router.get("/v1/operations/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    operation = await OperationRepository(session).get(operation_id)
    if operation is None:
        raise HTTPException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No operation with id {operation_id}"},
        )
    return OperationResponse.model_validate(operation)


@router.get("/v1/workspaces/{workspace_id}/operations", response_model=OperationListResponse)
async def list_workspace_operations(
    workspace_id: str,
    status: OperationStatus | None = None,
    operation_type: Annotated[OperationType | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> OperationListResponse:
    if not await WorkspaceRepository(session).exists(workspace_id):
        raise HTTPException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    rows = await OperationRepository(session).list_for_workspace(
        workspace_id,
        status=status,
        operation_type=operation_type,
        limit=limit,
    )
    return OperationListResponse(
        items=[OperationResponse.model_validate(row) for row in rows],
        next_cursor=None,
        has_more=False,
    )
