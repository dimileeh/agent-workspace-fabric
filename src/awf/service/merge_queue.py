"""Merge-queue ordering policy helpers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, TaskAttempt, Workspace

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
            "blocked_candidate_id": self.candidate_id,
            "blocked_workspace_id": self.workspace_id,
            "blocked_pr_url": self.pr_url,
            "blocked_pr_number": self.pr_number,
            "blocked_title": self.title,
            "blocked_status": self.status,
            "blocked_state": self.blocker_state,
        }


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
        blocker_state = _blocking_state(row)
        if blocker_state is None:
            continue
        blockers.append(_blocker_from_candidate(row, blocker_state=blocker_state))
    return blockers


async def list_merge_queue_blockers_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: str,
) -> list[MergeQueueBlocker]:
    candidate = await _load_open_candidate_for_workspace(session, workspace_id)
    if candidate is None:
        return []
    return await list_merge_queue_blockers_for_candidate(session, candidate_id=candidate.id)


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


def _blocking_state(candidate: MergeCandidate) -> str | None:
    if _is_merge_ready_candidate(candidate):
        return "merge_eligible"
    if _is_monitor_owned_recovery(candidate):
        return "monitor_owned_recovery"
    return None


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
    return payload.get("source") == "pr_monitor" or isinstance(payload.get("reason"), str)


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
