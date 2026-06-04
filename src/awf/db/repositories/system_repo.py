"""System and operational database repositories."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_value
from awf.common.ids import (
    new_egress_audit_record_id,
    new_provider_model_circuit_breaker_id,
    new_queue_decision_id,
)
from awf.db.models import (
    EgressAuditRecord,
    ProviderModelCircuitBreaker,
    QueueDecision,
    WorkerHeartbeat,
    Workspace,
)
from awf.db.repositories.base import (
    ACTIVE_RESOURCE_RESERVATION_EXCLUDED_STATUSES,
    QueueDecisionCreate,
    _circuit_breaker_expired,
    _provider_model_circuit_breaker_insert_if_absent_stmt,
    _worker_heartbeat_upsert_stmt,
    resolve_session_dialect_name,
)


class EgressAuditRepository:
    """CRUD helpers for outbound egress audit evidence."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def create(
        self,
        *,
        workspace_id: str,
        attempt_id: str | None,
        policy_posture: str,
        decision: str,
        destination_category: str,
        reason_code: str,
        details: dict[str, Any],
    ) -> EgressAuditRecord:
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            policy_posture=policy_posture,
            decision=decision,
            destination_category=destination_category,
            reason_code=reason_code,
            details=redact_audit_value(details),
        )
        self._session.add(record)
        return record

    async def get_latest_for_workspace(self, workspace_id: str) -> EgressAuditRecord | None:
        stmt = (
            select(EgressAuditRecord)
            .where(EgressAuditRecord.workspace_id == workspace_id)
            .order_by(EgressAuditRecord.enforced_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[EgressAuditRecord]:
        stmt = select(EgressAuditRecord).order_by(
            EgressAuditRecord.enforced_at.desc(),
            EgressAuditRecord.created_at.desc(),
            EgressAuditRecord.id.desc(),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars())

    async def summary_counts_by_posture(self) -> dict[str, int]:
        latest_ranked = (
            select(
                EgressAuditRecord.workspace_id,
                EgressAuditRecord.policy_posture,
                func.row_number()
                .over(
                    partition_by=EgressAuditRecord.workspace_id,
                    order_by=EgressAuditRecord.enforced_at.desc(),
                )
                .label("rn"),
            )
            .join(Workspace, EgressAuditRecord.workspace_id == Workspace.id)
            .where(~Workspace.status.in_(ACTIVE_RESOURCE_RESERVATION_EXCLUDED_STATUSES))
            .subquery("latest_ranked")
        )
        stmt = (
            select(
                latest_ranked.c.policy_posture,
                func.count(),
            )
            .select_from(latest_ranked)
            .where(latest_ranked.c.rn == 1)
            .group_by(latest_ranked.c.policy_posture)
        )
        result = await self._session.execute(stmt)
        return {str(row[0]): row[1] for row in result.fetchall()}


class WorkerHeartbeatRepository:
    """CRUD helpers for control-worker heartbeat liveness records."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def get(self, *, worker_id: str) -> WorkerHeartbeat | None:
        stmt = select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_heartbeat(
        self,
        *,
        worker_id: str,
        node_id: str,
        started_at: datetime,
        last_heartbeat_at: datetime,
        poll_interval_seconds: float,
    ) -> WorkerHeartbeat:
        now = datetime.now(UTC)
        heartbeat_values: dict[str, Any] = {
            "worker_id": worker_id,
            "node_id": node_id,
            "started_at": started_at,
            "last_heartbeat_at": last_heartbeat_at,
            "poll_interval_seconds": poll_interval_seconds,
        }
        upsert_stmt = _worker_heartbeat_upsert_stmt(self._dialect_name)
        if upsert_stmt is not None:
            result = await self._session.execute(
                upsert_stmt.values(
                    **heartbeat_values,
                    created_at=now,
                    updated_at=now,
                ).execution_options(populate_existing=True)
            )
            await self._session.flush()
            return cast(WorkerHeartbeat, result.scalar_one())

        heartbeat = await self.get(worker_id=worker_id)
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(
                **heartbeat_values,
            )
            self._session.add(heartbeat)
        else:
            heartbeat.node_id = node_id
            heartbeat.last_heartbeat_at = last_heartbeat_at
            heartbeat.poll_interval_seconds = poll_interval_seconds
        await self._session.flush()
        return heartbeat

    async def latest_for_node(self, *, node_id: str) -> WorkerHeartbeat | None:
        stmt = (
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.node_id == node_id)
            .order_by(WorkerHeartbeat.last_heartbeat_at.desc(), WorkerHeartbeat.worker_id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def prune_stale(self, *, before: datetime, limit: int) -> int:
        if limit <= 0:
            return 0
        stale_worker_ids = (
            select(WorkerHeartbeat.worker_id)
            .where(WorkerHeartbeat.last_heartbeat_at < before)
            .order_by(WorkerHeartbeat.last_heartbeat_at.asc(), WorkerHeartbeat.worker_id.asc())
            .limit(limit)
            .subquery()
        )
        stmt = delete(WorkerHeartbeat).where(
            WorkerHeartbeat.worker_id.in_(select(stale_worker_ids.c.worker_id))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        rowcount = getattr(result, "rowcount", 0)
        if rowcount is None or rowcount < 0:
            return 0
        return int(rowcount)


class ProviderModelCircuitBreakerRepository:
    """CRUD helpers for provider/model circuit breaker cooldown state."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def get(
        self,
        *,
        provider: str,
        model: str,
    ) -> ProviderModelCircuitBreaker | None:
        stmt = select(ProviderModelCircuitBreaker).where(
            ProviderModelCircuitBreaker.provider == provider,
            ProviderModelCircuitBreaker.model == model,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_failure(
        self,
        *,
        provider: str,
        model: str,
        reason_code: str,
        failure_fingerprint: str,
        workspace_id: str | None,
        attempt_id: str | None,
        now: datetime,
        failure_threshold: int,
        cooldown_seconds: int,
    ) -> ProviderModelCircuitBreaker:
        normalized_provider = provider.strip()
        normalized_model = model.strip()
        breaker = await self.get(provider=normalized_provider, model=normalized_model)
        if breaker is None:
            breaker = await self._create_or_get_missing(
                provider=normalized_provider,
                model=normalized_model,
            )

        if _circuit_breaker_expired(breaker, now):
            breaker.state = "closed"
            breaker.failure_count = 0
            breaker.opened_at = None
            breaker.cooldown_until = None

        breaker.failure_count += 1
        breaker.last_reason_code = reason_code
        breaker.last_failure_fingerprint = failure_fingerprint[:512]
        breaker.last_workspace_id = workspace_id
        breaker.last_attempt_id = attempt_id
        if breaker.failure_count >= max(1, failure_threshold):
            breaker.state = "open"
            breaker.opened_at = now
            breaker.cooldown_until = now + timedelta(seconds=max(0, cooldown_seconds))
        await self._session.flush()
        return breaker

    async def _create_or_get_missing(
        self,
        *,
        provider: str,
        model: str,
    ) -> ProviderModelCircuitBreaker:
        stmt = _provider_model_circuit_breaker_insert_if_absent_stmt(self._dialect_name)
        if stmt is not None:
            await self._session.execute(
                stmt.values(
                    id=new_provider_model_circuit_breaker_id(),
                    provider=provider,
                    model=model,
                    state="closed",
                    failure_count=0,
                )
            )
            breaker = await self.get(provider=provider, model=model)
            if breaker is None:
                raise RuntimeError("provider/model circuit breaker insert did not return a row")
            return breaker

        breaker = ProviderModelCircuitBreaker(
            id=new_provider_model_circuit_breaker_id(),
            provider=provider,
            model=model,
            state="closed",
            failure_count=0,
        )
        self._session.add(breaker)
        return breaker

    async def is_suppressed(
        self,
        *,
        provider: str,
        model: str,
        now: datetime,
    ) -> bool:
        breaker = await self.get(provider=provider, model=model)
        if breaker is None:
            return False
        if _circuit_breaker_expired(breaker, now):
            breaker.state = "closed"
            breaker.failure_count = 0
            breaker.opened_at = None
            breaker.cooldown_until = None
            await self._session.flush()
            return False
        return breaker.state == "open"

    async def open_breaker(
        self,
        *,
        provider: str,
        model: str,
        now: datetime,
    ) -> ProviderModelCircuitBreaker | None:
        breaker = await self.get(provider=provider, model=model)
        if breaker is None:
            return None
        if _circuit_breaker_expired(breaker, now):
            breaker.state = "closed"
            breaker.failure_count = 0
            breaker.opened_at = None
            breaker.cooldown_until = None
            await self._session.flush()
            return None
        return breaker if breaker.state == "open" else None

    async def open_breakers_for_pairs(
        self,
        *,
        pairs: Iterable[tuple[str, str]],
        now: datetime,
    ) -> dict[tuple[str, str], ProviderModelCircuitBreaker]:
        normalized_pairs = {
            (provider.strip(), model.strip())
            for provider, model in pairs
            if provider.strip() and model.strip()
        }
        if not normalized_pairs:
            return {}

        pair_filter = or_(
            *(
                and_(
                    ProviderModelCircuitBreaker.provider == provider,
                    ProviderModelCircuitBreaker.model == model,
                )
                for provider, model in sorted(normalized_pairs)
            )
        )
        stmt = select(ProviderModelCircuitBreaker).where(
            ProviderModelCircuitBreaker.state == "open",
            pair_filter,
        )
        breakers = list((await self._session.execute(stmt)).scalars())
        open_breakers: dict[tuple[str, str], ProviderModelCircuitBreaker] = {}
        for breaker in breakers:
            if _circuit_breaker_expired(breaker, now):
                breaker.state = "closed"
                breaker.failure_count = 0
                breaker.opened_at = None
                breaker.cooldown_until = None
                continue
            open_breakers[(breaker.provider, breaker.model)] = breaker
        await self._session.flush()
        return open_breakers

    async def list_open(self, *, now: datetime) -> builtins.list[ProviderModelCircuitBreaker]:
        stmt = select(ProviderModelCircuitBreaker).where(
            ProviderModelCircuitBreaker.state == "open"
        )
        breakers = list((await self._session.execute(stmt)).scalars())
        open_breakers: list[ProviderModelCircuitBreaker] = []
        for breaker in breakers:
            if _circuit_breaker_expired(breaker, now):
                breaker.state = "closed"
                breaker.failure_count = 0
                breaker.opened_at = None
                breaker.cooldown_until = None
            else:
                open_breakers.append(breaker)
        await self._session.flush()
        return open_breakers


class QueueDecisionRepository:
    """CRUD helpers for durable scheduler admission decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: str,
        task_id: str,
        attempt_id: str,
        decision: str,
        reason_code: str,
        class_priority: int,
        computed_priority: int,
        age_boost: int,
        retry_bonus: int,
        resource_summary: dict[str, Any],
        overlap_risk_summary: dict[str, Any],
        score_summary: dict[str, Any] | None = None,
        decided_at: datetime | None = None,
    ) -> QueueDecision:
        rows = await self.create_many(
            [
                QueueDecisionCreate(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    decision=decision,
                    reason_code=reason_code,
                    class_priority=class_priority,
                    computed_priority=computed_priority,
                    age_boost=age_boost,
                    retry_bonus=retry_bonus,
                    resource_summary=resource_summary,
                    overlap_risk_summary=overlap_risk_summary,
                    score_summary=score_summary,
                    decided_at=decided_at,
                )
            ]
        )
        return rows[0]

    async def create_many(
        self,
        records: Iterable[QueueDecisionCreate],
    ) -> builtins.list[QueueDecision]:
        rows = [
            QueueDecision(
                id=new_queue_decision_id(),
                workspace_id=record.workspace_id,
                task_id=record.task_id,
                attempt_id=record.attempt_id,
                decision=record.decision,
                reason_code=record.reason_code,
                class_priority=record.class_priority,
                computed_priority=record.computed_priority,
                age_boost=record.age_boost,
                retry_bonus=record.retry_bonus,
                resource_summary=dict(record.resource_summary),
                overlap_risk_summary=dict(record.overlap_risk_summary),
                score_summary=dict(record.score_summary or {}),
                decided_at=record.decided_at or datetime.now(UTC),
            )
            for record in records
        ]
        if not rows:
            return []
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def latest_by_workspace_ids(
        self,
        workspace_ids: Iterable[str],
    ) -> dict[str, QueueDecision]:
        unique_workspace_ids = tuple(dict.fromkeys(workspace_ids))
        if not unique_workspace_ids:
            return {}

        ranked_decisions = (
            select(
                QueueDecision.id.label("queue_decision_id"),
                func.row_number()
                .over(
                    partition_by=QueueDecision.workspace_id,
                    order_by=(
                        QueueDecision.decided_at.desc(),
                        QueueDecision.id.desc(),
                    ),
                )
                .label("decision_rank"),
            )
            .where(QueueDecision.workspace_id.in_(unique_workspace_ids))
            .subquery()
        )
        stmt = (
            select(QueueDecision)
            .join(ranked_decisions, QueueDecision.id == ranked_decisions.c.queue_decision_id)
            .where(ranked_decisions.c.decision_rank == 1)
        )
        return {
            decision.workspace_id: decision
            for decision in (await self._session.execute(stmt)).scalars()
        }

    async def list_for_workspace(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[QueueDecision]:
        stmt = (
            select(QueueDecision)
            .where(QueueDecision.workspace_id == workspace_id)
            .order_by(QueueDecision.decided_at.desc(), QueueDecision.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_for_attempt(
        self,
        attempt_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[QueueDecision]:
        stmt = (
            select(QueueDecision)
            .where(QueueDecision.attempt_id == attempt_id)
            .order_by(QueueDecision.decided_at.desc(), QueueDecision.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())
