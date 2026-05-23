"""Shared task-list and task-attempt list construction for REST and MCP."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    MergeCandidateReadinessResponse,
    TaskAttemptListResponse,
    TaskAttemptResponse,
    TaskListResponse,
    TaskResponse,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import MergeCandidate, TaskAttempt, Workspace
from awf.db.repositories import TaskAttemptRepository, TaskRepository, WorkspaceRepository
from awf.service.usage_store import read_latest_usage_snapshots
from awf.service.workspace_observability import (
    prefetched_usage_snapshots,
    workspace_identity_usage_payload,
    workspace_pricing_metadata,
)


def _readiness_from_candidate(
    candidate: MergeCandidate | None,
) -> MergeCandidateReadinessResponse | None:
    if candidate is None:
        return None
    return MergeCandidateReadinessResponse(
        ready=candidate.ready,
        manual_merge_required=candidate.manual_merge_required,
        waiting_for_monitor=candidate.waiting_for_monitor,
        failed_or_cancelled=candidate.failed_or_cancelled,
        completed=candidate.completed,
        not_canonical=candidate.not_canonical,
        stale=candidate.stale,
    )


def _pricing_from_workspace(workspace: Workspace) -> dict[str, object] | None:
    pricing = workspace_pricing_metadata(workspace)
    if pricing is None:
        return None
    return {
        "provider": pricing.provider,
        "model": pricing.model,
        "currency": pricing.currency,
        "unit": pricing.unit,
        "price_per_unit": pricing.price_per_unit,
        "timestamp": pricing.timestamp,
        "version": pricing.version,
        "is_current": pricing.is_current(),
    }


def _task_from_attempt(
    attempt: TaskAttempt,
    *,
    canonical_attempt_id: str | None,
) -> TaskResponse:
    workspace = attempt.workspace
    candidate = attempt.merge_candidate
    observability = workspace_identity_usage_payload(workspace)
    return TaskResponse(
        task_id=attempt.task.external_id or attempt.task.id,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        parent_attempt_id=attempt.parent_attempt_id,
        redispatch_from_attempt_id=attempt.redispatch_from_attempt_id,
        superseded_by_attempt_id=attempt.superseded_by_attempt_id,
        is_canonical_for_merge=attempt.is_canonical_for_merge,
        canonical_attempt_id=canonical_attempt_id,
        candidate_id=candidate.id if candidate is not None else None,
        candidate_status=candidate.status if candidate is not None else None,
        readiness=_readiness_from_candidate(candidate),
        workspace_id=attempt.workspace_id,
        title=attempt.title,
        repo_url=attempt.repo_url,
        base_branch=attempt.base_branch,
        task_class=attempt.task_class,
        owned_paths=list(attempt.owned_paths),
        agent=AgentRuntime(attempt.agent),
        agent_model=observability["agent_model"],
        agent_effort=observability["agent_effort"],
        agent_model_source=observability["agent_model_source"],
        agent_effort_source=observability["agent_effort_source"],
        llm_usage=observability["llm_usage"],
        pricing=_pricing_from_workspace(workspace),
        status=WorkspaceStatus(workspace.status),
        pr_url=workspace.pr_url,
        failure_reason=workspace.failure_reason,
        created_at=attempt.created_at,
        updated_at=workspace.updated_at,
    )


def _attempt_response(attempt: TaskAttempt) -> TaskAttemptResponse:
    workspace = attempt.workspace
    candidate = attempt.merge_candidate
    return TaskAttemptResponse(
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        workspace_id=attempt.workspace_id,
        attempt_number=attempt.attempt_number,
        parent_attempt_id=attempt.parent_attempt_id,
        redispatch_from_attempt_id=attempt.redispatch_from_attempt_id,
        superseded_by_attempt_id=attempt.superseded_by_attempt_id,
        is_canonical_for_merge=attempt.is_canonical_for_merge,
        candidate_id=candidate.id if candidate is not None else None,
        candidate_status=candidate.status if candidate is not None else None,
        readiness=_readiness_from_candidate(candidate),
        agent=AgentRuntime(attempt.agent),
        status=WorkspaceStatus(workspace.status),
        pr_url=workspace.pr_url,
        failure_reason=workspace.failure_reason,
        created_at=attempt.created_at,
        updated_at=workspace.updated_at,
    )


def _task_from_workspace(row: Workspace) -> TaskResponse:
    observability = workspace_identity_usage_payload(row)
    return TaskResponse(
        task_id=row.task_external_id or row.id,
        attempt_id=None,
        attempt_number=None,
        parent_attempt_id=None,
        redispatch_from_attempt_id=None,
        superseded_by_attempt_id=None,
        is_canonical_for_merge=None,
        canonical_attempt_id=None,
        candidate_id=None,
        candidate_status=None,
        readiness=None,
        workspace_id=row.id,
        title=row.task_title,
        repo_url=row.repo_url,
        base_branch=row.branch_base,
        task_class=row.task_class,
        owned_paths=list(row.owned_paths),
        agent=AgentRuntime(row.agent),
        agent_model=observability["agent_model"],
        agent_effort=observability["agent_effort"],
        agent_model_source=observability["agent_model_source"],
        agent_effort_source=observability["agent_effort_source"],
        llm_usage=observability["llm_usage"],
        pricing=_pricing_from_workspace(row),
        status=WorkspaceStatus(row.status),
        pr_url=row.pr_url,
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def build_task_list_response(
    session: AsyncSession,
    *,
    workspace_status: WorkspaceStatus | None = None,
    agent: AgentRuntime | None = None,
    repo_url: str | None = None,
    limit: int = 50,
) -> TaskListResponse:
    attempt_repo = TaskAttemptRepository(session)
    attempt_rows = await attempt_repo.list_latest(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    legacy_rows = await WorkspaceRepository(session).list_without_task_attempts(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    canonical_attempt_ids = await attempt_repo.list_canonical_ids_for_tasks(
        row.task_id for row in attempt_rows
    )
    workspace_ids = [row.workspace_id for row in attempt_rows]
    workspace_ids.extend(row.id for row in legacy_rows)
    snapshots = await asyncio.to_thread(read_latest_usage_snapshots, workspace_ids)
    items: list[TaskResponse] = []
    with prefetched_usage_snapshots(snapshots):
        for row in attempt_rows:
            items.append(
                _task_from_attempt(
                    row,
                    canonical_attempt_id=canonical_attempt_ids.get(row.task_id),
                )
            )
        items.extend(_task_from_workspace(row) for row in legacy_rows)
    items.sort(key=lambda item: (item.created_at, item.workspace_id), reverse=True)
    return TaskListResponse(
        items=items[:limit],
        limit=limit,
        cursor=None,
    )


async def build_task_attempt_list_response(
    session: AsyncSession,
    task_ref: str,
    *,
    limit: int = 100,
) -> TaskAttemptListResponse | None:
    task = await TaskRepository(session).get_by_ref(task_ref)
    if task is None:
        return None
    rows = await TaskAttemptRepository(session).list_for_task(task.id, limit=limit)
    return TaskAttemptListResponse(
        task_id=task.id,
        task_ref=task_ref,
        items=[_attempt_response(row) for row in rows],
        limit=limit,
        cursor=None,
    )
