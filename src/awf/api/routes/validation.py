"""Validation provenance endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import ValidationProvenanceListResponse
from awf.service.validation_provenance import (
    list_validation_provenance_response,
)

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/validation", tags=["validation"])

__all__ = ["list_validation_provenance"]


@router.get("", response_model=ValidationProvenanceListResponse)
async def list_validation_provenance(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ValidationProvenanceListResponse:
    response = await list_validation_provenance_response(
        session,
        workspace_id=workspace_id,
    )
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return response
