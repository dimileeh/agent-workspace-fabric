"""Read-only merge queue visualization endpoint."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import (
    MergeBlockerReason,
    MergeCandidateReadinessResponse,
    MergeQueueItemResponse,
    MergeQueueListResponse,
    WorkspaceEventResponse,
)
from awf.db.enums import WorkspaceStatus
from awf.db.models import MergeCandidate, Workspace, WorkspaceEvent
from awf.db.repositories import MergeCandidateRepository, WorkspaceRepository

router = APIRouter(prefix="/v1/merge-queue", tags=["merge-queue"])


@dataclass(frozen=True)
class _DecodedCursor:
    updated_at: datetime
    workspace_id: str


class InvalidMergeQueueCursorError(ValueError):
    """Raised when a merge queue pagination cursor cannot be decoded."""


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
        decoded_cursor = _decode_cursor(cursor)
    except InvalidMergeQueueCursorError as exc:
        raise HTTPException(
            status_code=fastapi_status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid merge queue cursor.",
            },
        ) from exc
    candidate_rows = await MergeCandidateRepository(session).list_queue(
        repo_url=repo_url,
        base_branch=base_branch,
        status=workspace_status,
        before_updated_at=decoded_cursor.updated_at if decoded_cursor is not None else None,
        before_workspace_id=decoded_cursor.workspace_id if decoded_cursor is not None else None,
        limit=limit + 1,
    )
    legacy_rows = await WorkspaceRepository(session).list_merge_queue_without_candidates(
        repo_url=repo_url,
        base_branch=base_branch,
        status=workspace_status,
        before_updated_at=decoded_cursor.updated_at if decoded_cursor is not None else None,
        before_workspace_id=decoded_cursor.workspace_id if decoded_cursor is not None else None,
        limit=limit + 1,
    )
    queue_rows: list[MergeCandidate | Workspace] = [*candidate_rows, *legacy_rows]
    rows = sorted(
        queue_rows,
        key=lambda row: (_row_workspace(row).updated_at, _row_workspace(row).id),
        reverse=True,
    )
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    return MergeQueueListResponse(
        items=[_item_from_row(row) for row in page_rows],
        next_cursor=_encode_cursor(_row_workspace(page_rows[-1]))
        if has_more and page_rows
        else None,
        has_more=has_more,
    )


def _item_from_row(row: MergeCandidate | Workspace) -> MergeQueueItemResponse:
    if isinstance(row, MergeCandidate):
        return _item_from_candidate(row)
    return _item_from_legacy_workspace(row)


def _item_from_candidate(candidate: MergeCandidate) -> MergeQueueItemResponse:
    workspace = candidate.workspace
    latest_event = _latest_event(workspace.events)
    reason, action = _merge_blocker_reason(candidate)
    return MergeQueueItemResponse(
        candidate_id=candidate.id,
        candidate_status=candidate.status,
        close_reason=candidate.close_reason,
        attempt_id=candidate.attempt_id,
        task_id=candidate.task_id,
        workspace_id=workspace.id,
        title=workspace.task_title,
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        branch_name=workspace.branch_name,
        pr_url=candidate.pr_url,
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
        merge_blocker_reason=reason,
        required_next_action=action,
        readiness=_readiness_from_candidate(candidate),
        canonical=candidate.attempt.is_canonical_for_merge,
    )


def _item_from_legacy_workspace(workspace: Workspace) -> MergeQueueItemResponse:
    latest_event = _latest_event(workspace.events)
    pr_url = workspace.pr_url
    if pr_url is None:  # pragma: no cover - filtered at repository boundary
        raise ValueError("legacy merge queue rows must have a PR URL")
    reason, action = _merge_blocker_reason_from_workspace(workspace)
    return MergeQueueItemResponse(
        candidate_id=None,
        candidate_status=None,
        close_reason=None,
        attempt_id=None,
        task_id=workspace.task_external_id or workspace.id,
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
        merge_blocker_reason=reason,
        required_next_action=action,
        readiness=None,
        canonical=False,
    )


def _row_workspace(row: MergeCandidate | Workspace) -> Workspace:
    if isinstance(row, MergeCandidate):
        return row.workspace
    return row


def _latest_event(events: list[WorkspaceEvent]) -> WorkspaceEvent | None:
    return events[-1] if events else None


def _merge_blocker_reason(candidate: MergeCandidate) -> tuple[MergeBlockerReason, str | None]:
    if candidate.completed:
        return "completed", None
    if candidate.failed_or_cancelled:
        return "failed_or_cancelled", None
    if candidate.not_canonical:
        return "not_canonical", None
    if candidate.stale:
        reason = candidate.stale_reason or "stale"
        action = "rebase" if "validation" not in reason else "validate"
        return "stale", action
    if candidate.manual_merge_required:
        return "manual_merge_required", None
    if candidate.waiting_for_monitor:
        return "waiting_for_monitor", None
    if candidate.ready:
        return "ready_to_merge_or_waiting_for_github", None
    return "workspace_not_terminal", None


def _merge_blocker_reason_from_workspace(workspace: Workspace) -> tuple[MergeBlockerReason, str | None]:
    workspace_status = WorkspaceStatus(workspace.status)
    if workspace_status == WorkspaceStatus.monitoring_pr:
        if workspace.auto_merge:
            return "ready_to_merge_or_waiting_for_github", None
        return "manual_merge_required", None
    if workspace_status == WorkspaceStatus.pushing:
        return "waiting_for_monitor", None
    if workspace_status == WorkspaceStatus.completed:
        return "completed", None
    if workspace_status in {WorkspaceStatus.failed, WorkspaceStatus.cancelled}:
        return "failed_or_cancelled", None
    return "workspace_not_terminal", None


def _readiness_from_candidate(candidate: MergeCandidate) -> MergeCandidateReadinessResponse:
    return MergeCandidateReadinessResponse(
        ready=candidate.ready,
        manual_merge_required=candidate.manual_merge_required,
        waiting_for_monitor=candidate.waiting_for_monitor,
        failed_or_cancelled=candidate.failed_or_cancelled,
        completed=candidate.completed,
        not_canonical=candidate.not_canonical,
        stale=candidate.stale,
        stale_reason=candidate.stale_reason,
    )


def _encode_cursor(workspace: Workspace) -> str:
    payload = {
        "u": workspace.updated_at.isoformat(),
        "id": workspace.id,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii")


def _decode_cursor(cursor: str | None) -> _DecodedCursor | None:
    if cursor is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        updated_at = datetime.fromisoformat(payload["u"])
        workspace_id = payload["id"]
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidMergeQueueCursorError("Invalid merge queue cursor") from exc
    if not isinstance(workspace_id, str) or workspace_id == "":
        raise InvalidMergeQueueCursorError("Invalid merge queue cursor")
    return _DecodedCursor(updated_at=updated_at, workspace_id=workspace_id)
