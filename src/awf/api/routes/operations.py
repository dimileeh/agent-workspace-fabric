"""Operation status endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import OperationListResponse, OperationResponse
from awf.db.repositories import OperationRepository

router = APIRouter(tags=["operations"])


@router.get("/v1/operations/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    operation = await OperationRepository(session).get(operation_id)
    if operation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No operation with id {operation_id}"},
        )
    return OperationResponse.model_validate(operation)


@router.get("/v1/workspaces/{workspace_id}/operations", response_model=OperationListResponse)
async def list_workspace_operations(
    workspace_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> OperationListResponse:
    rows = await OperationRepository(session).list_for_workspace(workspace_id, limit=limit)
    return OperationListResponse(items=[OperationResponse.model_validate(row) for row in rows])
