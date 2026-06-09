"""Task starvation prevention scheduling and starvation ordering."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.workspace_policy import agent_model_from_task_policy
from awf.db.enums import WorkspaceStatus
from awf.db.models import QueueDecision, TaskAttempt, Workspace
from awf.db.repositories import (
    SCHEDULER_SQL_AGE_BOOST_DIALECTS,
    ProviderModelCircuitBreakerRepository,
    QueueDecisionRepository,
    TaskAttemptRepository,
)
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_STATE_KEY,
    provider_cooldown_not_before,
    provider_for_agent_model,
)
from awf.service.scheduler import (
    SchedulerOrderCursor,
    scheduler_order_key,
    scheduler_score_from_workspace,
    score_summary_with_suppression,
)

if TYPE_CHECKING:
    from awf.control.worker.types import _ActiveExecutionCandidate
    from awf.db.repositories import QueueDecisionCreate

QUEUE_DECISION_ORDERED = "ordered"
ORDERED_REQUESTED_PROVISIONING_REASON = "ORDERED_REQUESTED_PROVISIONING"
LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON = "LOCAL_CAPACITY_RESERVATION_DEFAULTED"
_ACTIVE_EXECUTION_STATUSES: frozenset[WorkspaceStatus] = frozenset(
    [
        WorkspaceStatus.provisioning,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
    ]
)


@dataclass(frozen=True)
class _CapacityQueueDecisionContext:
    attempt: TaskAttempt | None
    latest_decision: QueueDecision | None


@dataclass(frozen=True)
class _OrderedDecisionCandidate:
    workspace_id: str
    task_class: str | None
    task_policy: dict[str, Any] | None
    created_at: datetime
    task_id: str
    attempt_id: str

    @property
    def id(self) -> str:
        return self.workspace_id


type _OrderedDecisionKey = tuple[str, str, str]


def _order_scheduler_workspaces(
    workspaces: list[Workspace],
    *,
    now: datetime | None = None,
) -> list[Workspace]:
    scoring_at = now or datetime.now(UTC)
    scored = sorted(
        (
            (scheduler_score_from_workspace(workspace, now=scoring_at), workspace)
            for workspace in workspaces
        ),
        key=lambda item: scheduler_order_key(item[0]),
    )
    return [workspace for _score, workspace in scored]


def _scheduler_candidate_fetch_limit(limit: int) -> int:
    """Return the candidate batch size for eligibility refill scans."""
    if limit <= 0:
        return 0
    proportional_fetch_limit = limit * 4
    fixed_cushion_fetch_limit = limit + 16
    widened_fetch_limit = min(
        250,
        fixed_cushion_fetch_limit,
        proportional_fetch_limit,
    )
    return max(limit, widened_fetch_limit)


def _scheduler_candidate_cursor(
    workspaces: list[Workspace],
    *,
    scoring_at: datetime,
    dialect_name: str | None = None,
) -> SchedulerOrderCursor | None:
    if not workspaces:
        return None
    last = workspaces[-1]
    score = scheduler_score_from_workspace(last, now=scoring_at)
    effective_score = score.effective_score
    if dialect_name not in SCHEDULER_SQL_AGE_BOOST_DIALECTS:
        effective_score -= score.age_boost
    return SchedulerOrderCursor(
        class_priority=score.class_priority,
        effective_score=effective_score,
        queued_at=score.queued_at,
        workspace_id=last.id,
        scoring_at=scoring_at,
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stale_execution_claim_filter(claim_cutoff: datetime) -> Any:
    return or_(
        Workspace.execution_claimed_by.is_(None),
        Workspace.execution_claim_expires_at.is_(None),
        Workspace.execution_claim_expires_at <= claim_cutoff,
    )


def _stale_monitor_claim_filter(claim_cutoff: datetime) -> Any:
    return or_(
        Workspace.monitor_claimed_by.is_(None),
        Workspace.monitor_claim_expires_at.is_(None),
        Workspace.monitor_claim_expires_at <= claim_cutoff,
    )


def _claim_recheck_conditions(
    status: WorkspaceStatus,
    claim_cutoff: datetime,
) -> tuple[Any, ...]:
    if status in _ACTIVE_EXECUTION_STATUSES:
        return (_stale_execution_claim_filter(claim_cutoff),)
    if status == WorkspaceStatus.monitoring_pr:
        return (_stale_monitor_claim_filter(claim_cutoff),)
    return ()


def _ordered_queue_decision_key(
    candidate: _OrderedDecisionCandidate,
) -> _OrderedDecisionKey:
    return (candidate.workspace_id, candidate.task_id, candidate.attempt_id)


def _ordered_queue_decision_create(
    candidate: _OrderedDecisionCandidate,
    *,
    latest: QueueDecision | None,
    reason_code: str,
    decided_at: datetime,
) -> QueueDecisionCreate:
    from awf.db.repositories import QueueDecisionCreate

    score = scheduler_score_from_workspace(candidate, now=decided_at)
    return QueueDecisionCreate(
        workspace_id=candidate.workspace_id,
        task_id=candidate.task_id,
        attempt_id=candidate.attempt_id,
        decision=QUEUE_DECISION_ORDERED,
        reason_code=reason_code,
        class_priority=score.class_priority,
        computed_priority=score.effective_score,
        age_boost=score.age_boost,
        retry_bonus=score.retry_bonus,
        resource_summary=dict(latest.resource_summary) if latest else {},
        overlap_risk_summary=dict(latest.overlap_risk_summary) if latest else {},
        score_summary=score.score_summary,
        decided_at=decided_at,
    )


def _ordered_queue_decision_matches(
    decision: QueueDecision | None,
    candidate: _OrderedDecisionCandidate,
    *,
    reason_code: str,
    decided_at: datetime,
) -> bool:
    if decision is None:
        return False
    if not (
        decision.workspace_id == candidate.workspace_id
        and decision.task_id == candidate.task_id
        and decision.attempt_id == candidate.attempt_id
        and decision.decision == QUEUE_DECISION_ORDERED
    ):
        return False
    if decision.reason_code == reason_code:
        return _utc_datetime(decision.decided_at) == _utc_datetime(decided_at)
    return (
        reason_code == ORDERED_REQUESTED_PROVISIONING_REASON
        and decision.reason_code == LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON
    )


async def _existing_ordered_queue_decision_keys(
    session: Any,
    candidates: list[_OrderedDecisionCandidate],
    *,
    reason_code: str,
    decided_at: datetime,
) -> set[_OrderedDecisionKey]:
    candidate_keys = {_ordered_queue_decision_key(candidate) for candidate in candidates}
    if not candidate_keys:
        return set()

    stmt = select(
        QueueDecision.workspace_id,
        QueueDecision.task_id,
        QueueDecision.attempt_id,
    ).where(
        QueueDecision.workspace_id.in_(
            sorted({candidate.workspace_id for candidate in candidates})
        ),
        QueueDecision.decision == QUEUE_DECISION_ORDERED,
        QueueDecision.reason_code == reason_code,
        QueueDecision.decided_at == decided_at,
    )
    rows = (await session.execute(stmt)).all()
    return {
        (workspace_id, task_id, attempt_id)
        for workspace_id, task_id, attempt_id in rows
        if (workspace_id, task_id, attempt_id) in candidate_keys
    }


async def _record_scheduler_queue_decision(
    session: Any,
    workspace: Workspace,
    *,
    decision: str,
    reason_code: str,
    decided_at: datetime,
    suppression_detail: dict[str, Any] | None = None,
) -> None:
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    if attempt is None:
        return

    queue_repo = QueueDecisionRepository(session)
    latest = await queue_repo.list_for_workspace(workspace.id, limit=1)
    score = scheduler_score_from_workspace(workspace, now=decided_at)
    score_summary = (
        score_summary_with_suppression(
            score,
            reason_code=reason_code,
            detail=suppression_detail,
        )
        if suppression_detail is not None
        else score.score_summary
    )
    await queue_repo.create(
        workspace_id=workspace.id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        decision=decision,
        reason_code=reason_code,
        class_priority=score.class_priority,
        computed_priority=score.effective_score,
        age_boost=score.age_boost,
        retry_bonus=score.retry_bonus,
        resource_summary=dict(latest[0].resource_summary) if latest else {},
        overlap_risk_summary=dict(latest[0].overlap_risk_summary) if latest else {},
        score_summary=score_summary,
        decided_at=decided_at,
    )


async def _monitor_provider_recovery_resume_pending(
    session_factory: async_sessionmaker[AsyncSession],
    candidate: _ActiveExecutionCandidate,
) -> bool:
    if candidate.status != WorkspaceStatus.monitoring_pr:
        return False
    task_policy = candidate.task_policy if isinstance(candidate.task_policy, Mapping) else {}
    raw_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
    if not isinstance(raw_state, Mapping):
        return False
    action = raw_state.get("action")
    if action == "fallback":
        return True
    if action != "retry":
        return False

    not_before = provider_cooldown_not_before(candidate.task_policy)
    now = datetime.now(UTC)
    if not_before is not None and not_before > now:
        return True

    if candidate.agent is None:
        return False

    model = agent_model_from_task_policy(candidate.task_policy)
    provider = provider_for_agent_model(candidate.agent, model)
    if provider is None or model is None:
        return False

    async with session_factory() as session:
        breaker = await ProviderModelCircuitBreakerRepository(session).open_breaker(
            provider=provider,
            model=model,
            now=now,
        )
        return breaker is not None
