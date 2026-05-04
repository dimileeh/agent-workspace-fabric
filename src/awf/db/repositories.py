"""Data access layer for control-plane entities.

Repositories encapsulate SQL queries so route handlers and workers don't sprinkle
SQLAlchemy calls everywhere. Rules:

- Repositories do NOT commit; callers manage transactions.
- Repositories route every ``status`` mutation through ``WorkspaceStateMachine``.
- Repositories are the only code that writes to ``workspace_events``.
"""

from __future__ import annotations

import builtins
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from awf.common.audit import build_audit_payload, redact_audit_value
from awf.common.callback_events import (
    CALLBACK_EVENT_WILDCARDS,
    PUBLIC_CALLBACK_EVENT_TYPES,
)
from awf.common.ids import (
    new_callback_delivery_id,
    new_callback_subscription_id,
    new_event_id,
    new_log_stream_id,
    new_merge_candidate_id,
    new_operation_id,
    new_policy_finding_id,
    new_provider_model_circuit_breaker_id,
    new_queue_decision_id,
    new_resource_reservation_id,
    new_secret_lease_id,
    new_stale_reason_id,
    new_task_attempt_id,
    new_task_id,
    new_validation_run_id,
    new_workspace_id,
)
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import (
    AgentRuntime,
    CallbackDeliveryStatus,
    CallbackEventKind,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    PolicyFinding,
    ProviderModelCircuitBreaker,
    QueueDecision,
    ResourceReservation,
    StaleReason,
    Task,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceLogStream,
    WorkspaceSecretLease,
)
from awf.runtime.merge_eligibility import DOCS_TASK_SCOPE_VIOLATION_STALE_REASON
from awf.service.scheduler import scheduler_order_key, scheduler_score_from_workspace

ACTIVE_OWNED_PATH_OVERLAP_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.requested.value,
    WorkspaceStatus.provisioning.value,
    WorkspaceStatus.ready.value,
    WorkspaceStatus.running.value,
    WorkspaceStatus.validating.value,
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
)
ACTIVE_OWNED_PATH_CONFLICT_STATUSES: Final[tuple[str, ...]] = (
    ACTIVE_OWNED_PATH_OVERLAP_STATUSES
)
ACTIVE_RESOURCE_RESERVATION_EXCLUDED_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.completed.value,
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.destroyed.value,
)
OWNED_PATH_EXACT_MATCH_REASON: Final = "OWNED_PATH_EXACT_MATCH"
OWNED_PATH_ANCESTOR_MATCH_REASON: Final = "OWNED_PATH_ANCESTOR_MATCH"
OWNED_PATH_WILDCARD_MATCH_REASON: Final = "OWNED_PATH_WILDCARD_MATCH"
_SECRET_LEASE_DECLARATION_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "workspace_id",
    "secret_name",
    "kind",
    "target",
)
_CALLBACK_SUBSCRIPTION_IDEMPOTENCY_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "idempotency_key",
)
_CALLBACK_DELIVERY_DEDUPE_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "subscription_id",
    "dedupe_key",
)
_PROVIDER_MODEL_CIRCUIT_BREAKER_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "provider",
    "model",
)


@dataclass(frozen=True)
class OwnedPathOverlap:
    workspace_id: str
    existing_path: str
    requested_path: str


@dataclass(frozen=True)
class OwnedPathConflict:
    workspace_id: str
    existing_path: str
    requested_path: str


@dataclass(frozen=True)
class OwnedPathOverlapMatch:
    left_path: str
    right_path: str
    normalized_left_path: str
    normalized_right_path: str
    match_reason_code: str
    explanation: str


@dataclass(frozen=True)
class WorkspaceEventCreate:
    event_type: str
    reason_code: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class _IssuedSecretLease:
    lease: WorkspaceSecretLease
    issue_event_required: bool


@dataclass(frozen=True)
class QueueDecisionCreate:
    workspace_id: str
    task_id: str
    attempt_id: str
    decision: str
    reason_code: str
    class_priority: int
    computed_priority: int
    age_boost: int
    retry_bonus: int
    resource_summary: dict[str, Any]
    overlap_risk_summary: dict[str, Any]
    score_summary: dict[str, Any] | None = None
    decided_at: datetime | None = None


def validation_command_set_hash(commands: list[dict[str, Any]]) -> str:
    """Stable hash for the configured command metadata in a validation run."""

    payload = json.dumps(
        [_validation_command_identity(command) for command in commands],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation_command_identity(command: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in command.items()
        if key not in {"evidence_status", "evidence_reason_code", "evidence_source_run_id"}
    }


def _resolve_session_dialect_name(
    session: AsyncSession,
    dialect_name: str | None,
) -> str | None:
    if dialect_name is not None:
        return dialect_name

    info_value = session.info.get(SESSION_DIALECT_NAME_KEY)
    if isinstance(info_value, str):
        return info_value

    bind = getattr(session, "bind", None)
    if bind is None:
        return None
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return name if isinstance(name, str) else None


def _secret_lease_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
    if dialect_name == "postgresql":
        return (
            postgresql_insert(WorkspaceSecretLease)
            .on_conflict_do_nothing(index_elements=_SECRET_LEASE_DECLARATION_CONFLICT_COLUMNS)
            .returning(WorkspaceSecretLease.id)
        )
    if dialect_name == "sqlite":
        return (
            sqlite_insert(WorkspaceSecretLease)
            .on_conflict_do_nothing(index_elements=_SECRET_LEASE_DECLARATION_CONFLICT_COLUMNS)
            .returning(WorkspaceSecretLease.id)
        )
    return None


def _callback_subscription_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
    if dialect_name == "postgresql":
        return (
            postgresql_insert(CallbackSubscription)
            .on_conflict_do_nothing(
                index_elements=_CALLBACK_SUBSCRIPTION_IDEMPOTENCY_CONFLICT_COLUMNS
            )
            .returning(CallbackSubscription.id)
        )
    if dialect_name == "sqlite":
        return (
            sqlite_insert(CallbackSubscription)
            .on_conflict_do_nothing(
                index_elements=_CALLBACK_SUBSCRIPTION_IDEMPOTENCY_CONFLICT_COLUMNS
            )
            .returning(CallbackSubscription.id)
        )
    return None


def _callback_delivery_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
    if dialect_name == "postgresql":
        return (
            postgresql_insert(CallbackDelivery)
            .on_conflict_do_nothing(index_elements=_CALLBACK_DELIVERY_DEDUPE_CONFLICT_COLUMNS)
            .returning(CallbackDelivery.id)
        )
    if dialect_name == "sqlite":
        return (
            sqlite_insert(CallbackDelivery)
            .on_conflict_do_nothing(index_elements=_CALLBACK_DELIVERY_DEDUPE_CONFLICT_COLUMNS)
            .returning(CallbackDelivery.id)
        )
    return None


def _provider_model_circuit_breaker_insert_if_absent_stmt(
    dialect_name: str | None,
) -> Any | None:
    if dialect_name == "postgresql":
        return (
            postgresql_insert(ProviderModelCircuitBreaker)
            .on_conflict_do_nothing(
                index_elements=_PROVIDER_MODEL_CIRCUIT_BREAKER_CONFLICT_COLUMNS
            )
            .returning(ProviderModelCircuitBreaker.id)
        )
    if dialect_name == "sqlite":
        return (
            sqlite_insert(ProviderModelCircuitBreaker)
            .on_conflict_do_nothing(
                index_elements=_PROVIDER_MODEL_CIRCUIT_BREAKER_CONFLICT_COLUMNS
            )
            .returning(ProviderModelCircuitBreaker.id)
        )
    return None


def _callback_subscription_event_type_candidates(event_type: str) -> tuple[str, ...]:
    if event_type not in PUBLIC_CALLBACK_EVENT_TYPES:
        return ()

    candidates = [event_type]
    namespace, separator, _suffix = event_type.partition(".")
    wildcard = f"{namespace}.*"
    if separator and wildcard in CALLBACK_EVENT_WILDCARDS:
        candidates.append(wildcard)
    return tuple(candidates)


def _callback_subscription_event_type_filter(
    event_type_candidates: tuple[str, ...],
    dialect_name: str | None,
) -> ColumnElement[bool]:
    event_type_values: Any
    if dialect_name == "postgresql":
        event_type_values = (
            func.jsonb_array_elements_text(CallbackSubscription.event_types.cast(JSONB))
            .table_valued("value")
            .render_derived(name="callback_event_type")
        )
    else:
        event_type_values = (
            func.json_each(CallbackSubscription.event_types)
            .table_valued("value")
            .alias("callback_event_type")
        )

    return (
        select(1)
        .select_from(event_type_values)
        .where(event_type_values.c.value.in_(event_type_candidates))
        .exists()
    )


class TaskExternalIdConflictError(ValueError):
    """Raised when a caller reuses an external task id for a different task scope."""

    def __init__(self, external_id: str) -> None:
        self.external_id = external_id
        super().__init__(
            f"External task id {external_id!r} already belongs to a different task scope."
        )


class TaskRepository:
    """CRUD helpers for first-class task rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get(
        self,
        *,
        repo_url: str,
        base_branch: str,
        title: str,
        prompt: str,
        external_id: str | None,
        idempotency_key: str | None,
        task_class: str | None,
        owned_paths: list[str],
    ) -> Task:
        existing = await self._find_reusable(
            external_id=external_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            if (
                external_id is not None
                and existing.external_id == external_id
                and not _task_scope_matches(
                    existing,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    task_class=task_class,
                    owned_paths=owned_paths,
                    title=title,
                )
            ):
                raise TaskExternalIdConflictError(external_id)
            if existing.external_id is None and external_id is not None:
                existing.external_id = external_id
            if existing.idempotency_key is None and idempotency_key is not None:
                existing.idempotency_key = idempotency_key
            await self._session.flush()
            return existing

        task = Task(
            id=new_task_id(),
            external_id=external_id,
            idempotency_key=idempotency_key,
            repo_url=repo_url,
            base_branch=base_branch,
            title=title,
            prompt=prompt,
            task_class=task_class,
            owned_paths=list(owned_paths),
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def get(self, task_id: str) -> Task | None:
        return await self._session.get(Task, task_id)

    async def get_by_ref(self, task_ref: str) -> Task | None:
        task = await self.get(task_ref)
        if task is not None:
            return task
        stmt = select(Task).where(Task.external_id == task_ref)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _find_reusable(
        self,
        *,
        external_id: str | None,
        idempotency_key: str | None,
    ) -> Task | None:
        if external_id is not None:
            stmt = select(Task).where(Task.external_id == external_id)
            existing = (await self._session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                return existing

        if idempotency_key is not None:
            stmt = select(Task).where(Task.idempotency_key == idempotency_key)
            return (await self._session.execute(stmt)).scalar_one_or_none()

        return None


def _task_scope_matches(
    task: Task,
    *,
    repo_url: str,
    base_branch: str,
    task_class: str | None,
    owned_paths: list[str],
    title: str | None = None,
) -> bool:
    return (
        task.repo_url == repo_url
        and task.base_branch == base_branch
        and task.task_class == task_class
        and list(task.owned_paths) == list(owned_paths)
        and (title is None or task.title == title)
    )


class TaskAttemptRepository:
    """CRUD helpers for task-attempt rows."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

    async def create_for_workspace(
        self,
        *,
        task: Task,
        workspace: Workspace,
        parent_attempt_id: str | None = None,
        redispatch_from_attempt_id: str | None = None,
    ) -> TaskAttempt:
        await self._lock_attempt_number_sequence(task.id)
        max_attempt_number = (
            await self._session.execute(
                select(func.max(TaskAttempt.attempt_number)).where(TaskAttempt.task_id == task.id)
            )
        ).scalar_one()
        attempt_number = (max_attempt_number or 0) + 1
        attempt = TaskAttempt(
            id=new_task_attempt_id(),
            task_id=task.id,
            workspace_id=workspace.id,
            attempt_number=attempt_number,
            parent_attempt_id=parent_attempt_id,
            redispatch_from_attempt_id=redispatch_from_attempt_id,
            agent=workspace.agent,
            status=workspace.status,
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def _lock_attempt_number_sequence(self, task_id: str) -> None:
        if self._dialect_name != "postgresql":
            return

        await self._session.execute(self._attempt_number_sequence_lock_stmt(task_id))

    @staticmethod
    def _attempt_number_sequence_lock_stmt(task_id: str) -> Select[tuple[str]]:
        return select(Task.id).where(Task.id == task_id).with_for_update()

    async def get_by_workspace_id(self, workspace_id: str) -> TaskAttempt | None:
        stmt = select(TaskAttempt).where(TaskAttempt.workspace_id == workspace_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_canonical_for_task(self, task_id: str) -> TaskAttempt | None:
        stmt = select(TaskAttempt).where(
            TaskAttempt.task_id == task_id,
            TaskAttempt.is_canonical_for_merge.is_(True),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_canonical_ids_for_tasks(self, task_ids: Iterable[str]) -> dict[str, str]:
        unique_task_ids = tuple(dict.fromkeys(task_ids))
        if not unique_task_ids:
            return {}

        stmt = select(TaskAttempt.task_id, TaskAttempt.id).where(
            TaskAttempt.task_id.in_(unique_task_ids),
            TaskAttempt.is_canonical_for_merge.is_(True),
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return dict(rows)

    async def mark_canonical_for_merge(self, attempt: TaskAttempt) -> TaskAttempt | None:
        previous = await self.get_canonical_for_task(attempt.task_id)
        if previous is not None and previous.id != attempt.id:
            previous.is_canonical_for_merge = False
            previous.superseded_by_attempt_id = attempt.id
            await self._session.flush([previous])
        attempt.is_canonical_for_merge = True
        await self._session.flush()
        return previous

    async def list_for_task(self, task_id: str, *, limit: int = 100) -> list[TaskAttempt]:
        stmt = (
            select(TaskAttempt)
            .where(TaskAttempt.task_id == task_id)
            .options(
                selectinload(TaskAttempt.workspace).selectinload(Workspace.operations),
                selectinload(TaskAttempt.merge_candidate),
            )
            .order_by(TaskAttempt.attempt_number.desc(), TaskAttempt.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_latest(
        self,
        *,
        status: WorkspaceStatus | str | None = None,
        agent: AgentRuntime | str | None = None,
        repo_url: str | None = None,
        limit: int = 50,
    ) -> list[TaskAttempt]:
        latest_attempt_numbers = (
            select(
                TaskAttempt.task_id.label("task_id"),
                func.max(TaskAttempt.attempt_number).label("attempt_number"),
            )
            .group_by(TaskAttempt.task_id)
            .subquery()
        )
        stmt = (
            select(TaskAttempt)
            .join(
                latest_attempt_numbers,
                and_(
                    TaskAttempt.task_id == latest_attempt_numbers.c.task_id,
                    TaskAttempt.attempt_number == latest_attempt_numbers.c.attempt_number,
                ),
            )
            .join(Workspace, TaskAttempt.workspace_id == Workspace.id)
            .options(
                selectinload(TaskAttempt.task),
                selectinload(TaskAttempt.workspace).selectinload(Workspace.operations),
                selectinload(TaskAttempt.merge_candidate),
            )
        )
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if agent is not None:
            stmt = stmt.where(TaskAttempt.agent == agent)
        if repo_url is not None:
            stmt = stmt.where(TaskAttempt.repo_url == repo_url)

        stmt = stmt.order_by(TaskAttempt.created_at.desc(), TaskAttempt.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())


class ProviderModelCircuitBreakerRepository:
    """CRUD helpers for provider/model circuit breaker cooldown state."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

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


def _circuit_breaker_expired(
    breaker: ProviderModelCircuitBreaker,
    now: datetime,
) -> bool:
    cooldown_until = breaker.cooldown_until
    if breaker.state != "open" or cooldown_until is None:
        return False
    return _as_utc_naive(cooldown_until) <= _as_utc_naive(now)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


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


class ResourceReservationRepository:
    """CRUD helpers for local resource reservation records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: str,
        attempt_id: str,
        node_id: str,
        steady_cpu: float,
        steady_memory_gb: float,
        peak_cpu: float,
        peak_memory_gb: float,
        disk_mb: int | None,
        dind_slots: int = 0,
        phase: str,
        reserved_at: datetime | None = None,
    ) -> ResourceReservation:
        row = ResourceReservation(
            id=new_resource_reservation_id(),
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            dind_slots=dind_slots,
            phase=phase,
            reserved_at=reserved_at or datetime.now(UTC),
            released_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_for_workspace(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[ResourceReservation]:
        stmt = (
            select(ResourceReservation)
            .where(ResourceReservation.workspace_id == workspace_id)
            .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def active_for_workspace(self, workspace_id: str) -> ResourceReservation | None:
        stmt = (
            select(ResourceReservation)
            .where(
                ResourceReservation.workspace_id == workspace_id,
                ResourceReservation.released_at.is_(None),
            )
            .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def release_active_for_workspace(
        self,
        workspace_id: str,
        *,
        released_at: datetime | None = None,
    ) -> builtins.list[ResourceReservation]:
        release_time = released_at or datetime.now(UTC)
        result = await self._session.execute(
            update(ResourceReservation)
            .where(
                ResourceReservation.workspace_id == workspace_id,
                ResourceReservation.released_at.is_(None),
            )
            .values(released_at=release_time)
            .returning(ResourceReservation)
        )
        rows = list(result.scalars())
        rows.sort(key=lambda row: (row.reserved_at, row.id))
        return rows

    async def active_latest_totals(self) -> dict[str, float | int]:
        latest_active_reservations = (
            select(
                ResourceReservation.workspace_id.label("workspace_id"),
                ResourceReservation.steady_cpu.label("steady_cpu"),
                ResourceReservation.steady_memory_gb.label("steady_memory_gb"),
                ResourceReservation.peak_cpu.label("peak_cpu"),
                ResourceReservation.peak_memory_gb.label("peak_memory_gb"),
                ResourceReservation.disk_mb.label("disk_mb"),
                ResourceReservation.dind_slots.label("dind_slots"),
                func.row_number()
                .over(
                    partition_by=ResourceReservation.workspace_id,
                    order_by=(
                        ResourceReservation.reserved_at.desc(),
                        ResourceReservation.id.desc(),
                    ),
                )
                .label("reservation_rank"),
            )
            .join(Workspace, ResourceReservation.workspace_id == Workspace.id)
            .where(
                ResourceReservation.released_at.is_(None),
                ~Workspace.status.in_(ACTIVE_RESOURCE_RESERVATION_EXCLUDED_STATUSES),
            )
            .subquery()
        )
        stmt = (
            select(
                func.count(latest_active_reservations.c.workspace_id),
                func.coalesce(func.sum(latest_active_reservations.c.steady_cpu), 0.0),
                func.coalesce(
                    func.sum(latest_active_reservations.c.steady_memory_gb),
                    0.0,
                ),
                func.coalesce(func.sum(latest_active_reservations.c.peak_cpu), 0.0),
                func.coalesce(
                    func.sum(latest_active_reservations.c.peak_memory_gb),
                    0.0,
                ),
                func.coalesce(func.sum(latest_active_reservations.c.disk_mb), 0),
                func.coalesce(func.sum(latest_active_reservations.c.dind_slots), 0),
            )
            .select_from(latest_active_reservations)
            .where(latest_active_reservations.c.reservation_rank == 1)
        )
        row = (await self._session.execute(stmt)).one()
        return {
            "workspace_count": int(row[0] or 0),
            "steady_cpu": float(row[1] or 0.0),
            "steady_memory_gb": float(row[2] or 0.0),
            "peak_cpu": float(row[3] or 0.0),
            "peak_memory_gb": float(row[4] or 0.0),
            "disk_mb": int(row[5] or 0),
            "dind_slots": int(row[6] or 0),
        }


class MergeCandidateRepository:
    """CRUD helpers for explicit PR-backed merge candidates."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_attempt_id(self, attempt_id: str) -> MergeCandidate | None:
        stmt = (
            select(MergeCandidate)
            .where(MergeCandidate.attempt_id == attempt_id)
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.workspace),
                selectinload(MergeCandidate.task),
            )
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_open_for_workspace_with_merge_inputs(
        self,
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
                selectinload(MergeCandidate.task),
                selectinload(MergeCandidate.policy_findings),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
                selectinload(MergeCandidate.workspace).selectinload(
                    Workspace.validation_runs
                ),
                selectinload(MergeCandidate.workspace).selectinload(
                    Workspace.policy_findings
                ),
            )
            .order_by(MergeCandidate.created_at.desc(), MergeCandidate.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_task(
        self,
        task_id: str,
        *,
        limit: int = 100,
    ) -> builtins.list[MergeCandidate]:
        stmt = (
            select(MergeCandidate)
            .where(MergeCandidate.task_id == task_id)
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.events),
                selectinload(MergeCandidate.task),
            )
            .order_by(
                MergeCandidate.updated_at.desc(),
                MergeCandidate.id.desc(),
            )
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_queue(
        self,
        *,
        repo_url: str | None = None,
        base_branch: str | None = None,
        status: WorkspaceStatus | str | None = None,
        before_updated_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[MergeCandidate]:
        stmt = (
            select(MergeCandidate)
            .join(Workspace, MergeCandidate.workspace_id == Workspace.id)
            .where(
                MergeCandidate.status == "open",
                ~Workspace.status.in_(
                    (
                        WorkspaceStatus.destroying.value,
                        WorkspaceStatus.destroyed.value,
                    )
                ),
            )
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.events),
                selectinload(MergeCandidate.task),
            )
            .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        )
        if repo_url is not None:
            stmt = stmt.where(MergeCandidate.repo_url == repo_url)
        if base_branch is not None:
            stmt = stmt.where(MergeCandidate.base_branch == base_branch)
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if before_updated_at is not None and before_workspace_id is not None:
            stmt = stmt.where(
                or_(
                    Workspace.updated_at < before_updated_at,
                    and_(
                        Workspace.updated_at == before_updated_at,
                        Workspace.id < before_workspace_id,
                    ),
                )
            )
        stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def create_or_update_open_for_attempt(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        workspace: Workspace,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> MergeCandidate:
        if not workspace.pr_url:
            raise ValueError("merge candidates require a workspace PR URL")

        candidate = await self.get_by_attempt_id(attempt.id)
        if candidate is None:
            candidate = MergeCandidate(
                id=new_merge_candidate_id(),
                task_id=task.id,
                attempt_id=attempt.id,
                workspace_id=workspace.id,
                pr_url=workspace.pr_url,
                pr_number=workspace.pr_number,
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                branch_name=workspace.branch_name,
                head_sha=head_sha,
                base_sha=base_sha,
                status="open",
            )
            self._session.add(candidate)
        else:
            candidate.task_id = task.id
            candidate.workspace_id = workspace.id
            candidate.pr_url = workspace.pr_url
            candidate.pr_number = workspace.pr_number
            candidate.repo_url = workspace.repo_url
            candidate.base_branch = workspace.branch_base
            candidate.branch_name = workspace.branch_name
            if head_sha is not None:
                candidate.head_sha = head_sha
            if base_sha is not None:
                candidate.base_sha = base_sha
            candidate.status = "open"
            candidate.close_reason = None
            candidate.closed_at = None
            candidate.merged_at = None

        sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)
        await self._session.flush()
        return candidate

    async def make_attempt_canonical_and_create_candidate(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        workspace: Workspace,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> MergeCandidate:
        attempt_repo = TaskAttemptRepository(self._session)
        previous = await attempt_repo.mark_canonical_for_merge(attempt)
        if previous is not None and previous.id != attempt.id:
            await self.close_open_for_attempt(
                previous.id,
                close_reason="CANONICAL_CHANGED",
            )
        return await self.create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha=head_sha,
            base_sha=base_sha,
        )

    async def close_open_for_attempt(
        self,
        attempt_id: str,
        *,
        close_reason: str,
    ) -> MergeCandidate | None:
        candidate = await self.get_by_attempt_id(attempt_id)
        if candidate is None or candidate.status != "open":
            return candidate
        candidate.status = "closed"
        candidate.close_reason = close_reason
        candidate.closed_at = datetime.now(UTC)
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
        )
        await self._session.flush()
        return candidate

    async def close_open_for_workspace(
        self,
        workspace_id: str,
        *,
        close_reason: str,
    ) -> builtins.list[MergeCandidate]:
        stmt = (
            select(MergeCandidate)
            .where(
                MergeCandidate.workspace_id == workspace_id,
                MergeCandidate.status == "open",
            )
            .options(
                selectinload(MergeCandidate.workspace),
                selectinload(MergeCandidate.attempt),
            )
        )
        candidates = list((await self._session.execute(stmt)).scalars())
        now = datetime.now(UTC)
        for candidate in candidates:
            candidate.status = "closed"
            candidate.close_reason = close_reason
            candidate.closed_at = now
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
            )
        await self._session.flush()
        return candidates

    async def mark_workspace_merged(self, workspace_id: str) -> MergeCandidate | None:
        stmt = (
            select(MergeCandidate)
            .where(
                MergeCandidate.workspace_id == workspace_id,
                MergeCandidate.status == "open",
            )
            .options(
                selectinload(MergeCandidate.workspace),
                selectinload(MergeCandidate.attempt),
            )
            .order_by(MergeCandidate.updated_at.desc(), MergeCandidate.id.desc())
            .limit(1)
        )
        candidate = (await self._session.execute(stmt)).scalar_one_or_none()
        if candidate is None:
            return None
        now = datetime.now(UTC)
        candidate.status = "merged"
        candidate.close_reason = None
        candidate.closed_at = None
        candidate.merged_at = now
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
        )
        await self._session.flush()
        return candidate


class ValidationRunRepository:
    """CRUD helpers for durable validation provenance rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        workspace_id: str,
        attempt_id: str | None,
        tier: int,
        commands: list[dict[str, Any]],
        base_commit: str | None,
        target_branch: str | None,
        target_head_sha: str | None,
        log_stream_refs: dict[str, Any],
        base_sha: str | None = None,
        workspace_head_sha: str | None = None,
        profile_name: str | None = None,
        profile_version: int | None = None,
        profile_source: str | None = None,
        resolved_profile_digest: str | None = None,
        environment_identity_digest: str | None = None,
        environment_identity_inputs: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> ValidationRun:
        now = started_at or datetime.now(UTC)
        run = ValidationRun(
            id=new_validation_run_id(),
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            tier=tier,
            command_set_hash=validation_command_set_hash(commands),
            commands=commands,
            base_commit=base_commit,
            base_sha=base_sha,
            workspace_head_sha=workspace_head_sha,
            target_branch=target_branch,
            target_head_sha=target_head_sha,
            profile_name=profile_name,
            profile_version=profile_version,
            profile_source=profile_source,
            resolved_profile_digest=resolved_profile_digest,
            environment_identity_digest=environment_identity_digest,
            environment_identity_inputs=environment_identity_inputs,
            status="running",
            reason_code=None,
            started_at=now,
            finished_at=None,
            log_stream_refs=log_stream_refs,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, validation_run_id: str) -> ValidationRun | None:
        return await self._session.get(ValidationRun, validation_run_id)

    async def finish(
        self,
        validation_run_id: str,
        *,
        status: str,
        reason_code: str | None,
        finished_at: datetime | None = None,
        retry_count: int = 0,
        coverage: dict[str, Any] | None = None,
        command_retries: list[int] | None = None,
        coverage_evidence_status: str | None = None,
        coverage_evidence_reason_code: str | None = None,
        coverage_evidence_source_run_id: str | None = None,
    ) -> ValidationRun | None:
        run = await self.get(validation_run_id)
        if run is None:
            return None
        run.status = status
        run.reason_code = reason_code
        run.retry_count = retry_count
        run.finished_at = finished_at or datetime.now(UTC)
        if coverage is not None:
            log_stream_refs = dict(run.log_stream_refs or {})
            log_stream_refs["coverage"] = dict(coverage)
            run.log_stream_refs = log_stream_refs
        if command_retries is not None:
            updated_commands = list(run.commands)
            for i, rc in enumerate(command_retries):
                if i < len(updated_commands):
                    updated_commands[i] = dict(updated_commands[i], retry_count=rc)
            run.commands = updated_commands
        if coverage_evidence_status is not None:
            updated_commands = list(run.commands)
            for i, command in enumerate(updated_commands):
                if command.get("phase") == "coverage":
                    updated = dict(command)
                    updated["evidence_status"] = coverage_evidence_status
                    if coverage_evidence_reason_code is not None:
                        updated["evidence_reason_code"] = coverage_evidence_reason_code
                    if coverage_evidence_source_run_id is not None:
                        updated["evidence_source_run_id"] = coverage_evidence_source_run_id
                    updated_commands[i] = updated
                    break
            run.commands = updated_commands
        await self._session.flush()
        return run

    async def update_target_head_sha(
        self,
        validation_run_id: str,
        *,
        target_head_sha: str | None,
        workspace_head_sha: str | None = None,
    ) -> ValidationRun | None:
        run = await self.get(validation_run_id)
        if run is None:
            return None
        run.target_head_sha = target_head_sha
        if workspace_head_sha is not None:
            run.workspace_head_sha = workspace_head_sha
        await self._session.flush()
        return run

    async def find_reusable_coverage_evidence(
        self,
        *,
        workspace_id: str,
        tier: int,
        commands: list[dict[str, Any]],
        workspace_head_sha: str | None,
        resolved_profile_digest: str | None,
        environment_identity_digest: str | None,
        max_age_seconds: int,
        now: datetime | None = None,
    ) -> ValidationRun | None:
        if not workspace_head_sha:
            return None
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=max_age_seconds)
        stmt = (
            select(ValidationRun)
            .where(
                ValidationRun.workspace_id == workspace_id,
                ValidationRun.tier == tier,
                ValidationRun.workspace_head_sha == workspace_head_sha,
                ValidationRun.command_set_hash == validation_command_set_hash(commands),
                ValidationRun.resolved_profile_digest == resolved_profile_digest,
                ValidationRun.environment_identity_digest == environment_identity_digest,
                ValidationRun.status == "succeeded",
                ValidationRun.finished_at.is_not(None),
                ValidationRun.finished_at >= cutoff,
            )
            .order_by(ValidationRun.finished_at.desc(), ValidationRun.id.desc())
            .limit(5)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        for row in rows:
            coverage = (row.log_stream_refs or {}).get("coverage")
            if isinstance(coverage, Mapping) and coverage.get("status") in {
                "passed",
                "not_configured",
            }:
                return row
        return None

    async def list_for_workspace(self, workspace_id: str) -> builtins.list[ValidationRun]:
        stmt = (
            select(ValidationRun)
            .where(ValidationRun.workspace_id == workspace_id)
            .order_by(ValidationRun.started_at, ValidationRun.id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_by_workspace_ids(
        self,
        workspace_ids: Iterable[str],
        *,
        status: str | None = None,
    ) -> dict[str, builtins.list[ValidationRun]]:
        unique_workspace_ids = tuple(dict.fromkeys(workspace_ids))
        if not unique_workspace_ids:
            return {}

        stmt = select(ValidationRun).where(
            ValidationRun.workspace_id.in_(unique_workspace_ids)
        )
        if status is not None:
            stmt = stmt.where(ValidationRun.status == status)
        stmt = stmt.order_by(
            ValidationRun.workspace_id.asc(),
            ValidationRun.started_at.asc(),
            ValidationRun.id.asc(),
        )
        out: dict[str, builtins.list[ValidationRun]] = {
            workspace_id: [] for workspace_id in unique_workspace_ids
        }
        for run in (await self._session.execute(stmt)).scalars():
            out[run.workspace_id].append(run)
        return out

    async def latest_by_workspace_ids(
        self,
        workspace_ids: Iterable[str],
    ) -> dict[str, ValidationRun]:
        unique_workspace_ids = tuple(dict.fromkeys(workspace_ids))
        if not unique_workspace_ids:
            return {}

        ranked_runs = (
            select(
                ValidationRun.id.label("validation_run_id"),
                func.row_number()
                .over(
                    partition_by=ValidationRun.workspace_id,
                    order_by=(
                        ValidationRun.started_at.desc(),
                        ValidationRun.id.desc(),
                    ),
                )
                .label("run_rank"),
            )
            .where(ValidationRun.workspace_id.in_(unique_workspace_ids))
            .subquery()
        )
        stmt = (
            select(ValidationRun)
            .join(ranked_runs, ValidationRun.id == ranked_runs.c.validation_run_id)
            .where(ranked_runs.c.run_rank == 1)
        )
        return {run.workspace_id: run for run in (await self._session.execute(stmt)).scalars()}


@dataclass(frozen=True)
class StaleReasonCreate:
    """Per-finding payload for ``StaleReasonRepository.replace_active_findings``."""

    reason_code: str
    trigger_type: str
    trigger_ref: str | None
    explanation: str


class StaleReasonRepository:
    """CRUD helpers for the durable ``stale_reasons`` table.

    Used by the staleness refresh service to keep ``status='active'`` rows
    in lockstep with the latest evaluation against the target branch. Rows
    are flipped to ``status='resolved'`` (with ``resolved_at`` set) when a
    finding no longer applies — the historical row stays so console
    timelines can show recovery, not just degradation.
    """

    _ACTIVE = "active"
    _RESOLVED = "resolved"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_for_candidate(
        self,
        candidate_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_candidate(candidate_id, status=self._ACTIVE)

    async def list_active_for_candidates(
        self,
        candidate_ids: Iterable[str],
    ) -> dict[str, builtins.list[StaleReason]]:
        unique_ids = tuple(dict.fromkeys(candidate_ids))
        if not unique_ids:
            return {}
        stmt = (
            select(StaleReason)
            .where(
                StaleReason.candidate_id.in_(unique_ids),
                StaleReason.status == self._ACTIVE,
            )
            .order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        out: dict[str, builtins.list[StaleReason]] = {cid: [] for cid in unique_ids}
        for row in rows:
            if row.candidate_id is None:  # pragma: no cover - filtered by WHERE
                continue
            out[row.candidate_id].append(row)
        return out

    async def list_for_candidate(
        self,
        candidate_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_candidate(candidate_id, status=None)

    async def list_active_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(workspace_id, status=self._ACTIVE)

    async def list_active_for_workspace_page(
        self,
        workspace_id: str,
        *,
        limit: int,
        offset: int,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(
            workspace_id,
            status=self._ACTIVE,
            limit=limit,
            offset=offset,
        )

    async def list_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(workspace_id, status=None)

    async def list_for_workspace_page(
        self,
        workspace_id: str,
        *,
        limit: int,
        offset: int,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(
            workspace_id,
            status=None,
            limit=limit,
            offset=offset,
        )

    async def replace_active_findings(
        self,
        *,
        workspace_id: str,
        candidate_id: str,
        attempt_id: str | None,
        task_id: str | None,
        findings: builtins.list[StaleReasonCreate],
    ) -> tuple[builtins.list[StaleReason], builtins.list[StaleReason]]:
        """Make the active findings for ``candidate_id`` exactly match the
        supplied list. Returns ``(newly_added, newly_resolved)``.

        Idempotent: re-running with the same findings is a no-op (kept rows
        are not re-emitted as ``newly_added``).
        """
        existing_active = await self._list_for_candidate(
            candidate_id,
            status=self._ACTIVE,
        )
        finding_keys = {(f.reason_code, f.trigger_type, f.trigger_ref) for f in findings}
        now = datetime.now(UTC)

        newly_resolved: builtins.list[StaleReason] = []
        kept_keys: set[tuple[str, str, str | None]] = set()
        for row in existing_active:
            key = (row.reason_code, row.trigger_type, row.trigger_ref)
            if key in finding_keys:
                kept_keys.add(key)
                continue
            row.status = self._RESOLVED
            row.resolved_at = now
            newly_resolved.append(row)

        newly_added: builtins.list[StaleReason] = []
        for finding in findings:
            key = (finding.reason_code, finding.trigger_type, finding.trigger_ref)
            if key in kept_keys:
                continue
            row = StaleReason(
                id=new_stale_reason_id(),
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                task_id=task_id,
                trigger_type=finding.trigger_type,
                trigger_ref=finding.trigger_ref,
                reason_code=finding.reason_code,
                explanation=finding.explanation,
                status=self._ACTIVE,
                detected_at=now,
                resolved_at=None,
            )
            self._session.add(row)
            newly_added.append(row)

        await self._session.flush()
        return newly_added, newly_resolved

    async def _list_for_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None,
    ) -> builtins.list[StaleReason]:
        stmt = select(StaleReason).where(StaleReason.candidate_id == candidate_id)
        if status is not None:
            stmt = stmt.where(StaleReason.status == status)
        stmt = stmt.order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
        return list((await self._session.execute(stmt)).scalars())

    async def _list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: str | None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> builtins.list[StaleReason]:
        stmt = select(StaleReason).where(StaleReason.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(StaleReason.status == status)
        stmt = stmt.order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)
        return list((await self._session.execute(stmt)).scalars())


@dataclass(frozen=True)
class PolicyFindingCreate:
    """Per-finding payload for ``PolicyFindingRepository.replace_active_findings``."""

    reason_code: str
    severity: str
    subject_path: str | None
    explanation: str
    details: dict[str, Any]


class PolicyFindingRepository:
    """CRUD helpers for durable workspace policy findings."""

    _ACTIVE = "active"
    _RESOLVED = "resolved"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[PolicyFinding]:
        return await self._list_for_workspace(workspace_id, status=self._ACTIVE)

    async def list_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[PolicyFinding]:
        return await self._list_for_workspace(workspace_id, status=None)

    async def list_active_for_candidate(
        self,
        candidate_id: str,
    ) -> builtins.list[PolicyFinding]:
        return await self._list_for_candidate(candidate_id, status=self._ACTIVE)

    async def list_active_for_candidates(
        self,
        candidate_ids: Iterable[str],
    ) -> dict[str, builtins.list[PolicyFinding]]:
        unique_ids = tuple(dict.fromkeys(candidate_ids))
        if not unique_ids:
            return {}
        stmt = (
            select(PolicyFinding)
            .where(
                PolicyFinding.candidate_id.in_(unique_ids),
                PolicyFinding.status == self._ACTIVE,
            )
            .order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        out: dict[str, builtins.list[PolicyFinding]] = {cid: [] for cid in unique_ids}
        for row in rows:
            if row.candidate_id is None:  # pragma: no cover - filtered by WHERE
                continue
            out[row.candidate_id].append(row)
        return out

    async def replace_active_findings(
        self,
        *,
        workspace_id: str,
        candidate_id: str | None,
        attempt_id: str | None,
        task_id: str | None,
        reason_code: str,
        findings: builtins.list[PolicyFindingCreate],
    ) -> tuple[builtins.list[PolicyFinding], builtins.list[PolicyFinding]]:
        """Make active findings for ``reason_code`` exactly match ``findings``.

        Historical rows are resolved rather than deleted so operator timelines
        can show when a policy issue appeared and when it cleared.
        """
        existing_active = await self._list_for_subject(
            workspace_id=workspace_id,
            candidate_id=candidate_id,
            reason_code=reason_code,
            status=self._ACTIVE,
        )
        finding_keys = {
            (
                f.reason_code,
                f.severity,
                f.subject_path,
                json.dumps(f.details, sort_keys=True, separators=(",", ":")),
            )
            for f in findings
        }
        now = datetime.now(UTC)

        newly_resolved: builtins.list[PolicyFinding] = []
        kept_keys: set[tuple[str, str, str | None, str]] = set()
        for row in existing_active:
            key = (
                row.reason_code,
                row.severity,
                row.subject_path,
                json.dumps(row.details, sort_keys=True, separators=(",", ":")),
            )
            if key in finding_keys:
                kept_keys.add(key)
                continue
            row.status = self._RESOLVED
            row.resolved_at = now
            newly_resolved.append(row)

        newly_added: builtins.list[PolicyFinding] = []
        for finding in findings:
            key = (
                finding.reason_code,
                finding.severity,
                finding.subject_path,
                json.dumps(finding.details, sort_keys=True, separators=(",", ":")),
            )
            if key in kept_keys:
                continue
            row = PolicyFinding(
                id=new_policy_finding_id(),
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                task_id=task_id,
                reason_code=finding.reason_code,
                severity=finding.severity,
                subject_path=finding.subject_path,
                explanation=finding.explanation,
                details=finding.details,
                status=self._ACTIVE,
                detected_at=now,
                resolved_at=None,
            )
            self._session.add(row)
            newly_added.append(row)

        await self._session.flush()
        return newly_added, newly_resolved

    async def _list_for_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None,
    ) -> builtins.list[PolicyFinding]:
        stmt = select(PolicyFinding).where(PolicyFinding.candidate_id == candidate_id)
        if status is not None:
            stmt = stmt.where(PolicyFinding.status == status)
        stmt = stmt.order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        return list((await self._session.execute(stmt)).scalars())

    async def _list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: str | None,
    ) -> builtins.list[PolicyFinding]:
        stmt = select(PolicyFinding).where(PolicyFinding.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(PolicyFinding.status == status)
        stmt = stmt.order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        return list((await self._session.execute(stmt)).scalars())

    async def _list_for_subject(
        self,
        *,
        workspace_id: str,
        candidate_id: str | None,
        reason_code: str,
        status: str | None,
    ) -> builtins.list[PolicyFinding]:
        stmt = select(PolicyFinding).where(
            PolicyFinding.workspace_id == workspace_id,
            PolicyFinding.reason_code == reason_code,
        )
        if candidate_id is None:
            stmt = stmt.where(PolicyFinding.candidate_id.is_(None))
        else:
            stmt = stmt.where(PolicyFinding.candidate_id == candidate_id)
        if status is not None:
            stmt = stmt.where(PolicyFinding.status == status)
        stmt = stmt.order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        return list((await self._session.execute(stmt)).scalars())


SECRET_LEASE_AUDIT_EVENT_TYPE: Final = "workspace.secret_lease"
SECRET_LEASE_AUDIT_SCHEMA: Final = "secret_lease_audit.v1"
SECRET_LEASE_STATUS_ISSUED: Final = "issued"
SECRET_LEASE_STATUS_MOUNTED: Final = "mounted"
SECRET_LEASE_STATUS_EXPIRED: Final = "expired"
SECRET_LEASE_STATUS_REVOKED: Final = "revoked"
_SECRET_LEASE_ACTIVE_STATUSES: Final = (
    SECRET_LEASE_STATUS_ISSUED,
    SECRET_LEASE_STATUS_MOUNTED,
)
_SECRET_LEASE_REVOCABLE_STATUSES: Final = (
    SECRET_LEASE_STATUS_ISSUED,
    SECRET_LEASE_STATUS_MOUNTED,
    SECRET_LEASE_STATUS_EXPIRED,
)


@dataclass(frozen=True)
class SecretLeaseIssue:
    secret_name: str
    kind: str
    target: str
    mode: str
    required: bool
    provider: str | None
    ref_digest: str | None
    expires_at: datetime | None
    issue_metadata: dict[str, Any]
    attempt_id: str | None = None


class SecretLeaseRepository:
    """CRUD helpers for local workspace secret lease metadata."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

    async def issue_declared_leases(
        self,
        workspace: Workspace,
        *,
        leases: Iterable[SecretLeaseIssue],
        now: datetime,
    ) -> list[WorkspaceSecretLease]:
        issues = list(leases)
        if not issues:
            return []

        existing_by_declaration = await self._leases_by_declaration_for_workspace(workspace.id)
        issue_events: list[WorkspaceSecretLease] = []
        results: list[WorkspaceSecretLease] = []
        for issue in issues:
            declaration_key = _secret_lease_declaration_key(
                issue.secret_name,
                issue.kind,
                issue.target,
            )
            existing = existing_by_declaration.get(declaration_key)
            if existing is not None:
                if _declared_lease_requires_reissue(existing, issue):
                    _reissue_declared_lease(existing, issue=issue, now=now)
                    issue_events.append(existing)
                results.append(existing)
                continue

            issued = await self._issue_declared_lease_if_absent(
                workspace,
                issue=issue,
                now=now,
            )
            existing_by_declaration[declaration_key] = issued.lease
            results.append(issued.lease)
            if issued.issue_event_required:
                issue_events.append(issued.lease)
        if issue_events:
            await self._add_lease_events(
                workspace,
                leases=issue_events,
                reason_code="SECRET_LEASE_ISSUED",
                action="issue",
                now=now,
            )
        return results

    async def _issue_declared_lease_if_absent(
        self,
        workspace: Workspace,
        *,
        issue: SecretLeaseIssue,
        now: datetime,
    ) -> _IssuedSecretLease:
        values = {
            "id": new_secret_lease_id(),
            "workspace_id": workspace.id,
            "attempt_id": issue.attempt_id,
            "secret_name": issue.secret_name,
            "kind": issue.kind,
            "target": issue.target,
            "mode": issue.mode,
            "required": issue.required,
            "provider": issue.provider,
            "ref_digest": issue.ref_digest,
            "status": SECRET_LEASE_STATUS_ISSUED,
            "issued_at": now,
            "expires_at": issue.expires_at,
            "issue_metadata": _sanitize_metadata(issue.issue_metadata),
            "mount_metadata": {},
        }
        conflict_guarded, inserted_id = await self._insert_declared_lease_if_absent(values)
        if inserted_id is not None:
            lease = await self._session.get(WorkspaceSecretLease, inserted_id)
            if lease is None:
                raise RuntimeError(f"inserted secret lease {inserted_id} was not visible")
            set_committed_value(lease, "issued_at", now)
            set_committed_value(lease, "expires_at", issue.expires_at)
            return _IssuedSecretLease(lease=lease, issue_event_required=True)

        existing = await self._get_for_declaration(
            workspace.id,
            secret_name=issue.secret_name,
            kind=issue.kind,
            target=issue.target,
        )
        if existing is not None:
            if _declared_lease_requires_reissue(existing, issue):
                _reissue_declared_lease(existing, issue=issue, now=now)
                return _IssuedSecretLease(lease=existing, issue_event_required=True)
            return _IssuedSecretLease(lease=existing, issue_event_required=False)
        if conflict_guarded:
            raise RuntimeError(
                "secret lease insert hit a declaration conflict but no existing row was visible"
            )

        lease = WorkspaceSecretLease(**values)
        self._session.add(lease)
        await self._session.flush()
        return _IssuedSecretLease(lease=lease, issue_event_required=True)

    async def _insert_declared_lease_if_absent(
        self,
        values: Mapping[str, Any],
    ) -> tuple[bool, str | None]:
        stmt = _secret_lease_insert_if_absent_stmt(self._dialect_name)
        if stmt is None:
            return False, None
        result = await self._session.execute(stmt.values(**values))
        return True, result.scalar_one_or_none()

    async def mark_issued_mounted(
        self,
        workspace: Workspace,
        *,
        now: datetime,
        mount_metadata: Mapping[str, Any] | None = None,
    ) -> list[WorkspaceSecretLease]:
        leases = await self._list_for_workspace_statuses(
            workspace.id,
            statuses=(SECRET_LEASE_STATUS_ISSUED,),
        )
        sanitized_metadata = _sanitize_metadata(dict(mount_metadata or {}))
        for lease in leases:
            lease.status = SECRET_LEASE_STATUS_MOUNTED
            lease.mounted_at = now
            lease.mount_metadata = sanitized_metadata
        await self._session.flush()
        if leases:
            await self._add_lease_events(
                workspace,
                leases=leases,
                reason_code="SECRET_LEASE_MOUNTED",
                action="mount",
                now=now,
            )
        return leases

    async def expire_due_leases(self, *, now: datetime) -> list[WorkspaceSecretLease]:
        stmt = (
            select(WorkspaceSecretLease)
            .where(
                WorkspaceSecretLease.status.in_(_SECRET_LEASE_ACTIVE_STATUSES),
                WorkspaceSecretLease.expires_at.is_not(None),
                WorkspaceSecretLease.expires_at <= now,
            )
            .order_by(WorkspaceSecretLease.workspace_id, WorkspaceSecretLease.issued_at)
        )
        leases = list((await self._session.execute(stmt)).scalars())
        if not leases:
            return []
        workspaces = await self._workspaces_by_id({lease.workspace_id for lease in leases})
        for lease in leases:
            lease.status = SECRET_LEASE_STATUS_EXPIRED
        await self._session.flush()
        for workspace_id, workspace_leases in _group_leases_by_workspace(leases).items():
            workspace = workspaces.get(workspace_id)
            if workspace is None:
                continue
            await self._add_lease_events(
                workspace,
                leases=workspace_leases,
                reason_code="SECRET_LEASE_EXPIRED",
                action="expire",
                now=now,
            )
        return leases

    async def revoke_workspace_leases(
        self,
        workspace: Workspace,
        *,
        now: datetime,
        reason_code: str,
    ) -> list[WorkspaceSecretLease]:
        leases = await self._list_for_workspace_statuses(
            workspace.id,
            statuses=_SECRET_LEASE_REVOCABLE_STATUSES,
        )
        for lease in leases:
            lease.status = SECRET_LEASE_STATUS_REVOKED
            lease.revoked_at = now
            lease.revoke_reason_code = reason_code
        await self._session.flush()
        if leases:
            await self._add_lease_events(
                workspace,
                leases=leases,
                reason_code="SECRET_LEASE_REVOKED",
                action="revoke",
                now=now,
            )
        return leases

    async def list_for_workspace(self, workspace_id: str) -> list[WorkspaceSecretLease]:
        stmt = (
            select(WorkspaceSecretLease)
            .where(WorkspaceSecretLease.workspace_id == workspace_id)
            .order_by(WorkspaceSecretLease.issued_at, WorkspaceSecretLease.id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def _get_for_declaration(
        self,
        workspace_id: str,
        *,
        secret_name: str,
        kind: str,
        target: str,
    ) -> WorkspaceSecretLease | None:
        stmt = select(WorkspaceSecretLease).where(
            WorkspaceSecretLease.workspace_id == workspace_id,
            WorkspaceSecretLease.secret_name == secret_name,
            WorkspaceSecretLease.kind == kind,
            WorkspaceSecretLease.target == target,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _leases_by_declaration_for_workspace(
        self,
        workspace_id: str,
    ) -> dict[tuple[str, str, str], WorkspaceSecretLease]:
        stmt = select(WorkspaceSecretLease).where(WorkspaceSecretLease.workspace_id == workspace_id)
        rows = (await self._session.execute(stmt)).scalars()
        return {
            _secret_lease_declaration_key(lease.secret_name, lease.kind, lease.target): lease
            for lease in rows
        }

    async def _list_for_workspace_statuses(
        self,
        workspace_id: str,
        *,
        statuses: tuple[str, ...],
    ) -> list[WorkspaceSecretLease]:
        stmt = (
            select(WorkspaceSecretLease)
            .where(
                WorkspaceSecretLease.workspace_id == workspace_id,
                WorkspaceSecretLease.status.in_(statuses),
            )
            .order_by(WorkspaceSecretLease.issued_at, WorkspaceSecretLease.id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def _workspaces_by_id(self, workspace_ids: set[str]) -> dict[str, Workspace]:
        if not workspace_ids:
            return {}
        stmt = select(Workspace).where(Workspace.id.in_(workspace_ids))
        rows = (await self._session.execute(stmt)).scalars()
        return {workspace.id: workspace for workspace in rows}

    async def _add_lease_events(
        self,
        workspace: Workspace,
        *,
        leases: list[WorkspaceSecretLease],
        reason_code: str,
        action: str,
        now: datetime,
    ) -> None:
        events = [
            WorkspaceEventCreate(
                event_type=SECRET_LEASE_AUDIT_EVENT_TYPE,
                reason_code=reason_code,
                payload=_lease_audit_payload(
                    lease,
                    action=action,
                    reason_code=reason_code,
                    occurred_at=now,
                ),
            )
            for lease in leases
        ]
        await WorkspaceRepository(self._session).add_events(workspace, events=events)


def _sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_audit_value(dict(metadata))
    return redacted if isinstance(redacted, dict) else {}


def _secret_lease_declaration_key(
    secret_name: str,
    kind: str,
    target: str,
) -> tuple[str, str, str]:
    return (secret_name, kind, target)


def _declared_lease_requires_reissue(
    lease: WorkspaceSecretLease,
    issue: SecretLeaseIssue,
) -> bool:
    if lease.status not in _SECRET_LEASE_ACTIVE_STATUSES:
        return True
    return (
        lease.attempt_id != issue.attempt_id
        or lease.mode != issue.mode
        or lease.required != issue.required
        or lease.provider != issue.provider
        or lease.ref_digest != issue.ref_digest
        or lease.issue_metadata != _sanitize_metadata(issue.issue_metadata)
    )


def _reissue_declared_lease(
    lease: WorkspaceSecretLease,
    *,
    issue: SecretLeaseIssue,
    now: datetime,
) -> None:
    lease.attempt_id = issue.attempt_id
    lease.mode = issue.mode
    lease.required = issue.required
    lease.provider = issue.provider
    lease.ref_digest = issue.ref_digest
    lease.status = SECRET_LEASE_STATUS_ISSUED
    lease.issued_at = now
    lease.expires_at = issue.expires_at
    lease.mounted_at = None
    lease.revoked_at = None
    lease.revoke_reason_code = None
    lease.issue_metadata = _sanitize_metadata(issue.issue_metadata)
    lease.mount_metadata = {}


def _lease_audit_payload(
    lease: WorkspaceSecretLease,
    *,
    action: str,
    reason_code: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SECRET_LEASE_AUDIT_SCHEMA,
        "lease_id": lease.id,
        "action": action,
        "reason_code": reason_code,
        "workspace_id": lease.workspace_id,
        "attempt_id": lease.attempt_id,
        "secret_name": lease.secret_name,
        "kind": lease.kind,
        "target": lease.target,
        "mode": lease.mode,
        "required": lease.required,
        "provider": lease.provider,
        "ref_digest": lease.ref_digest,
        "status": lease.status,
        "issued_at": lease.issued_at.isoformat(),
        "mounted_at": lease.mounted_at.isoformat() if lease.mounted_at else None,
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
        "revoked_at": lease.revoked_at.isoformat() if lease.revoked_at else None,
        "revoke_reason_code": lease.revoke_reason_code,
        "occurred_at": occurred_at.isoformat(),
    }
    if lease.issue_metadata:
        payload["issue_metadata"] = _sanitize_metadata(lease.issue_metadata)
    if lease.mount_metadata:
        payload["mount_metadata"] = _sanitize_metadata(lease.mount_metadata)
    return {key: value for key, value in payload.items() if value is not None}


def _group_leases_by_workspace(
    leases: Iterable[WorkspaceSecretLease],
) -> dict[str, list[WorkspaceSecretLease]]:
    grouped: dict[str, list[WorkspaceSecretLease]] = {}
    for lease in leases:
        grouped.setdefault(lease.workspace_id, []).append(lease)
    return grouped


class WorkspaceRepository:
    """CRUD + state transitions for workspaces.

    Holds a reference to an ``AsyncSession`` for the life of one logical unit
    of work. Do not reuse across request boundaries.
    """

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

    async def create(
        self,
        *,
        repo_url: str,
        branch_base: str,
        task_title: str,
        task_prompt: str,
        agent: str,
        test_commands: list[str],
        requires_database: bool = False,
        task_external_id: str | None = None,
        task_class: str | None = None,
        owned_paths: list[str] | None = None,
        task_policy: dict[str, Any] | None = None,
        auto_merge: bool = True,
        initial_review_grace_period_seconds: float | None = None,
        env_profile: str | None = None,
        profile_ref: str | None = None,
        requested_profile: dict[str, Any] | None = None,
        resolved_profile: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        task_kind: str = "feature_branch_pr",
        remote_push_branch: str | None = None,
    ) -> Workspace:
        """Create a new workspace in ``requested`` status and emit a creation event.

        Does not commit — the caller owns the transaction boundary.
        """
        workspace = Workspace(
            id=new_workspace_id(),
            status=WorkspaceStatus.requested.value,
            version=1,
            repo_url=repo_url,
            branch_base=branch_base,
            remote_push_branch=remote_push_branch,
            task_title=task_title,
            task_prompt=task_prompt,
            task_external_id=task_external_id,
            task_class=task_class,
            task_kind=task_kind,
            owned_paths=list(owned_paths or []),
            task_policy=dict(task_policy or {}),
            auto_merge=auto_merge,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            agent=agent,
            env_profile=env_profile,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            test_commands=test_commands,
            requires_database=requires_database,
            idempotency_key=idempotency_key,
        )
        # Append to the relationship so both the session AND the in-memory
        # ``workspace.events`` collection are populated. A bare ``session.add(event)``
        # would only add the row; callers reading ``workspace.events`` would then
        # trigger a lazy load, which fails in async contexts.
        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.created",
                old_state=None,
                new_state=WorkspaceStatus.requested.value,
                reason_code="CREATED",
            )
        )
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def get(self, workspace_id: str) -> Workspace | None:
        return await self._session.get(Workspace, workspace_id)

    async def get_with_secret_leases(self, workspace_id: str) -> Workspace | None:
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.secret_leases))
            .options(selectinload(Workspace.operations))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_with_operations(self, workspace_id: str) -> Workspace | None:
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.operations))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_with_validation_runs(self, workspace_id: str) -> Workspace | None:
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.validation_runs))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, workspace_id: str) -> Workspace | None:
        """Load one workspace with a row lock when the database supports it."""
        stmt = select(Workspace).where(Workspace.id == workspace_id)
        if self._dialect_name == "postgresql":
            stmt = stmt.with_for_update(of=Workspace)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def exists(self, workspace_id: str) -> bool:
        stmt = select(Workspace.id).where(Workspace.id == workspace_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def get_by_idempotency_key(self, key: str) -> Workspace | None:
        stmt = select(Workspace).where(Workspace.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def acquire_owned_path_conflict_lock(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> None:
        """Serialize owned-path admission for one repo/base transaction on Postgres."""
        if not any(_normalize_owned_path(path) != "" for path in owned_paths):
            return

        if self._dialect_name != "postgresql":
            return

        lock_key = _owned_path_conflict_advisory_lock_key(
            repo_url=repo_url,
            branch_base=branch_base,
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def find_active_owned_path_conflicts(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> list[OwnedPathConflict]:
        overlaps = await self.find_active_owned_path_overlaps(
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
        )
        return [
            OwnedPathConflict(
                workspace_id=overlap.workspace_id,
                existing_path=overlap.existing_path,
                requested_path=overlap.requested_path,
            )
            for overlap in overlaps
        ]

    async def find_active_owned_path_overlaps(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> list[OwnedPathOverlap]:
        requested_paths = [
            path for path in owned_paths if _normalize_owned_path(path) != ""
        ]
        if not requested_paths:
            return []

        stmt = (
            select(Workspace)
            .where(
                Workspace.repo_url == repo_url,
                Workspace.branch_base == branch_base,
                Workspace.status.in_(ACTIVE_OWNED_PATH_OVERLAP_STATUSES),
            )
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        overlaps: list[OwnedPathOverlap] = []
        for workspace in rows:
            for existing_path in workspace.owned_paths:
                if _normalize_owned_path(existing_path) == "":
                    continue
                for requested_path in requested_paths:
                    if _owned_paths_overlap(existing_path, requested_path):
                        overlaps.append(
                            OwnedPathOverlap(
                                workspace_id=workspace.id,
                                existing_path=existing_path,
                                requested_path=requested_path,
                            )
                        )
        return overlaps

    async def list(
        self,
        *,
        status: WorkspaceStatus | str | None = None,
        agent: AgentRuntime | str | None = None,
        repo_url: str | None = None,
        before_created_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        stmt = select(Workspace)
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if agent is not None:
            stmt = stmt.where(Workspace.agent == agent)
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        if before_created_at is not None and before_workspace_id is not None:
            stmt = stmt.where(
                or_(
                    Workspace.created_at < before_created_at,
                    and_(
                        Workspace.created_at == before_created_at,
                        Workspace.id < before_workspace_id,
                    ),
                )
            )
        stmt = (
            stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .options(selectinload(Workspace.operations))
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_without_task_attempts(
        self,
        *,
        status: WorkspaceStatus | str | None = None,
        agent: AgentRuntime | str | None = None,
        repo_url: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        stmt = (
            select(Workspace)
            .outerjoin(TaskAttempt, TaskAttempt.workspace_id == Workspace.id)
            .where(TaskAttempt.id.is_(None))
        )
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if agent is not None:
            stmt = stmt.where(Workspace.agent == agent)
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        stmt = stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_merge_queue(
        self,
        *,
        repo_url: str | None = None,
        base_branch: str | None = None,
        status: WorkspaceStatus | str | None = None,
        before_updated_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        stmt = (
            select(Workspace)
            .where(
                Workspace.pr_url.is_not(None),
                Workspace.pr_url != "",
                ~Workspace.status.in_(
                    (
                        WorkspaceStatus.destroying.value,
                        WorkspaceStatus.destroyed.value,
                    )
                ),
            )
            .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        )
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        if base_branch is not None:
            stmt = stmt.where(Workspace.branch_base == base_branch)
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if before_updated_at is not None and before_workspace_id is not None:
            stmt = stmt.where(
                or_(
                    Workspace.updated_at < before_updated_at,
                    and_(
                        Workspace.updated_at == before_updated_at,
                        Workspace.id < before_workspace_id,
                    ),
                )
            )
        stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_merge_queue_without_candidates(
        self,
        *,
        repo_url: str | None = None,
        base_branch: str | None = None,
        status: WorkspaceStatus | str | None = None,
        before_updated_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        stmt = (
            select(Workspace)
            .outerjoin(MergeCandidate, MergeCandidate.workspace_id == Workspace.id)
            .where(
                MergeCandidate.id.is_(None),
                Workspace.pr_url.is_not(None),
                Workspace.pr_url != "",
                ~Workspace.status.in_(
                    (
                        WorkspaceStatus.destroying.value,
                        WorkspaceStatus.destroyed.value,
                    )
                ),
            )
            .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        )
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        if base_branch is not None:
            stmt = stmt.where(Workspace.branch_base == base_branch)
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if before_updated_at is not None and before_workspace_id is not None:
            stmt = stmt.where(
                or_(
                    Workspace.updated_at < before_updated_at,
                    and_(
                        Workspace.updated_at == before_updated_at,
                        Workspace.id < before_workspace_id,
                    ),
                )
            )
        stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_schedulable_ids(
        self,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
        after: tuple[datetime, str] | None = None,
    ) -> builtins.list[str]:
        """Return candidate workspace IDs for one worker poll.

        Postgres uses row-level locks with ``SKIP LOCKED`` to reduce duplicate
        candidates while poll transactions overlap. For provisioning and ready
        execution, the durable claim happens through ``transition_if_current()``.
        For monitor recovery, active claim leases are filtered out before
        limiting so claimed rows do not block later unclaimed rows.
        """
        if limit <= 0:
            return []

        candidates = await self._list_schedulable_candidates(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            after=after,
        )
        return [
            workspace.id
            for workspace in self._sort_schedulable_workspaces(candidates, limit)
        ]

    async def list_schedulable_workspaces(
        self,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
        after: tuple[datetime, str] | None = None,
    ) -> builtins.list[Workspace]:
        """Return ordered candidate workspaces for one worker poll."""
        if limit <= 0:
            return []

        candidates = await self._list_schedulable_candidates(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            after=after,
        )

        return self._sort_schedulable_workspaces(candidates, limit)

    async def _list_schedulable_candidates(
        self,
        *,
        status: WorkspaceStatus,
        limit: int | None,
        exclude_ids: set[str] | None = None,
        after: tuple[datetime, str] | None = None,
    ) -> builtins.list[Workspace]:
        stmt = _schedulable_workspace_ids_stmt(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            after=after,
            skip_locked=self._dialect_name == "postgresql",
            claim_cutoff=datetime.now(UTC) if status == WorkspaceStatus.monitoring_pr else None,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _sort_schedulable_workspaces(
        candidates: builtins.list[Workspace],
        limit: int | None,
    ) -> builtins.list[Workspace]:
        now = datetime.now(UTC)
        scored = sorted(
            (
                (scheduler_score_from_workspace(workspace, now=now), workspace)
                for workspace in candidates
            ),
            key=lambda item: scheduler_order_key(item[0]),
        )
        ordered = [workspace for _score, workspace in scored]
        return ordered if limit is None else ordered[:limit]

    async def transition(
        self,
        workspace: Workspace,
        *,
        to: WorkspaceStatus,
        reason_code: str,
        payload: dict[str, Any] | None = None,
    ) -> Workspace:
        """Move a workspace to the given status, recording an event.

        Validates the transition through ``WorkspaceStateMachine``. Bumps
        ``version`` for optimistic concurrency on downstream updates.
        """
        current = WorkspaceStatus(workspace.status)
        WorkspaceStateMachine.assert_transition(current, to)

        old_state = workspace.status
        workspace.status = to.value
        workspace.version += 1
        attempt = await TaskAttemptRepository(
            self._session,
            dialect_name=self._dialect_name,
        ).get_by_workspace_id(workspace.id)
        if attempt is not None:
            attempt.status = to.value
        if to == WorkspaceStatus.monitoring_pr and workspace.monitor_started_at is None:
            workspace.monitor_started_at = datetime.now(UTC)
        await self._sync_merge_candidate_lifecycle(workspace, attempt=attempt, to=to)
        if _releases_resource_reservation(to):
            await ResourceReservationRepository(self._session).release_active_for_workspace(
                workspace.id
            )

        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.state_changed",
                old_state=old_state,
                new_state=to.value,
                reason_code=reason_code,
                payload=payload,
            )
        )
        return workspace

    async def transition_if_current(
        self,
        workspace_id: str,
        *,
        from_status: WorkspaceStatus,
        to: WorkspaceStatus,
        reason_code: str,
        extra_conditions: tuple[ColumnElement[bool], ...] = (),
    ) -> Workspace | None:
        """Atomically transition a row only if it is still in ``from_status``."""
        WorkspaceStateMachine.assert_transition(from_status, to)

        now = datetime.now(UTC)
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == from_status.value,
                *extra_conditions,
            )
            .values(
                status=to.value,
                version=Workspace.version + 1,
                updated_at=now,
            )
            .returning(Workspace.id)
        )
        if result.scalar_one_or_none() is None:
            return None

        workspace = await self.get(workspace_id)
        if workspace is None:  # pragma: no cover - row was just updated in this txn
            return None

        attempt = await TaskAttemptRepository(
            self._session,
            dialect_name=self._dialect_name,
        ).get_by_workspace_id(workspace.id)
        if attempt is not None:
            attempt.status = to.value
        if to == WorkspaceStatus.monitoring_pr and workspace.monitor_started_at is None:
            workspace.monitor_started_at = now
        await self._sync_merge_candidate_lifecycle(workspace, attempt=attempt, to=to)
        if _releases_resource_reservation(to):
            await ResourceReservationRepository(self._session).release_active_for_workspace(
                workspace.id,
                released_at=now,
            )

        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.state_changed",
                old_state=from_status.value,
                new_state=to.value,
                reason_code=reason_code,
            )
        )
        await self._session.flush()
        return workspace

    async def _sync_merge_candidate_lifecycle(
        self,
        workspace: Workspace,
        *,
        attempt: TaskAttempt | None,
        to: WorkspaceStatus,
    ) -> None:
        candidate_repo = MergeCandidateRepository(self._session)
        if to == WorkspaceStatus.monitoring_pr:
            if attempt is None or not workspace.pr_url:
                return
            task = await TaskRepository(self._session).get(attempt.task_id)
            if task is None:  # pragma: no cover - FK invariant
                return
            await candidate_repo.make_attempt_canonical_and_create_candidate(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha=workspace.monitor_last_commit_sha,
                base_sha=workspace.base_commit,
            )
            return

        if to == WorkspaceStatus.completed:
            await candidate_repo.mark_workspace_merged(workspace.id)
            return

        if to in {WorkspaceStatus.failed, WorkspaceStatus.cancelled}:
            await candidate_repo.close_open_for_workspace(
                workspace.id,
                close_reason=_candidate_terminal_close_reason(to),
            )

    async def claim_monitoring_pr(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        now: datetime | None = None,
        clear_stale_execution_claim_cutoff: datetime | None = None,
    ) -> bool:
        """Claim a monitor-recovery workspace unless another lease is active."""
        cutoff = now or datetime.now(UTC)
        values: dict[str, Any] = {
            "monitor_claimed_by": owner_id,
            "monitor_claim_expires_at": lease_expires_at,
            "updated_at": Workspace.updated_at,
        }
        if clear_stale_execution_claim_cutoff is not None:
            stale_execution_claim = or_(
                Workspace.execution_claimed_by.is_(None),
                Workspace.execution_claim_expires_at.is_(None),
                Workspace.execution_claim_expires_at <= clear_stale_execution_claim_cutoff,
            )
            values.update(
                execution_claimed_by=case(
                    (stale_execution_claim, None),
                    else_=Workspace.execution_claimed_by,
                ),
                execution_claim_expires_at=case(
                    (stale_execution_claim, None),
                    else_=Workspace.execution_claim_expires_at,
                ),
            )
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == WorkspaceStatus.monitoring_pr.value,
                or_(
                    Workspace.monitor_claim_expires_at.is_(None),
                    Workspace.monitor_claim_expires_at <= cutoff,
                    Workspace.monitor_claimed_by == owner_id,
                ),
            )
            .values(**values)
            .returning(Workspace.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def refresh_monitoring_pr_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Extend this worker's active monitor-recovery lease."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == WorkspaceStatus.monitoring_pr.value,
                Workspace.monitor_claimed_by == owner_id,
            )
            .values(
                monitor_claim_expires_at=lease_expires_at,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def refresh_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Extend this worker's active-execution lease."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.execution_claimed_by == owner_id,
            )
            .values(
                execution_claim_expires_at=lease_expires_at,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def release_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        """Release this worker's active-execution lease, if it still owns it."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.execution_claimed_by == owner_id,
            )
            .values(
                execution_claimed_by=None,
                execution_claim_expires_at=None,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def release_monitoring_pr_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        """Release this worker's monitor-recovery lease, if it still owns it."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.monitor_claimed_by == owner_id,
            )
            .values(
                monitor_claimed_by=None,
                monitor_claim_expires_at=None,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def add_event(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkspaceEvent:
        events = await self.add_events(
            workspace,
            events=[
                WorkspaceEventCreate(
                    event_type=event_type,
                    reason_code=reason_code,
                    payload=payload,
                )
            ],
        )
        return events[0]

    async def add_audit_event(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        actor: str,
        action: str,
        outcome: str,
        reason_code: str,
        source: str | None = None,
        operation_id: str | None = None,
        operation_type: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        source_head_sha: str | None = None,
        source_base_sha: str | None = None,
        target_branch: str | None = None,
        remote_branch: str | None = None,
        branch_name: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> WorkspaceEvent:
        return await self.add_event(
            workspace,
            event_type=event_type,
            reason_code=reason_code,
            payload=build_audit_payload(
                actor=actor,
                source=source,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                operation_id=operation_id,
                operation_type=operation_type,
                pr_number=pr_number,
                pr_url=pr_url,
                source_head_sha=source_head_sha,
                source_base_sha=source_base_sha,
                target_branch=target_branch,
                remote_branch=remote_branch,
                branch_name=branch_name,
                evidence=evidence,
                extra=extra,
            ),
        )

    async def record_ignored_stale_callback(
        self,
        workspace: Workspace,
        *,
        callback_source: str,
        callback_action: str,
        expected_status: WorkspaceStatus | str,
        requested_status: WorkspaceStatus | str | None = None,
        operation_id: str | None = None,
        reason_code: str | None = None,
    ) -> WorkspaceEvent:
        expected_status_value = (
            expected_status.value
            if isinstance(expected_status, WorkspaceStatus)
            else expected_status
        )
        payload: dict[str, Any] = {
            "callback_source": callback_source,
            "callback_action": callback_action,
            "expected_status": expected_status_value,
            "actual_status": workspace.status,
        }
        if requested_status is not None:
            payload["requested_status"] = (
                requested_status.value
                if isinstance(requested_status, WorkspaceStatus)
                else requested_status
            )
        if operation_id is not None:
            payload["operation_id"] = operation_id
        if reason_code is not None:
            payload["reason_code"] = reason_code
        return await self.add_event(
            workspace,
            event_type="workspace.stale_callback_ignored",
            reason_code="STALE_CALLBACK_IGNORED",
            payload=payload,
        )

    async def add_events(
        self,
        workspace: Workspace,
        *,
        events: builtins.list[WorkspaceEventCreate],
    ) -> builtins.list[WorkspaceEvent]:
        created = [
            WorkspaceEvent(
                id=new_event_id(),
                workspace_id=workspace.id,
                event_type=event.event_type,
                old_state=workspace.status,
                new_state=workspace.status,
                reason_code=event.reason_code,
                payload=event.payload,
            )
            for event in events
        ]
        workspace.events.extend(created)
        await self._session.flush()
        return created


def _candidate_terminal_close_reason(status: WorkspaceStatus) -> str:
    if status == WorkspaceStatus.failed:
        return "WORKSPACE_FAILED"
    if status == WorkspaceStatus.cancelled:
        return "WORKSPACE_CANCELLED"
    return f"WORKSPACE_{status.value.upper()}"


def _releases_resource_reservation(status: WorkspaceStatus) -> bool:
    return status in {
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    }


def sync_candidate_readiness(
    candidate: MergeCandidate,
    *,
    workspace: Workspace,
    attempt: TaskAttempt,
    sync_validation_staleness: bool = True,
    recompute_stale: bool | None = None,
) -> None:
    from awf.runtime.merge_eligibility import (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        compute_stale_reason,
    )

    if recompute_stale is not None:
        sync_validation_staleness = recompute_stale

    workspace_status = WorkspaceStatus(workspace.status)
    is_open = candidate.status == "open"
    is_canonical = attempt.is_canonical_for_merge
    is_completed = candidate.status == "merged" or workspace_status == WorkspaceStatus.completed
    failed_or_cancelled = workspace_status in {
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
    }
    not_canonical = not is_canonical

    if sync_validation_staleness:
        scope_stale_reason = _initial_scope_stale_reason(workspace)
        stale_reason, _ = compute_stale_reason(workspace)
        # If there's an active stale reason, update it. If the reason clears, clear it.
        if scope_stale_reason is not None:
            candidate.stale = True
            candidate.stale_reason = scope_stale_reason
        elif stale_reason is not None:
            candidate.stale = True
            candidate.stale_reason = stale_reason
        elif candidate.stale_reason in (
            VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
            DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
        ):
            candidate.stale = False
            candidate.stale_reason = None

    candidate.completed = is_completed
    candidate.failed_or_cancelled = failed_or_cancelled
    candidate.not_canonical = not_canonical
    candidate.waiting_for_monitor = is_open and workspace_status == WorkspaceStatus.pushing
    candidate.manual_merge_required = (
        is_open
        and workspace_status == WorkspaceStatus.monitoring_pr
        and not workspace.auto_merge
        and is_canonical
        and not candidate.policy_blocked
        and not candidate.stale
    )
    candidate.ready = (
        is_open
        and workspace_status == WorkspaceStatus.monitoring_pr
        and workspace.auto_merge
        and is_canonical
        and not candidate.policy_blocked
        and not candidate.stale
    )
    if not is_open:
        candidate.ready = False
        candidate.waiting_for_monitor = False
        candidate.manual_merge_required = False
    if is_completed:
        candidate.ready = False
        candidate.waiting_for_monitor = False
        candidate.manual_merge_required = False
        candidate.failed_or_cancelled = False
        candidate.stale = False
        candidate.stale_reason = None


def _initial_scope_stale_reason(workspace: Workspace) -> str | None:
    if workspace.task_class != TaskClass.docs_task.value:
        return None
    if not _claims_non_docs_path(workspace.owned_paths):
        return None
    return DOCS_TASK_SCOPE_VIOLATION_STALE_REASON


def _claims_non_docs_path(owned_paths: list[str] | tuple[str, ...]) -> bool:
    for path in owned_paths:
        normalized = path.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized.startswith("docs/"):
            continue
        if normalized in {"README.md", "CHANGELOG.md", "CONTRIBUTING.md"}:
            continue
        if normalized.endswith((".md", ".mdx", ".rst")):
            continue
        return True
    return False


def _schedulable_workspace_ids_stmt(
    *,
    status: WorkspaceStatus,
    limit: int | None,
    exclude_ids: set[str] | None = None,
    after: tuple[datetime, str] | None = None,
    skip_locked: bool,
    claim_cutoff: datetime | None = None,
) -> Select[tuple[Workspace]]:
    stmt = select(Workspace).where(Workspace.status == status.value)
    if status == WorkspaceStatus.monitoring_pr and claim_cutoff is not None:
        stmt = stmt.where(
            or_(
                Workspace.monitor_claim_expires_at.is_(None),
                Workspace.monitor_claim_expires_at <= claim_cutoff,
            )
        )
    if exclude_ids:
        stmt = stmt.where(~Workspace.id.in_(sorted(exclude_ids)))
    if after is not None:
        after_created_at, after_id = after
        stmt = stmt.where(
            or_(
                Workspace.created_at > after_created_at,
                and_(
                    Workspace.created_at == after_created_at,
                    Workspace.id > after_id,
                ),
            )
        )
    stmt = stmt.order_by(Workspace.created_at.asc(), Workspace.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if skip_locked:
        stmt = stmt.with_for_update(skip_locked=True, of=Workspace)
    return stmt


def _owned_paths_overlap(left: str, right: str) -> bool:
    return _owned_path_overlap_match(left, right) is not None


def _owned_path_overlap_match(left: str, right: str) -> OwnedPathOverlapMatch | None:
    left_path = _normalize_owned_path(left)
    right_path = _normalize_owned_path(right)
    if left_path == "" or right_path == "":
        return None
    if left_path == right_path:
        return _owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
            match_reason_code=OWNED_PATH_EXACT_MATCH_REASON,
            explanation=f"Owned paths normalize to the same path: {left_path}.",
        )
    if _literal_paths_overlap(left_path, right_path):
        ancestor, descendant = (
            (left_path, right_path)
            if _is_descendant(left_path, right_path)
            else (right_path, left_path)
        )
        return _owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
            match_reason_code=OWNED_PATH_ANCESTOR_MATCH_REASON,
            explanation=f"One owned path contains the other: {ancestor} -> {descendant}.",
        )

    left_prefix = _wildcard_prefix(left_path)
    right_prefix = _wildcard_prefix(right_path)
    if left_prefix is not None and _wildcard_prefix_overlaps(left_prefix, right_path):
        return _wildcard_owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
        )
    if right_prefix is not None and _wildcard_prefix_overlaps(right_prefix, left_path):
        return _wildcard_owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
        )
    if (
        left_prefix is not None
        and right_prefix is not None
        and _wildcard_prefixes_overlap(left_prefix, right_prefix)
    ):
        return _wildcard_owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
        )
    return None


def _owned_path_match(
    left: str,
    right: str,
    *,
    normalized_left_path: str,
    normalized_right_path: str,
    match_reason_code: str,
    explanation: str,
) -> OwnedPathOverlapMatch:
    return OwnedPathOverlapMatch(
        left_path=left,
        right_path=right,
        normalized_left_path=normalized_left_path,
        normalized_right_path=normalized_right_path,
        match_reason_code=match_reason_code,
        explanation=explanation,
    )


def _wildcard_owned_path_match(
    left: str,
    right: str,
    *,
    normalized_left_path: str,
    normalized_right_path: str,
) -> OwnedPathOverlapMatch:
    return _owned_path_match(
        left,
        right,
        normalized_left_path=normalized_left_path,
        normalized_right_path=normalized_right_path,
        match_reason_code=OWNED_PATH_WILDCARD_MATCH_REASON,
        explanation=f"Wildcard owned-path prefixes overlap: {left} <-> {right}.",
    )


def _owned_path_conflict_advisory_lock_key(*, repo_url: str, branch_base: str) -> int:
    digest = hashlib.sha256(
        f"awf:owned-path-conflicts\x00{repo_url}\x00{branch_base}".encode()
    ).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _operation_idempotency_advisory_lock_key(key: str) -> int:
    digest = hashlib.sha256(f"awf:operation-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def owned_paths_overlap(left: str, right: str) -> bool:
    return _owned_paths_overlap(left, right)


def owned_path_overlap_match(left: str, right: str) -> OwnedPathOverlapMatch | None:
    return _owned_path_overlap_match(left, right)


def _normalize_owned_path(path: str) -> str:
    segments: list[str] = []
    for segment in path.strip().replace("\\", "/").split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/".join(segments)


def _literal_paths_overlap(left: str, right: str) -> bool:
    return left == right or _is_descendant(left, right) or _is_descendant(right, left)


def _is_descendant(parent: str, child: str) -> bool:
    return child.startswith(f"{parent.rstrip('/')}/")


def _wildcard_prefix(path: str) -> str | None:
    wildcard_indexes = [
        index for index in (path.find("*"), path.find("?"), path.find("[")) if index >= 0
    ]
    if not wildcard_indexes:
        return None
    return path[: min(wildcard_indexes)]


def _wildcard_prefix_overlaps(prefix: str, path: str) -> bool:
    if prefix == "":
        return True
    if path.startswith(prefix):
        return True
    return _literal_paths_overlap(prefix.rstrip("/"), path.rstrip("/"))


def _wildcard_prefixes_overlap(left: str, right: str) -> bool:
    if left == "" or right == "":
        return True
    if left.startswith(right) or right.startswith(left):
        return True
    return _literal_paths_overlap(left.rstrip("/"), right.rstrip("/"))


class WorkspaceEventRepository:
    """Read-only queries for immutable workspace events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        workspace_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[WorkspaceEvent]:
        stmt = select(WorkspaceEvent)
        if workspace_id is not None:
            stmt = stmt.where(WorkspaceEvent.workspace_id == workspace_id)
        if event_type is not None:
            stmt = stmt.where(WorkspaceEvent.event_type == event_type)
        stmt = stmt.order_by(
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.id.desc(),
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())


class CallbackIdempotencyConflictError(ValueError):
    """Raised when an idempotency key is replayed with a different request body."""


class CallbackSubscriptionRepository:
    """CRUD helpers for external callback registrations."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

    async def create_idempotent(
        self,
        *,
        name: str,
        target_url: str,
        event_types: list[str],
        enabled: bool,
        timeout_seconds: int,
        max_attempts: int,
        initial_backoff_seconds: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[CallbackSubscription, bool]:
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            return existing, False

        now = datetime.now(UTC)
        subscription_values: dict[str, Any] = {
            "id": new_callback_subscription_id(),
            "name": name,
            "target_url": target_url,
            "event_types": list(event_types),
            "enabled": enabled,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "initial_backoff_seconds": initial_backoff_seconds,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "created_at": now,
            "updated_at": now,
            "disabled_at": None if enabled else now,
        }
        insert_if_absent = _callback_subscription_insert_if_absent_stmt(self._dialect_name)
        if insert_if_absent is not None:
            result = await self._session.execute(insert_if_absent.values(**subscription_values))
            inserted_id = result.scalar_one_or_none()
            if inserted_id is not None:
                inserted = await self.get(inserted_id)
                if inserted is None:
                    raise RuntimeError("Inserted callback subscription could not be loaded.")
                return inserted, True

            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                raise RuntimeError(
                    "Callback subscription insert conflicted but no row could be loaded."
                )
            if existing.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            return existing, False

        subscription = CallbackSubscription(**subscription_values)
        self._session.add(subscription)
        await self._session.flush()
        return subscription, True

    async def get(self, subscription_id: str) -> CallbackSubscription | None:
        return await self._session.get(CallbackSubscription, subscription_id)

    async def get_by_idempotency_key(self, key: str) -> CallbackSubscription | None:
        stmt = select(CallbackSubscription).where(CallbackSubscription.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 50,
    ) -> builtins.list[CallbackSubscription]:
        stmt = select(CallbackSubscription)
        if enabled is not None:
            stmt = stmt.where(CallbackSubscription.enabled.is_(enabled))
        stmt = stmt.order_by(
            CallbackSubscription.created_at.desc(),
            CallbackSubscription.id.desc(),
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_enabled_for_event_type(
        self,
        event_type: str,
    ) -> builtins.list[CallbackSubscription]:
        event_type_candidates = _callback_subscription_event_type_candidates(event_type)
        if not event_type_candidates:
            return []

        stmt = (
            select(CallbackSubscription)
            .where(
                CallbackSubscription.enabled.is_(True),
                _callback_subscription_event_type_filter(
                    event_type_candidates,
                    self._dialect_name,
                ),
            )
            .order_by(CallbackSubscription.created_at.asc(), CallbackSubscription.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars())


class CallbackDeliveryRepository:
    """CRUD helpers for durable callback delivery records."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

    async def get(self, delivery_id: str) -> CallbackDelivery | None:
        stmt = (
            select(CallbackDelivery)
            .where(CallbackDelivery.id == delivery_id)
            .options(selectinload(CallbackDelivery.subscription))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def enqueue_once(
        self,
        *,
        subscription: CallbackSubscription,
        event_kind: CallbackEventKind | str,
        event_type: str,
        source_id: str,
        dedupe_key: str,
        workspace_id: str | None,
        operation_id: str | None,
        merge_candidate_id: str | None,
        envelope: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[CallbackDelivery, bool]:
        existing = await self.get_by_dedupe_key(
            subscription_id=subscription.id,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return existing, False

        created_at = now or datetime.now(UTC)
        delivery_id = new_callback_delivery_id()
        event_kind_value = event_kind.value if isinstance(event_kind, CallbackEventKind) else event_kind
        idempotency_key = f"callback-delivery:{subscription.id}:{dedupe_key}"
        delivery_envelope = dict(envelope)
        delivery_envelope["delivery"] = {
            "id": delivery_id,
            "subscription_id": subscription.id,
            "idempotency_key": idempotency_key,
            "dedupe_key": dedupe_key,
            "attempt_count": 0,
            "max_attempts": subscription.max_attempts,
        }
        delivery_values: dict[str, Any] = {
            "id": delivery_id,
            "subscription_id": subscription.id,
            "event_kind": event_kind_value,
            "event_type": event_type,
            "source_id": source_id,
            "dedupe_key": dedupe_key,
            "workspace_id": workspace_id,
            "operation_id": operation_id,
            "merge_candidate_id": merge_candidate_id,
            "envelope": delivery_envelope,
            "idempotency_key": idempotency_key,
            "status": CallbackDeliveryStatus.pending.value,
            "attempt_count": 0,
            "max_attempts": subscription.max_attempts,
            "next_attempt_at": created_at,
        }
        insert_if_absent = _callback_delivery_insert_if_absent_stmt(self._dialect_name)
        if insert_if_absent is not None:
            result = await self._session.execute(insert_if_absent.values(**delivery_values))
            inserted_id = result.scalar_one_or_none()
            if inserted_id is not None:
                inserted = await self.get(inserted_id)
                if inserted is None:
                    raise RuntimeError("Inserted callback delivery could not be loaded.")
                return inserted, True

            existing = await self.get_by_dedupe_key(
                subscription_id=subscription.id,
                dedupe_key=dedupe_key,
            )
            if existing is None:
                raise RuntimeError(
                    "Callback delivery insert conflicted but no row could be loaded."
                )
            return existing, False

        delivery = CallbackDelivery(**delivery_values)
        self._session.add(delivery)
        await self._session.flush()
        return delivery, True

    async def get_by_dedupe_key(
        self,
        *,
        subscription_id: str,
        dedupe_key: str,
    ) -> CallbackDelivery | None:
        stmt = select(CallbackDelivery).where(
            CallbackDelivery.subscription_id == subscription_id,
            CallbackDelivery.dedupe_key == dedupe_key,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[CallbackDelivery]:
        due_at = now or datetime.now(UTC)
        stmt = (
            select(CallbackDelivery)
            .where(
                CallbackDelivery.status == CallbackDeliveryStatus.pending.value,
                or_(
                    CallbackDelivery.next_attempt_at.is_(None),
                    CallbackDelivery.next_attempt_at <= due_at,
                ),
            )
            .options(selectinload(CallbackDelivery.subscription))
            .order_by(CallbackDelivery.created_at.asc(), CallbackDelivery.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def mark_attempt_started(
        self,
        delivery: CallbackDelivery,
        *,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        delivery.status = CallbackDeliveryStatus.running.value
        delivery.attempt_count += 1
        delivery.last_attempt_at = now or datetime.now(UTC)
        delivery.next_attempt_at = None
        delivery.response_status_code = None
        delivery.error_code = None
        delivery.error_message = None
        await self._session.flush()
        return delivery

    async def sync_envelope_delivery_metadata(
        self,
        delivery: CallbackDelivery,
    ) -> CallbackDelivery:
        envelope = dict(delivery.envelope)
        delivery_metadata = dict(envelope.get("delivery", {}))
        delivery_metadata.update(
            {
                "id": delivery.id,
                "subscription_id": delivery.subscription_id,
                "idempotency_key": delivery.idempotency_key,
                "dedupe_key": delivery.dedupe_key,
                "attempt_count": delivery.attempt_count,
                "max_attempts": delivery.max_attempts,
            }
        )
        envelope["delivery"] = delivery_metadata
        delivery.envelope = envelope
        await self._session.flush()
        return delivery

    async def mark_succeeded(
        self,
        delivery: CallbackDelivery,
        *,
        response_status_code: int,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        delivered_at = now or datetime.now(UTC)
        delivery.status = CallbackDeliveryStatus.succeeded.value
        delivery.delivered_at = delivered_at
        delivery.next_attempt_at = None
        delivery.response_status_code = response_status_code
        delivery.error_code = None
        delivery.error_message = None
        await self._session.flush()
        return delivery

    async def mark_failed_or_retry(
        self,
        delivery: CallbackDelivery,
        *,
        error_code: str,
        error_message: str,
        response_status_code: int | None,
        backoff_seconds: int,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        attempted_at = now or datetime.now(UTC)
        delivery.response_status_code = response_status_code
        delivery.error_code = error_code
        delivery.error_message = error_message[:512]
        if delivery.attempt_count >= delivery.max_attempts:
            delivery.status = CallbackDeliveryStatus.failed.value
            delivery.next_attempt_at = None
        else:
            delivery.status = CallbackDeliveryStatus.pending.value
            delivery.next_attempt_at = attempted_at + timedelta(seconds=backoff_seconds)
        await self._session.flush()
        return delivery

    async def mark_skipped(
        self,
        delivery: CallbackDelivery,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        skipped_at = now or datetime.now(UTC)
        delivery.status = CallbackDeliveryStatus.skipped.value
        delivery.last_attempt_at = skipped_at
        delivery.next_attempt_at = None
        delivery.error_code = error_code
        delivery.error_message = error_message[:512]
        await self._session.flush()
        return delivery


class OperationRepository:
    """CRUD helpers for async control-plane operations."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = _resolve_session_dialect_name(session, dialect_name)

    async def acquire_idempotency_key_lock(self, key: str) -> None:
        """Serialize operation idempotency decisions for one key on Postgres."""
        if self._dialect_name != "postgresql":
            return

        lock_key = _operation_idempotency_advisory_lock_key(key)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def create(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        status_value = status.value if isinstance(status, OperationStatus) else status
        operation = Operation(
            id=new_operation_id(),
            workspace_id=workspace_id,
            type=operation_type.value
            if isinstance(operation_type, OperationType)
            else operation_type,
            status=status_value,
            payload=payload,
            idempotency_key=idempotency_key,
            started_at=datetime.now(UTC) if status_value == OperationStatus.running.value else None,
        )
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def create_idempotent(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> tuple[Operation, bool]:
        await self.acquire_idempotency_key_lock(idempotency_key)
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing, False
        return (
            await self.create(
                workspace_id=workspace_id,
                operation_type=operation_type,
                status=status,
                payload=payload,
                idempotency_key=idempotency_key,
            ),
            True,
        )

    async def get(self, operation_id: str) -> Operation | None:
        return await self._session.get(Operation, operation_id)

    async def start(self, operation: Operation) -> Operation:
        operation.status = OperationStatus.running.value
        if operation.started_at is None:
            operation.started_at = datetime.now(UTC)
        await self._session.flush()
        return operation

    async def get_by_idempotency_key(self, key: str) -> Operation | None:
        stmt = (
            select(Operation)
            .where(Operation.idempotency_key == key)
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_active_matching_payload(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        payload_identity: Mapping[str, Any],
        limit: int = 100,
    ) -> Operation | None:
        operation_type_value = (
            operation_type.value if isinstance(operation_type, OperationType) else operation_type
        )
        stmt = (
            select(Operation)
            .where(
                Operation.workspace_id == workspace_id,
                Operation.type == operation_type_value,
                Operation.status.in_(
                    (
                        OperationStatus.pending.value,
                        OperationStatus.running.value,
                    )
                ),
            )
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(limit)
        )
        for operation in (await self._session.execute(stmt)).scalars():
            payload = operation.payload
            if not isinstance(payload, dict):
                continue
            if all(
                key in payload and payload[key] == value
                for key, value in payload_identity.items()
            ):
                return operation
        return None

    async def list_all(
        self,
        *,
        workspace_id: str | None = None,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        stmt = select(Operation)
        status_value = status.value if isinstance(status, OperationStatus) else status
        operation_type_value = (
            operation_type.value if isinstance(operation_type, OperationType) else operation_type
        )
        if workspace_id is not None:
            stmt = stmt.where(Operation.workspace_id == workspace_id)
        if status_value is not None:
            stmt = stmt.where(Operation.status == status_value)
        if operation_type_value is not None:
            stmt = stmt.where(Operation.type == operation_type_value)

        stmt = stmt.order_by(Operation.created_at.desc(), Operation.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        return await self.list_all(
            workspace_id=workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
        )

    async def finish(
        self,
        operation: Operation,
        *,
        status: OperationStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_stream_refs: Mapping[str, Any] | None = None,
    ) -> Operation:
        operation.status = status.value
        operation.result = _operation_result_with_log_stream_refs(
            result,
            log_stream_refs=log_stream_refs,
        )
        operation.error_code = error_code
        operation.error_message = error_message
        operation.finished_at = datetime.now(UTC)
        if operation.started_at is None:
            operation.started_at = operation.finished_at
        await self._session.flush()
        return operation


def _operation_result_with_log_stream_refs(
    result: dict[str, Any] | None,
    *,
    log_stream_refs: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if log_stream_refs is None:
        return result
    merged_result = dict(result or {})
    existing_refs = merged_result.get("log_stream_refs")
    merged_refs: dict[str, Any] = {}
    if isinstance(existing_refs, Mapping):
        merged_refs.update(existing_refs)
    merged_refs.update(dict(log_stream_refs))
    merged_result["log_stream_refs"] = merged_refs
    return merged_result


class WorkspaceLogStreamRepository:
    """Metadata index for durable workspace log streams."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        name: str,
        kind: str,
        path: str,
    ) -> WorkspaceLogStream:
        existing = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if existing is not None:
            return existing
        stream = WorkspaceLogStream(
            id=new_log_stream_id(),
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            name=name,
            kind=kind,
            path=path,
            byte_count=0,
            line_count=0,
        )
        self._session.add(stream)
        await self._session.flush()
        return stream

    async def get(self, *, workspace_id: str, stream_id: str) -> WorkspaceLogStream | None:
        stmt = select(WorkspaceLogStream).where(
            WorkspaceLogStream.workspace_id == workspace_id,
            WorkspaceLogStream.stream_id == stream_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: str) -> list[WorkspaceLogStream]:
        stmt = (
            select(WorkspaceLogStream)
            .where(WorkspaceLogStream.workspace_id == workspace_id)
            .order_by(WorkspaceLogStream.opened_at, WorkspaceLogStream.stream_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_validation_for_workspace(self, workspace_id: str) -> list[WorkspaceLogStream]:
        stmt = (
            select(WorkspaceLogStream)
            .where(
                WorkspaceLogStream.workspace_id == workspace_id,
                or_(
                    WorkspaceLogStream.source.in_(("validation", "setup")),
                    WorkspaceLogStream.stream_id.like("validation.%"),
                    WorkspaceLogStream.stream_id.like("setup.%"),
                ),
            )
            .order_by(WorkspaceLogStream.opened_at, WorkspaceLogStream.stream_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def append_metadata(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        byte_delta: int,
        line_delta: int,
    ) -> WorkspaceLogStream | None:
        stream = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if stream is None:
            return None
        if byte_delta == 0 and line_delta == 0:
            return stream
        if stream.closed_at is not None:
            stream.closed_at = None
        stream.byte_count += byte_delta
        stream.line_count += line_delta
        await self._session.flush()
        return stream

    async def close(self, *, workspace_id: str, stream_id: str) -> WorkspaceLogStream | None:
        stream = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if stream is None:
            return None
        if stream.closed_at is None:
            stream.closed_at = datetime.now(UTC)
        await self._session.flush()
        return stream
