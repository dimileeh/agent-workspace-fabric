"""Merge-queue ordering policy helpers."""

from __future__ import annotations

from collections.abc import Iterable
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
            "blocker_candidate_id": self.candidate_id,
            "blocker_workspace_id": self.workspace_id,
            "blocker_pr_url": self.pr_url,
            "blocker_pr_number": self.pr_number,
            "blocker_title": self.title,
            "blocker_status": self.status,
            "blocker_state": self.blocker_state,
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
            blocker_state = _blocking_state(blocker_candidate)
            if blocker_state is None:
                continue
            blockers.append(
                _blocker_from_candidate(blocker_candidate, blocker_state=blocker_state)
            )
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
        for (repo_url, base_branch), candidate in sorted(
            latest_candidate_by_repo_base.items()
        )
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
