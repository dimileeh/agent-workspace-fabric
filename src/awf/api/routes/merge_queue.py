"""Read-only merge queue visualization endpoint."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import (
    MergeBlockerReason,
    MergeCandidateReadinessResponse,
    MergeQueueBlockerResponse,
    MergeQueueBlockerState,
    MergeQueueItemResponse,
    MergeQueueListResponse,
    PolicyFindingResponse,
    StaleReasonResponse,
    ValidationRunSummaryResponse,
    WorkspaceEventResponse,
)
from awf.api.validation_runs import validation_run_summary
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    MergeCandidate,
    PolicyFinding,
    StaleReason,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories import (
    MergeCandidateRepository,
    PolicyFindingRepository,
    StaleReasonRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.runtime.merge_eligibility import (
    DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
)
from awf.service.merge_queue import (
    MergeQueueBlocker,
    list_merge_queue_blockers_for_candidates,
)

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
    latest_validation_runs = await ValidationRunRepository(session).latest_by_workspace_ids(
        _row_workspace(row).id for row in page_rows
    )

    stale_reasons_by_candidate = await _load_active_stale_reasons(session, page_rows)
    policy_findings_by_candidate = await _load_active_policy_findings(session, page_rows)
    blockers_by_candidate = await _load_queue_blockers(session, page_rows)

    return MergeQueueListResponse(
        items=[
            _item_from_row(
                row,
                latest_validation_runs.get(_row_workspace(row).id),
                stale_reasons_by_candidate,
                policy_findings_by_candidate,
                blockers_by_candidate,
            )
            for row in page_rows
        ],
        next_cursor=_encode_cursor(_row_workspace(page_rows[-1]))
        if has_more and page_rows
        else None,
        has_more=has_more,
    )


async def _load_active_stale_reasons(
    session: AsyncSession,
    rows: list[MergeCandidate | Workspace],
) -> dict[str, list[StaleReason]]:
    candidate_ids = [row.id for row in rows if isinstance(row, MergeCandidate)]
    if not candidate_ids:
        return {}
    return await StaleReasonRepository(session).list_active_for_candidates(candidate_ids)


async def _load_active_policy_findings(
    session: AsyncSession,
    rows: list[MergeCandidate | Workspace],
) -> dict[str, list[PolicyFinding]]:
    candidate_ids = [row.id for row in rows if isinstance(row, MergeCandidate)]
    if not candidate_ids:
        return {}
    return await PolicyFindingRepository(session).list_active_for_candidates(candidate_ids)


async def _load_queue_blockers(
    session: AsyncSession,
    rows: list[MergeCandidate | Workspace],
) -> dict[str, list[MergeQueueBlocker]]:
    candidate_ids = [row.id for row in rows if isinstance(row, MergeCandidate)]
    return await list_merge_queue_blockers_for_candidates(
        session,
        candidate_ids=candidate_ids,
    )


def _item_from_row(
    row: MergeCandidate | Workspace,
    latest_validation_run: ValidationRun | None,
    stale_reasons_by_candidate: dict[str, list[StaleReason]],
    policy_findings_by_candidate: dict[str, list[PolicyFinding]],
    blockers_by_candidate: dict[str, list[MergeQueueBlocker]],
) -> MergeQueueItemResponse:
    if isinstance(row, MergeCandidate):
        return _item_from_candidate(
            row,
            latest_validation_run,
            stale_reasons=stale_reasons_by_candidate.get(row.id, []),
            policy_findings=policy_findings_by_candidate.get(row.id, []),
            queue_blockers=blockers_by_candidate.get(row.id, []),
        )
    return _item_from_legacy_workspace(row, latest_validation_run)


def _item_from_candidate(
    candidate: MergeCandidate,
    latest_validation_run: ValidationRun | None,
    *,
    stale_reasons: list[StaleReason],
    policy_findings: list[PolicyFinding],
    queue_blockers: list[MergeQueueBlocker],
) -> MergeQueueItemResponse:
    workspace = candidate.workspace
    latest_event = _latest_event(workspace.events)
    reason, action = _merge_blocker_reason(
        candidate,
        policy_findings=policy_findings,
        queue_blockers=queue_blockers,
    )
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
        readiness=_readiness_from_candidate(candidate, policy_findings=policy_findings),
        canonical=candidate.attempt.is_canonical_for_merge,
        queue_blockers=[_queue_blocker_response(blocker) for blocker in queue_blockers],
        latest_validation=_latest_validation_summary(
            latest_validation_run,
            current_target_head_sha=candidate.head_sha or workspace.monitor_last_commit_sha,
        ),
        stale_reasons=[StaleReasonResponse.model_validate(r) for r in stale_reasons],
        policy_findings=[
            PolicyFindingResponse.model_validate(finding) for finding in policy_findings
        ],
    )


def _item_from_legacy_workspace(
    workspace: Workspace,
    latest_validation_run: ValidationRun | None,
) -> MergeQueueItemResponse:
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
        queue_blockers=[],
        latest_validation=_latest_validation_summary(
            latest_validation_run,
            current_target_head_sha=workspace.monitor_last_commit_sha,
        ),
        stale_reasons=[],
        policy_findings=[],
    )


def _row_workspace(row: MergeCandidate | Workspace) -> Workspace:
    if isinstance(row, MergeCandidate):
        return row.workspace
    return row


def _latest_event(events: list[WorkspaceEvent]) -> WorkspaceEvent | None:
    return events[-1] if events else None


def _queue_blocker_response(blocker: MergeQueueBlocker) -> MergeQueueBlockerResponse:
    return MergeQueueBlockerResponse(
        candidate_id=blocker.candidate_id,
        workspace_id=blocker.workspace_id,
        attempt_id=blocker.attempt_id,
        task_id=blocker.task_id,
        title=blocker.title,
        pr_url=blocker.pr_url,
        pr_number=blocker.pr_number,
        status=WorkspaceStatus(blocker.status),
        blocker_state=cast(MergeQueueBlockerState, blocker.blocker_state),
        reason_code=blocker.reason_code,
    )


def _merge_blocker_reason(
    candidate: MergeCandidate,
    *,
    policy_findings: list[PolicyFinding],
    queue_blockers: list[MergeQueueBlocker],
) -> tuple[MergeBlockerReason, str | None]:
    if candidate.completed:
        return "completed", None
    if candidate.failed_or_cancelled:
        return "failed_or_cancelled", None
    if candidate.not_canonical:
        return "not_canonical", None
    if candidate.policy_blocked or _has_blocking_policy_finding(policy_findings):
        return "policy_blocked", "resolve_policy_findings"
    if candidate.stale:
        reason = candidate.stale_reason or "stale"
        action = _required_stale_action(reason)
        return "stale", action
    if candidate.manual_merge_required:
        return "manual_merge_required", None
    if candidate.waiting_for_monitor:
        return "waiting_for_monitor", None
    if queue_blockers:
        return "waiting_for_older_candidate", "wait_for_queue"
    if candidate.ready:
        return "ready_to_merge_or_waiting_for_github", None
    return "workspace_not_terminal", None


def _required_stale_action(reason: str) -> str:
    if reason == VALIDATION_INSUFFICIENT_TIER_STALE_REASON:
        return "validate"
    if reason == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON:
        return "resolve_task_scope"
    return "rebase"


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


def _readiness_from_candidate(
    candidate: MergeCandidate,
    *,
    policy_findings: list[PolicyFinding],
) -> MergeCandidateReadinessResponse:
    policy_blocked = candidate.policy_blocked or _has_blocking_policy_finding(policy_findings)
    return MergeCandidateReadinessResponse(
        ready=candidate.ready and not policy_blocked,
        manual_merge_required=candidate.manual_merge_required,
        waiting_for_monitor=candidate.waiting_for_monitor,
        failed_or_cancelled=candidate.failed_or_cancelled,
        completed=candidate.completed,
        not_canonical=candidate.not_canonical,
        stale=candidate.stale,
        stale_reason=candidate.stale_reason,
    )


def _has_blocking_policy_finding(policy_findings: list[PolicyFinding]) -> bool:
    return any(finding.severity == "blocking" for finding in policy_findings)


def _latest_validation_summary(
    run: ValidationRun | None,
    *,
    current_target_head_sha: str | None,
) -> ValidationRunSummaryResponse | None:
    if run is None:
        return None
    return validation_run_summary(run, current_target_head_sha=current_target_head_sha)


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
