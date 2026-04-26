"""Read-only operational metrics endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.service.metrics import (
    DEFAULT_SUMMARY_WINDOW_HOURS,
    MAX_SUMMARY_WINDOW_HOURS,
    MIN_SUMMARY_WINDOW_HOURS,
    summarize_workspace_reliability_for_session,
)

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])


class WorkspaceReliabilitySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    window_start: datetime
    since_hours: int
    status_counts: dict[str, int]
    failure_reason_counts: dict[str, int]
    active_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    destroyed_count: int
    cleanup_failure_count: int


@router.get(
    "/workspaces/summary",
    response_model=WorkspaceReliabilitySummaryResponse,
)
async def get_workspace_reliability_summary(
    since_hours: Annotated[
        int,
        Query(
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
    ] = DEFAULT_SUMMARY_WINDOW_HOURS,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceReliabilitySummaryResponse:
    summary = await summarize_workspace_reliability_for_session(
        session,
        since_hours=since_hours,
    )
    return WorkspaceReliabilitySummaryResponse.model_validate(summary)
