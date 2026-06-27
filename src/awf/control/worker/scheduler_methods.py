"""Extracted ControlWorker domain operations.

This module contains mechanically moved methods from ``awf.control.worker.manager`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import contextlib as contextlib
import hashlib as hashlib
import json as json
import re as re
import subprocess as subprocess
import uuid as uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.workspace_policy import (
    agent_model_from_task_policy,
)
from awf.control.worker.constants import (
    _SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL,
    PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
    PROVIDER_RECOVERY_NOT_BEFORE_REASON,
    QUEUE_DECISION_DEFERRED,
)
from awf.control.worker.helpers import (
    _earliest_future_datetime,
    _json_datetime,
    _scheduler_items_are_workspace_ids,
    _scheduler_items_are_workspaces,
)
from awf.control.worker.logging import _log
from awf.control.worker.scheduling import (
    _existing_ordered_queue_decision_keys,
    _order_scheduler_workspaces,
    _ordered_queue_decision_create,
    _ordered_queue_decision_key,
    _ordered_queue_decision_matches,
    _OrderedDecisionCandidate,
    _record_scheduler_queue_decision,
    _scheduler_candidate_cursor,
    _scheduler_candidate_fetch_limit,
)
from awf.control.worker.types import _SchedulerCandidateFilterResult
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    TaskAttempt,
    Workspace,
)
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    QueueDecisionRepository,
    WorkspaceRepository,
)
from awf.db.resilience import run_db_operation_with_retry
from awf.service.provider_recovery import (
    provider_cooldown_not_before,
    provider_for_agent_model,
)
from awf.service.scheduler import SchedulerOrderCursor


async def _list_scheduler_dispatchable_ids(
    self: Any,
    *,
    status: WorkspaceStatus,
    limit: int,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
) -> list[str]:
    async def _operation(session: AsyncSession) -> list[str]:
        return cast(
            list[str],
            await self._list_scheduler_dispatchable_ids_from_pages(
                session,
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
                node_id=node_id,
            ),
        )

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=False,
        on_retry=self._log_transient_db_retry,
    )


async def _list_scheduler_dispatchable_ids_from_pages(
    self: Any,
    session: AsyncSession,
    *,
    status: WorkspaceStatus,
    limit: int,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
) -> list[str]:
    dispatchable_workspaces_by_id: dict[str, Workspace] = {}
    candidate_limit = _scheduler_candidate_fetch_limit(limit)
    candidate_after: SchedulerOrderCursor | None = None
    scoring_at = datetime.now(UTC)
    priority_refill_pages_remaining: int | None = None
    ordered_workspaces: list[Workspace] = []
    repo = WorkspaceRepository(session)
    while True:
        workspaces = await repo.list_schedulable_workspaces(
            status=status,
            limit=candidate_limit,
            exclude_ids=exclude_ids,
            node_id=node_id,
            after=candidate_after,
            scoring_at=scoring_at,
            execution_claim_owner_id=(
                self._worker_id if status == WorkspaceStatus.monitoring_pr else None
            ),
        )
        if not workspaces:
            break
        remaining_dispatch_slots = limit - len(dispatchable_workspaces_by_id)
        page_dispatchable_ids = await self._filter_scheduler_candidate_workspaces(
            session,
            workspaces,
            limit=remaining_dispatch_slots if remaining_dispatch_slots > 0 else limit,
            scoring_at=scoring_at,
        )
        workspaces_by_id = {workspace.id: workspace for workspace in workspaces}
        for workspace_id in page_dispatchable_ids:
            workspace = workspaces_by_id.get(workspace_id)
            if workspace is not None:
                dispatchable_workspaces_by_id.setdefault(workspace_id, workspace)
        ordered_workspaces = _order_scheduler_workspaces(
            list(dispatchable_workspaces_by_id.values()),
            now=scoring_at,
        )
        if len(workspaces) < candidate_limit:
            break
        if len(ordered_workspaces) >= limit:
            # Preserve cross-page priority refill without scanning the tail
            # of a large status queue after dispatch slots are filled.
            if priority_refill_pages_remaining is None:
                priority_refill_pages_remaining = _SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL
            if priority_refill_pages_remaining <= 0:
                break
            priority_refill_pages_remaining -= 1
        candidate_after = _scheduler_candidate_cursor(
            workspaces,
            scoring_at=scoring_at,
            dialect_name=repo.dialect_name,
        )
    return [workspace.id for workspace in ordered_workspaces[:limit]]


async def _filter_scheduler_candidate_workspaces(
    self: Any,
    session: AsyncSession,
    candidate_workspaces: list[Workspace],
    *,
    limit: int,
    scoring_at: datetime,
    provider_recovery_decision_candidate_limit: int | None = None,
) -> list[str]:
    result = await self._filter_scheduler_candidate_workspaces_with_result(
        session,
        candidate_workspaces,
        limit=limit,
        scoring_at=scoring_at,
        provider_recovery_decision_candidate_limit=(provider_recovery_decision_candidate_limit),
    )
    return cast(list[str], result.workspace_ids)


async def _filter_scheduler_candidate_workspaces_with_result(
    self: Any,
    session: AsyncSession,
    candidate_workspaces: list[Workspace],
    *,
    limit: int,
    scoring_at: datetime,
    provider_recovery_decision_candidate_limit: int | None = None,
) -> _SchedulerCandidateFilterResult:
    if not candidate_workspaces:
        return _SchedulerCandidateFilterResult(workspace_ids=[])

    if provider_recovery_decision_candidate_limit is None:
        filter_result = await self._filter_provider_recovery_suppressed_with_result(
            session,
            candidate_workspaces,
        )
    else:
        filter_result = await self._filter_provider_recovery_suppressed_with_result(
            session,
            candidate_workspaces,
            decision_candidate_limit=provider_recovery_decision_candidate_limit,
        )
    workspaces_by_id = {workspace.id: workspace for workspace in candidate_workspaces}
    eligible_workspaces_by_id: dict[str, Workspace] = {}
    for workspace_id in filter_result.workspace_ids:
        workspace = workspaces_by_id.get(workspace_id)
        if workspace is not None:
            eligible_workspaces_by_id.setdefault(workspace_id, workspace)
    ordered_workspaces = _order_scheduler_workspaces(
        list(eligible_workspaces_by_id.values()),
        now=scoring_at,
    )
    return _SchedulerCandidateFilterResult(
        workspace_ids=[workspace.id for workspace in ordered_workspaces[:limit]],
        provider_suppression_resume_expires_at=(
            filter_result.provider_suppression_resume_expires_at
        ),
    )


async def _filter_provider_recovery_suppressed(
    self: Any,
    session: AsyncSession,
    workspaces: list[Workspace] | list[str],
    *,
    decision_candidate_limit: int | None = None,
) -> list[str]:
    result = await self._filter_provider_recovery_suppressed_with_result(
        session,
        workspaces,
        decision_candidate_limit=decision_candidate_limit,
    )
    return cast(list[str], result.workspace_ids)


async def _filter_provider_recovery_suppressed_with_result(
    self: Any,
    session: AsyncSession,
    workspaces: list[Workspace] | list[str],
    *,
    decision_candidate_limit: int | None = None,
) -> _SchedulerCandidateFilterResult:
    _ = self
    if not workspaces:
        return _SchedulerCandidateFilterResult(workspace_ids=[])
    if _scheduler_items_are_workspace_ids(workspaces):
        workspace_ids = workspaces
        stmt = select(Workspace).where(Workspace.id.in_(workspace_ids))
        rows = {workspace.id: workspace for workspace in (await session.execute(stmt)).scalars()}
    elif _scheduler_items_are_workspaces(workspaces):
        workspace_rows = workspaces
        workspace_ids = [workspace.id for workspace in workspace_rows]
        rows = {workspace.id: workspace for workspace in workspace_rows}
    else:
        return _SchedulerCandidateFilterResult(workspace_ids=[])
    now = datetime.now(UTC)
    breaker_repo = ProviderModelCircuitBreakerRepository(session)
    allowed: set[str] = set()
    circuit_candidates: dict[str, tuple[str, str]] = {}
    circuit_decision_workspace_ids: set[str] = set()
    provider_suppression_resume_expires_at: datetime | None = None
    for index, workspace_id in enumerate(workspace_ids):
        workspace = rows.get(workspace_id)
        if workspace is None:
            continue
        record_suppression_decision = (
            decision_candidate_limit is None or index < decision_candidate_limit
        )
        not_before = provider_cooldown_not_before(workspace.task_policy)
        if not_before is not None and not_before > now:
            provider_suppression_resume_expires_at = _earliest_future_datetime(
                provider_suppression_resume_expires_at,
                not_before,
                now=now,
            )
            if record_suppression_decision:
                await _record_scheduler_queue_decision(
                    session,
                    workspace,
                    decision=QUEUE_DECISION_DEFERRED,
                    reason_code=PROVIDER_RECOVERY_NOT_BEFORE_REASON,
                    decided_at=now,
                    suppression_detail={"not_before": not_before.isoformat()},
                )
            continue
        model = agent_model_from_task_policy(workspace.task_policy)
        provider = provider_for_agent_model(workspace.agent, model)
        if provider is None or model is None:
            allowed.add(workspace_id)
            continue
        circuit_candidates[workspace_id] = (provider, model)
        if record_suppression_decision:
            circuit_decision_workspace_ids.add(workspace_id)

    open_breakers = await breaker_repo.open_breakers_for_pairs(
        pairs=circuit_candidates.values(),
        now=now,
    )
    for workspace_id, pair in circuit_candidates.items():
        if pair not in open_breakers:
            allowed.add(workspace_id)
            continue
        workspace = rows.get(workspace_id)
        if workspace is None:
            continue
        breaker = open_breakers[pair]
        if workspace_id in circuit_decision_workspace_ids:
            await _record_scheduler_queue_decision(
                session,
                workspace,
                decision=QUEUE_DECISION_DEFERRED,
                reason_code=PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
                decided_at=now,
                suppression_detail={
                    "provider": breaker.provider,
                    "model": breaker.model,
                    "cooldown_until": _json_datetime(breaker.cooldown_until),
                },
            )
        provider_suppression_resume_expires_at = _earliest_future_datetime(
            provider_suppression_resume_expires_at,
            breaker.cooldown_until,
            now=now,
        )
    return _SchedulerCandidateFilterResult(
        workspace_ids=[workspace_id for workspace_id in workspace_ids if workspace_id in allowed],
        provider_suppression_resume_expires_at=provider_suppression_resume_expires_at,
    )


async def _record_ordered_decisions(
    self: Any,
    workspace_ids: list[str],
    *,
    reason_code: str,
) -> None:
    if not workspace_ids:
        return
    decided_at = datetime.now(UTC)

    async def _operation(session: AsyncSession) -> None:
        stmt = (
            select(
                Workspace.id,
                Workspace.task_class,
                Workspace.task_policy,
                Workspace.created_at,
                TaskAttempt.task_id,
                TaskAttempt.id,
            )
            .join(
                TaskAttempt,
                TaskAttempt.workspace_id == Workspace.id,
            )
            .where(Workspace.id.in_(workspace_ids))
        )
        rows = (await session.execute(stmt)).all()
        candidates_by_id = {
            row[0]: _OrderedDecisionCandidate(
                workspace_id=row[0],
                task_class=row[1],
                task_policy=row[2],
                created_at=row[3],
                task_id=row[4],
                attempt_id=row[5],
            )
            for row in rows
        }
        candidates = [
            candidates_by_id[workspace_id]
            for workspace_id in workspace_ids
            if workspace_id in candidates_by_id
        ]
        if not candidates:
            return

        queue_repo = QueueDecisionRepository(session)
        latest_by_workspace_id = await queue_repo.latest_by_workspace_ids(
            candidate.workspace_id for candidate in candidates
        )
        existing_ordered_decision_keys = await _existing_ordered_queue_decision_keys(
            session,
            candidates,
            reason_code=reason_code,
            decided_at=decided_at,
        )
        decision_rows = [
            _ordered_queue_decision_create(
                candidate,
                latest=latest_by_workspace_id.get(candidate.workspace_id),
                reason_code=reason_code,
                decided_at=decided_at,
            )
            for candidate in candidates
            if not _ordered_queue_decision_matches(
                latest_by_workspace_id.get(candidate.workspace_id),
                candidate,
                reason_code=reason_code,
                decided_at=decided_at,
            )
            and _ordered_queue_decision_key(candidate) not in existing_ordered_decision_keys
        ]
        await queue_repo.create_many(decision_rows)

    await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=True,
        on_retry=self._log_transient_db_retry,
    )


async def _load_workspace_statuses(self: Any, workspace_ids: list[str]) -> dict[str, str]:
    """Bulk-read current ``status`` for the given workspace IDs.

    Callers (``_filter_current_status`` and reconciliation) guard against an
    empty list, so this is only invoked with at least one ID.
    """

    async def _operation(session: AsyncSession) -> dict[str, str]:
        stmt = select(Workspace.id, Workspace.status).where(Workspace.id.in_(workspace_ids))
        result = await session.execute(stmt)
        return {row[0]: row[1] for row in result.all()}

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        on_retry=self._log_transient_db_retry,
    )


async def _filter_current_status(
    self: Any,
    workspace_ids: list[str],
    *,
    expected: WorkspaceStatus,
    action: str,
) -> list[str]:
    if not workspace_ids:
        return []

    statuses = await self._load_workspace_statuses(workspace_ids)

    current_ids: list[str] = []
    for workspace_id in workspace_ids:
        actual = statuses.get(workspace_id)
        if actual == expected.value:
            current_ids.append(workspace_id)
            continue

        _log.info(
            "worker.skip_stale_dispatch",
            workspace_id=workspace_id,
            action=action,
            expected_status=expected.value,
            status=actual,
        )
    return current_ids
