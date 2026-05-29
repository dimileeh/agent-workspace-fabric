"""Merge-queue ordering policy helpers."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from typing import cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.api.schemas import (
    FallbackTargetResponse,
    MergeBlockerReason,
    MergeCandidateReadinessResponse,
    MergeQueueBlockerResponse,
    MergeQueueBlockerState,
    MergeQueueItemResponse,
    MergeQueueListResponse,
    PolicyFindingResponse,
    ProviderRecoveryStateResponse,
    StaleReasonResponse,
    WorkspaceEventResponse,
)
from awf.common.owned_paths import interworkspace_owned_paths
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import (
    MergeCandidate,
    Operation,
    PolicyFinding,
    StaleReason,
    TaskAttempt,
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
    owned_paths_overlap,
)
from awf.runtime.merge_eligibility import (
    DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
    VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
    stale_reason_blocks_merge,
    stale_reason_required_action,
)
from awf.service.provider_recovery import provider_recovery_state_for_workspace
from awf.service.validation_observability import validation_freshness_summary

MERGE_QUEUE_WAIT_REASON_CODE = "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE"

_RECOVERY_OPERATION_TYPES = {
    OperationType.validate.value,
    OperationType.rebase.value,
}
_ACTIVE_OPERATION_STATUSES = {
    OperationStatus.pending.value,
    OperationStatus.running.value,
}
_MONITOR_RECOVERY_STATUSES = {
    WorkspaceStatus.ready,
    WorkspaceStatus.running,
    WorkspaceStatus.validating,
    WorkspaceStatus.pushing,
}


@dataclass(frozen=True)
class _DecodedCursor:
    updated_at: datetime
    workspace_id: str


class InvalidMergeQueueCursorError(ValueError):
    """Raised when a merge queue pagination cursor cannot be decoded."""


@dataclass(frozen=True)
class MergeQueueBlocker:
    candidate_id: str
    workspace_id: str
    attempt_id: str
    task_id: str
    title: str
    pr_url: str
    pr_number: int | None
    status: str
    blocker_state: str
    reason_code: str = MERGE_QUEUE_WAIT_REASON_CODE

    def event_payload(self, *, repo_url: str, base_branch: str) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "repo_url": repo_url,
            "base_branch": base_branch,
            "blocker_candidate_id": self.candidate_id,
            "blocker_workspace_id": self.workspace_id,
            "blocker_pr_url": self.pr_url,
            "blocker_pr_number": self.pr_number,
            "blocker_title": self.title,
            "blocker_status": self.status,
            "blocker_state": self.blocker_state,
        }


async def list_merge_queue_response(
    session: AsyncSession,
    *,
    repo_url: str | None = None,
    base_branch: str | None = None,
    workspace_status: WorkspaceStatus | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> MergeQueueListResponse:
    decoded_cursor = _decode_cursor(cursor)
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
    candidate_iter: Iterable[MergeCandidate | Workspace] = candidate_rows
    legacy_iter: Iterable[MergeCandidate | Workspace] = legacy_rows
    rows = sorted(
        chain(candidate_iter, legacy_iter),
        key=lambda row: (_row_workspace(row).updated_at, _row_workspace(row).id),
        reverse=True,
    )
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    validation_runs_by_workspace = await ValidationRunRepository(session).list_by_workspace_ids(
        _row_workspace(row).id for row in page_rows
    )

    stale_reasons_by_candidate = await _load_active_stale_reasons(session, page_rows)
    policy_findings_by_candidate = await _load_active_policy_findings(session, page_rows)
    blockers_by_candidate = await _load_queue_blockers(session, page_rows)

    return MergeQueueListResponse(
        items=[
            _item_from_row(
                row,
                validation_runs_by_workspace.get(_row_workspace(row).id, []),
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
        limit=limit,
        cursor=cursor,
    )


async def list_merge_queue_blockers_for_candidate(
    session: AsyncSession,
    *,
    candidate_id: str,
) -> list[MergeQueueBlocker]:
    """Return older same repo/base candidates that must integrate first."""

    candidate = await _load_candidate(session, candidate_id)
    if candidate is None or not _is_merge_ready_candidate(candidate):
        return []

    rows = await _load_older_open_candidates(session, candidate)
    blockers: list[MergeQueueBlocker] = []
    for row in rows:
        if not _candidate_blocks_target(row, candidate):
            continue
        blocker_state = _blocking_state(row)
        if blocker_state is None:
            continue
        blockers.append(_blocker_from_candidate(row, blocker_state=blocker_state))
    return blockers


async def list_merge_queue_blockers_for_candidates(
    session: AsyncSession,
    *,
    candidate_ids: Iterable[str],
) -> dict[str, list[MergeQueueBlocker]]:
    """Return older same repo/base blockers for a batch of candidates."""

    candidate_id_list = list(dict.fromkeys(candidate_ids))
    blockers_by_candidate: dict[str, list[MergeQueueBlocker]] = {
        candidate_id: [] for candidate_id in candidate_id_list
    }
    if not candidate_id_list:
        return blockers_by_candidate

    candidates = await _load_candidates(session, candidate_id_list)
    merge_ready_candidates = [
        candidate for candidate in candidates if _is_merge_ready_candidate(candidate)
    ]
    if not merge_ready_candidates:
        return blockers_by_candidate

    blocker_pool = await _load_older_open_candidate_pool(session, merge_ready_candidates)
    blocker_pool_by_repo_base: dict[tuple[str, str], list[MergeCandidate]] = {}
    for blocker_candidate in blocker_pool:
        key = (blocker_candidate.repo_url, blocker_candidate.base_branch)
        blocker_pool_by_repo_base.setdefault(key, []).append(blocker_candidate)

    for candidate in merge_ready_candidates:
        blockers = blockers_by_candidate[candidate.id]
        repo_base = (candidate.repo_url, candidate.base_branch)
        for blocker_candidate in blocker_pool_by_repo_base.get(repo_base, []):
            if blocker_candidate.id == candidate.id or not _is_older_candidate(
                blocker_candidate,
                candidate,
            ):
                continue
            if not _candidate_blocks_target(blocker_candidate, candidate):
                continue
            blocker_state = _blocking_state(blocker_candidate)
            if blocker_state is None:
                continue
            blockers.append(_blocker_from_candidate(blocker_candidate, blocker_state=blocker_state))
    return blockers_by_candidate


async def list_merge_queue_blockers_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: str,
) -> list[MergeQueueBlocker]:
    candidate = await _load_open_candidate_for_workspace(session, workspace_id)
    if candidate is None:
        return []
    return await list_merge_queue_blockers_for_candidate(session, candidate_id=candidate.id)


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
    validation_runs: list[ValidationRun],
    stale_reasons_by_candidate: dict[str, list[StaleReason]],
    policy_findings_by_candidate: dict[str, list[PolicyFinding]],
    blockers_by_candidate: dict[str, list[MergeQueueBlocker]],
) -> MergeQueueItemResponse:
    if isinstance(row, MergeCandidate):
        return _item_from_candidate(
            row,
            validation_runs,
            stale_reasons=stale_reasons_by_candidate.get(row.id, []),
            policy_findings=policy_findings_by_candidate.get(row.id, []),
            queue_blockers=blockers_by_candidate.get(row.id, []),
        )
    return _item_from_legacy_workspace(row, validation_runs)


def _item_from_candidate(
    candidate: MergeCandidate,
    validation_runs: list[ValidationRun],
    *,
    stale_reasons: list[StaleReason],
    policy_findings: list[PolicyFinding],
    queue_blockers: list[MergeQueueBlocker],
) -> MergeQueueItemResponse:
    workspace = candidate.workspace
    latest_event = _latest_event(workspace.events)
    reason, action = _merge_blocker_reason(
        candidate,
        stale_reasons=stale_reasons,
        policy_findings=policy_findings,
        queue_blockers=queue_blockers,
    )
    validation_summary = validation_freshness_summary(
        workspace,
        validation_runs,
        candidate=candidate,
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
        merged_at=candidate.merged_at,
        last_event=(
            WorkspaceEventResponse.model_validate(latest_event)
            if latest_event is not None
            else None
        ),
        merge_blocker_reason=reason,
        required_next_action=action,
        required_validation_tier=validation_summary.required_tier,
        latest_satisfied_validation_tier=validation_summary.latest_satisfied_tier,
        validation_freshness_status=validation_summary.freshness_status,
        validation_reason_code=validation_summary.reason_code,
        readiness=_readiness_from_candidate(candidate, policy_findings=policy_findings),
        canonical=candidate.attempt.is_canonical_for_merge,
        queue_blockers=[_queue_blocker_response(blocker) for blocker in queue_blockers],
        latest_validation=validation_summary.latest_validation,
        stale_reasons=[StaleReasonResponse.model_validate(r) for r in stale_reasons],
        policy_findings=[
            PolicyFindingResponse.model_validate(finding) for finding in policy_findings
        ],
        provider_recovery_state=_provider_recovery_state_response(workspace),
    )


def _item_from_legacy_workspace(
    workspace: Workspace,
    validation_runs: list[ValidationRun],
) -> MergeQueueItemResponse:
    latest_event = _latest_event(workspace.events)
    pr_url = workspace.pr_url
    if pr_url is None:  # pragma: no cover - filtered at repository boundary
        raise ValueError("legacy merge queue rows must have a PR URL")
    reason, action = _merge_blocker_reason_from_workspace(workspace)
    validation_summary = validation_freshness_summary(
        workspace,
        validation_runs,
        candidate=None,
    )
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
        merged_at=_legacy_workspace_merged_at(workspace),
        last_event=(
            WorkspaceEventResponse.model_validate(latest_event)
            if latest_event is not None
            else None
        ),
        merge_blocker_reason=reason,
        required_next_action=action,
        required_validation_tier=validation_summary.required_tier,
        latest_satisfied_validation_tier=validation_summary.latest_satisfied_tier,
        validation_freshness_status=validation_summary.freshness_status,
        validation_reason_code=validation_summary.reason_code,
        readiness=None,
        canonical=False,
        queue_blockers=[],
        latest_validation=validation_summary.latest_validation,
        stale_reasons=[],
        policy_findings=[],
        provider_recovery_state=_provider_recovery_state_response(workspace),
    )


def _row_workspace(row: MergeCandidate | Workspace) -> Workspace:
    if isinstance(row, MergeCandidate):
        return row.workspace
    return row


def _latest_event(events: list[WorkspaceEvent]) -> WorkspaceEvent | None:
    return events[-1] if events else None


def _legacy_workspace_merged_at(workspace: Workspace) -> datetime | None:
    completed_events = [
        event.occurred_at
        for event in workspace.events
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.completed.value
    ]
    if completed_events:
        return max(completed_events)
    if WorkspaceStatus(workspace.status) == WorkspaceStatus.completed:
        return workspace.updated_at
    return None


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
    stale_reasons: list[StaleReason],
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
    blocking_stale_reasons = _blocking_stale_reasons(stale_reasons)
    if candidate.stale or blocking_stale_reasons:
        reason = _stale_reason_for_action(
            candidate,
            stale_reasons=blocking_stale_reasons,
        )
        if stale_reason_blocks_merge(reason):
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


def _stale_reason_for_action(
    candidate: MergeCandidate,
    *,
    stale_reasons: list[StaleReason],
) -> str:
    legacy_reason = candidate.stale_reason or ""
    if legacy_reason in (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
        DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
    ):
        return legacy_reason
    if stale_reasons:
        return stale_reasons[0].reason_code
    return legacy_reason or "stale"


def _required_stale_action(reason: str) -> str:
    return stale_reason_required_action(reason) or "rebase"


def _blocking_stale_reasons(stale_reasons: list[StaleReason]) -> list[StaleReason]:
    return [reason for reason in stale_reasons if reason.blocks_merge]


def _merge_blocker_reason_from_workspace(
    workspace: Workspace,
) -> tuple[MergeBlockerReason, str | None]:
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


async def _load_candidates(
    session: AsyncSession,
    candidate_ids: list[str],
) -> list[MergeCandidate]:
    stmt = (
        select(MergeCandidate)
        .where(MergeCandidate.id.in_(candidate_ids))
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.task),
        )
    )
    return list((await session.execute(stmt)).scalars())


async def _load_candidate(
    session: AsyncSession,
    candidate_id: str,
) -> MergeCandidate | None:
    stmt = (
        select(MergeCandidate)
        .where(MergeCandidate.id == candidate_id)
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.task),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_open_candidate_for_workspace(
    session: AsyncSession,
    workspace_id: str,
) -> MergeCandidate | None:
    stmt = (
        select(MergeCandidate)
        .where(
            MergeCandidate.workspace_id == workspace_id,
            MergeCandidate.status == "open",
        )
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.task),
        )
        .order_by(MergeCandidate.created_at.desc(), MergeCandidate.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_older_open_candidates(
    session: AsyncSession,
    candidate: MergeCandidate,
) -> list[MergeCandidate]:
    stmt = (
        select(MergeCandidate)
        .join(TaskAttempt, MergeCandidate.attempt_id == TaskAttempt.id)
        .where(
            MergeCandidate.status == "open",
            MergeCandidate.repo_url == candidate.repo_url,
            MergeCandidate.base_branch == candidate.base_branch,
            MergeCandidate.id != candidate.id,
            TaskAttempt.is_canonical_for_merge.is_(True),
            or_(
                MergeCandidate.created_at < candidate.created_at,
                and_(
                    MergeCandidate.created_at == candidate.created_at,
                    MergeCandidate.id < candidate.id,
                ),
            ),
        )
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.task),
        )
        .order_by(MergeCandidate.created_at.asc(), MergeCandidate.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def _load_older_open_candidate_pool(
    session: AsyncSession,
    candidates: list[MergeCandidate],
) -> list[MergeCandidate]:
    latest_candidate_by_repo_base: dict[tuple[str, str], MergeCandidate] = {}
    for candidate in candidates:
        key = (candidate.repo_url, candidate.base_branch)
        latest_candidate = latest_candidate_by_repo_base.get(key)
        if latest_candidate is None or _is_older_candidate(latest_candidate, candidate):
            latest_candidate_by_repo_base[key] = candidate

    repo_base_conditions = [
        and_(
            MergeCandidate.repo_url == repo_url,
            MergeCandidate.base_branch == base_branch,
            or_(
                MergeCandidate.created_at < candidate.created_at,
                and_(
                    MergeCandidate.created_at == candidate.created_at,
                    MergeCandidate.id < candidate.id,
                ),
            ),
        )
        for (repo_url, base_branch), candidate in sorted(latest_candidate_by_repo_base.items())
    ]
    if not repo_base_conditions:
        return []

    stmt = (
        select(MergeCandidate)
        .join(TaskAttempt, MergeCandidate.attempt_id == TaskAttempt.id)
        .where(
            MergeCandidate.status == "open",
            TaskAttempt.is_canonical_for_merge.is_(True),
            or_(*repo_base_conditions),
        )
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.task),
        )
        .order_by(
            MergeCandidate.repo_url.asc(),
            MergeCandidate.base_branch.asc(),
            MergeCandidate.created_at.asc(),
            MergeCandidate.id.asc(),
        )
    )
    return list((await session.execute(stmt)).scalars())


def _is_older_candidate(
    candidate: MergeCandidate,
    target: MergeCandidate,
) -> bool:
    return candidate.created_at < target.created_at or (
        candidate.created_at == target.created_at and candidate.id < target.id
    )


def _blocking_state(candidate: MergeCandidate) -> str | None:
    if _is_merge_ready_candidate(candidate):
        return "merge_eligible"
    if _is_monitor_owned_recovery(candidate):
        return "monitor_owned_recovery"
    return None


def _candidate_blocks_target(
    candidate: MergeCandidate,
    target: MergeCandidate,
) -> bool:
    candidate_paths = _candidate_owned_paths(candidate)
    target_paths = _candidate_owned_paths(target)
    if not candidate_paths or not target_paths:
        return False
    return any(
        owned_paths_overlap(candidate_path, target_path)
        for candidate_path in candidate_paths
        for target_path in target_paths
    )


def _candidate_owned_paths(candidate: MergeCandidate) -> tuple[str, ...]:
    paths = tuple(path for path in candidate.workspace.owned_paths if path)
    if paths:
        return interworkspace_owned_paths(paths)
    return interworkspace_owned_paths(path for path in candidate.attempt.owned_paths if path)


def _is_merge_ready_candidate(candidate: MergeCandidate) -> bool:
    return (
        candidate.status == "open"
        and candidate.attempt.is_canonical_for_merge
        and candidate.workspace.auto_merge
        and _workspace_status(candidate.workspace) == WorkspaceStatus.monitoring_pr
        and not candidate.stale
    )


def _is_monitor_owned_recovery(candidate: MergeCandidate) -> bool:
    workspace_status = _workspace_status(candidate.workspace)
    return (
        candidate.status == "open"
        and candidate.attempt.is_canonical_for_merge
        and candidate.workspace.auto_merge
        and workspace_status in _MONITOR_RECOVERY_STATUSES
        and any(_is_monitor_recovery_operation(op) for op in candidate.workspace.operations)
    )


def _is_monitor_recovery_operation(operation: Operation) -> bool:
    if operation.type not in _RECOVERY_OPERATION_TYPES:
        return False
    if operation.status not in _ACTIVE_OPERATION_STATUSES:
        return False
    payload = operation.payload
    if not isinstance(payload, dict):
        return False
    return payload.get("source") == "pr_monitor"


def _workspace_status(workspace: Workspace) -> WorkspaceStatus | None:
    try:
        return WorkspaceStatus(workspace.status)
    except ValueError:
        return None


def _blocker_from_candidate(
    candidate: MergeCandidate,
    *,
    blocker_state: str,
) -> MergeQueueBlocker:
    return MergeQueueBlocker(
        candidate_id=candidate.id,
        workspace_id=candidate.workspace_id,
        attempt_id=candidate.attempt_id,
        task_id=candidate.task_id,
        title=candidate.workspace.task_title,
        pr_url=candidate.pr_url,
        pr_number=candidate.pr_number,
        status=candidate.workspace.status,
        blocker_state=blocker_state,
    )


def _provider_recovery_state_response(
    workspace: Workspace,
) -> ProviderRecoveryStateResponse | None:
    view = provider_recovery_state_for_workspace(workspace)
    if view is None:
        return None
    fallback_target_response: FallbackTargetResponse | None = None
    if view.fallback_target is not None:
        fallback_target_response = FallbackTargetResponse(
            agent=view.fallback_target.agent,
            provider=view.fallback_target.provider,
            model=view.fallback_target.model,
        )
    return ProviderRecoveryStateResponse(
        action=view.action,
        reason_code=view.reason_code,
        source_provider=view.source_provider,
        source_model=view.source_model,
        retry_attempt_number=view.retry_attempt_number,
        fallback_attempt_number=view.fallback_attempt_number,
        cooldown_until=view.cooldown_until,
        next_eligible_at=view.next_eligible_at,
        fallback_target=fallback_target_response,
        source_workspace_id=view.source_workspace_id,
        source_attempt_id=view.source_attempt_id,
        recommended_action=view.recommended_action,
        terminal=view.terminal,
    )
