"""Workspace event observability endpoint.

Exposes ``GET /v1/events`` — the minimal read-side of the append-only audit
log. Events are returned newest-first with an optional ``workspace_id``
filter and a bounded ``limit``.

This is the "basic slice" of event observability: no SSE stream, no
cursor-based pagination. The response envelope already carries the
``next_cursor`` / ``has_more`` fields called out in the PRD so clients can
adopt the shape now, but both are constants (``None`` / ``False``) until
cursor pagination is wired in a later slice.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import WorkspaceEventListResponse, WorkspaceEventResponse
from awf.db.repositories import WorkspaceRepository

router = APIRouter(prefix="/v1/events", tags=["events"])


@router.get("", response_model=WorkspaceEventListResponse)
async def list_events(
    workspace_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceEventListResponse:
    repo = WorkspaceRepository(session)
    rows = await repo.list_events(workspace_id=workspace_id, limit=limit)
    return WorkspaceEventListResponse(
        items=[WorkspaceEventResponse.model_validate(r) for r in rows],
        next_cursor=None,
        has_more=False,
    )
