"""Operation status endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import OperationListResponse, OperationResponse
from awf.db.enums import OperationStatus, OperationType
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.operations import build_operation_list_response
from awf.service.workspaces import WorkspaceService

router = APIRouter(
    tags=["operations"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)
_INVALID_OPERATION_CURSOR_DETAIL = {
    "error_code": "INVALID_CURSOR",
    "message": "Invalid operation list cursor.",
}


@router.get("/v1/operations", response_model=OperationListResponse)
async def list_operations(
    workspace_id: str | None = None,
    status: OperationStatus | None = None,
    operation_type: Annotated[OperationType | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> OperationListResponse:
    try:
        page = await WorkspaceService(session_factory).list_all_operations_page(
            workspace_id=workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit + 1,
            cursor=cursor,
        )
        return build_operation_list_response(
            page.rows,
            limit=limit,
            cursor=cursor,
        )
    except InvalidBoundedListCursorError as exc:
        raise _invalid_operation_cursor() from exc


@router.get("/v1/operations/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> OperationResponse:
    operation = await WorkspaceService(session_factory).get_operation(operation_id)
    if operation is None:
        raise HTTPException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No operation with id {operation_id}"},
        )
    return operation


@router.get("/v1/workspaces/{workspace_id}/operations", response_model=OperationListResponse)
async def list_workspace_operations(
    workspace_id: str,
    status: OperationStatus | None = None,
    operation_type: Annotated[OperationType | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> OperationListResponse:
    try:
        page = await WorkspaceService(session_factory).list_operations_page(
            workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit + 1,
            cursor=cursor,
        )
        if page is None:
            raise HTTPException(
                status_code=fastapi_status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "NOT_FOUND",
                    "message": f"No workspace with id {workspace_id}",
                },
            )
        return build_operation_list_response(
            page.rows,
            limit=limit,
            cursor=cursor,
        )
    except InvalidBoundedListCursorError as exc:
        raise _invalid_operation_cursor() from exc


def _invalid_operation_cursor() -> HTTPException:
    return HTTPException(
        status_code=fastapi_status.HTTP_400_BAD_REQUEST,
        detail=_INVALID_OPERATION_CURSOR_DETAIL,
    )
