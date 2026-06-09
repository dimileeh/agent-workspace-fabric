"""Workspace event listing endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import WorkspaceEventListResponse, WorkspaceEventResponse
from awf.db.repositories import WorkspaceEventRepository

router = APIRouter(
    prefix="/v1/events",
    tags=["events"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


@router.get("", response_model=WorkspaceEventListResponse)
async def list_events(
    workspace_id: str | None = None,
    event_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceEventListResponse:
    repo = WorkspaceEventRepository(session)
    rows = await repo.list(workspace_id=workspace_id, event_type=event_type, limit=limit + 1)
    has_more = len(rows) > limit
    items = rows[:limit]
    return WorkspaceEventListResponse(
        items=[WorkspaceEventResponse.model_validate(row) for row in items],
        has_more=has_more,
        limit=limit,
        cursor=None,
    )
