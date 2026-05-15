"""Validation provenance endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import ValidationProvenanceListResponse
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.validation_provenance import (
    DEFAULT_VALIDATION_PROVENANCE_LIMIT,
    MAX_VALIDATION_PROVENANCE_LIMIT,
    list_validation_provenance_response,
)

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/validation",
    tags=["validation"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)

__all__ = ["list_validation_provenance"]


@router.get("", response_model=ValidationProvenanceListResponse)
async def list_validation_provenance(
    workspace_id: str,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_VALIDATION_PROVENANCE_LIMIT,
            description="Maximum validation provenance records to return.",
        ),
    ] = DEFAULT_VALIDATION_PROVENANCE_LIMIT,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> ValidationProvenanceListResponse:
    try:
        response = await list_validation_provenance_response(
            session,
            workspace_id=workspace_id,
            limit=limit,
            cursor=cursor,
        )
    except InvalidBoundedListCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid validation provenance cursor.",
            },
        ) from exc
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return response
