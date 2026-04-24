"""Workspace event observability endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import WorkspaceEventListResponse, WorkspaceEventResponse
from awf.db.repositories import WorkspaceRepository

router = APIRouter(prefix="/v1/events", tags=["events"])


@router.get("", response_model=WorkspaceEventListResponse)
async def list_events(
    workspace_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceEventListResponse:
    repo = WorkspaceRepository(session)
    rows = await repo.list_events(workspace_id=workspace_id, limit=limit)
    return WorkspaceEventListResponse(
        items=[WorkspaceEventResponse.model_validate(row) for row in rows],
        next_cursor=None,
        has_more=False,
    )
