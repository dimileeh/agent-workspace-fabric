"""Read-only merge queue visualization endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import MergeQueueListResponse
from awf.db.enums import WorkspaceStatus
from awf.service.merge_queue import (
    InvalidMergeQueueCursorError,
    MergeQueueBlocker,
    _blocking_stale_reasons,
    _decode_cursor,
    _DecodedCursor,
    _encode_cursor,
    _has_blocking_policy_finding,
    _item_from_candidate,
    _item_from_legacy_workspace,
    _item_from_row,
    _latest_event,
    _legacy_workspace_merged_at,
    _load_active_policy_findings,
    _load_active_stale_reasons,
    _load_queue_blockers,
    _merge_blocker_reason,
    _merge_blocker_reason_from_workspace,
    _queue_blocker_response,
    _readiness_from_candidate,
    _required_stale_action,
    _row_workspace,
    _stale_reason_for_action,
    list_merge_queue_response,
)

router = APIRouter(prefix="/v1/merge-queue", tags=["merge-queue"])

__all__ = [
    "InvalidMergeQueueCursorError",
    "MergeQueueBlocker",
    "_DecodedCursor",
    "_blocking_stale_reasons",
    "_decode_cursor",
    "_encode_cursor",
    "_has_blocking_policy_finding",
    "_item_from_candidate",
    "_item_from_legacy_workspace",
    "_item_from_row",
    "_latest_event",
    "_legacy_workspace_merged_at",
    "_load_active_policy_findings",
    "_load_active_stale_reasons",
    "_load_queue_blockers",
    "_merge_blocker_reason",
    "_merge_blocker_reason_from_workspace",
    "_queue_blocker_response",
    "_readiness_from_candidate",
    "_required_stale_action",
    "_row_workspace",
    "_stale_reason_for_action",
    "list_merge_queue",
]


@router.get("", response_model=MergeQueueListResponse)
async def list_merge_queue(
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    base_branch: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> MergeQueueListResponse:
    try:
        return await list_merge_queue_response(
            session,
            repo_url=repo_url,
            base_branch=base_branch,
            workspace_status=workspace_status,
            limit=limit,
            cursor=cursor,
        )
    except InvalidMergeQueueCursorError as exc:
        raise HTTPException(
            status_code=fastapi_status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid merge queue cursor.",
            },
        ) from exc
