"""Read-only merge queue visualization endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import (
    MergeBlockerReason,
    MergeQueueItemResponse,
    MergeQueueListResponse,
    WorkspaceEventResponse,
)
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository

router = APIRouter(prefix="/v1/merge-queue", tags=["merge-queue"])


@router.get("", response_model=MergeQueueListResponse)
async def list_merge_queue(
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    base_branch: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> MergeQueueListResponse:
    del cursor
    rows = await WorkspaceRepository(session).list_merge_queue(
        repo_url=repo_url,
        base_branch=base_branch,
        status=workspace_status,
        limit=limit,
    )
    return MergeQueueListResponse(items=[_item_from_workspace(row) for row in rows])


def _item_from_workspace(workspace: Workspace) -> MergeQueueItemResponse:
    latest_event = _latest_event(workspace.events)
    pr_url = workspace.pr_url
    if pr_url is None:  # pragma: no cover - filtered at repository boundary
        raise ValueError("merge queue rows must have a PR URL")
    return MergeQueueItemResponse(
        workspace_id=workspace.id,
        title=workspace.task_title,
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        branch_name=workspace.branch_name,
        pr_url=pr_url,
        status=WorkspaceStatus(workspace.status),
        auto_merge=workspace.auto_merge,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        last_event=(
            WorkspaceEventResponse.model_validate(latest_event)
            if latest_event is not None
            else None
        ),
        merge_blocker_reason=_merge_blocker_reason(workspace),
    )


def _latest_event(events: list[WorkspaceEvent]) -> WorkspaceEvent | None:
    return max(events, key=lambda event: event.occurred_at, default=None)


def _merge_blocker_reason(workspace: Workspace) -> MergeBlockerReason:
    workspace_status = WorkspaceStatus(workspace.status)
    if workspace_status == WorkspaceStatus.monitoring_pr:
        if workspace.auto_merge:
            return "ready_to_merge_or_waiting_for_github"
        return "manual_merge_required"
    if workspace_status == WorkspaceStatus.pushing:
        return "waiting_for_monitor"
    if workspace_status == WorkspaceStatus.completed:
        return "completed"
    if workspace_status in {WorkspaceStatus.failed, WorkspaceStatus.cancelled}:
        return "failed_or_cancelled"
    return "workspace_not_terminal"
