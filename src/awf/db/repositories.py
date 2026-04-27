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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from awf.common.ids import (
    new_event_id,
    new_log_stream_id,
    new_merge_candidate_id,
    new_operation_id,
    new_policy_finding_id,
    new_queue_decision_id,
    new_resource_reservation_id,
    new_stale_reason_id,
    new_task_attempt_id,
    new_task_id,
    new_validation_run_id,
    new_workspace_id,
)
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import (
    MergeCandidate,
    Operation,
    PolicyFinding,
    QueueDecision,
    ResourceReservation,
    StaleReason,
    Task,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceLogStream,
)
from awf.runtime.merge_eligibility import DOCS_TASK_SCOPE_VIOLATION_STALE_REASON

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
class WorkspaceEventCreate:
    event_type: str
    reason_code: str | None = None
    payload: dict[str, Any] | None = None


def validation_command_set_hash(commands: list[dict[str, Any]]) -> str:
    """Stable hash for the configured command metadata in a validation run."""

    payload = json.dumps(commands, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
                selectinload(TaskAttempt.workspace),
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
                selectinload(TaskAttempt.workspace),
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
        decided_at: datetime | None = None,
    ) -> QueueDecision:
        row = QueueDecision(
            id=new_queue_decision_id(),
            workspace_id=workspace_id,
            task_id=task_id,
            attempt_id=attempt_id,
            decision=decision,
            reason_code=reason_code,
            class_priority=class_priority,
            computed_priority=computed_priority,
            age_boost=age_boost,
            retry_bonus=retry_bonus,
            resource_summary=dict(resource_summary),
            overlap_risk_summary=dict(overlap_risk_summary),
            decided_at=decided_at or datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return row

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
            target_branch=target_branch,
            target_head_sha=target_head_sha,
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
        await self._session.flush()
        return run

    async def update_target_head_sha(
        self,
        validation_run_id: str,
        *,
        target_head_sha: str | None,
    ) -> ValidationRun | None:
        run = await self.get(validation_run_id)
        if run is None:
            return None
        run.target_head_sha = target_head_sha
        await self._session.flush()
        return run

    async def list_for_workspace(self, workspace_id: str) -> builtins.list[ValidationRun]:
        stmt = (
            select(ValidationRun)
            .where(ValidationRun.workspace_id == workspace_id)
            .order_by(ValidationRun.started_at, ValidationRun.id)
        )
        return list((await self._session.execute(stmt)).scalars())

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

    async def list_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(workspace_id, status=None)

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
    ) -> builtins.list[StaleReason]:
        stmt = select(StaleReason).where(StaleReason.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(StaleReason.status == status)
        stmt = stmt.order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
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
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        stmt = select(Workspace)
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if agent is not None:
            stmt = stmt.where(Workspace.agent == agent)
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        stmt = stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc()).limit(limit)
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

        stmt = _schedulable_workspace_ids_stmt(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            skip_locked=self._dialect_name == "postgresql",
            claim_cutoff=datetime.now(UTC) if status == WorkspaceStatus.monitoring_pr else None,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def transition(
        self,
        workspace: Workspace,
        *,
        to: WorkspaceStatus,
        reason_code: str,
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
    ) -> bool:
        """Claim a monitor-recovery workspace unless another lease is active."""
        cutoff = now or datetime.now(UTC)
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
            .values(
                monitor_claimed_by=owner_id,
                monitor_claim_expires_at=lease_expires_at,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
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
    limit: int,
    exclude_ids: set[str] | None = None,
    skip_locked: bool,
    claim_cutoff: datetime | None = None,
) -> Select[tuple[str]]:
    stmt = select(Workspace.id).where(Workspace.status == status.value)
    if status == WorkspaceStatus.monitoring_pr and claim_cutoff is not None:
        stmt = stmt.where(
            or_(
                Workspace.monitor_claim_expires_at.is_(None),
                Workspace.monitor_claim_expires_at <= claim_cutoff,
            )
        )
    if exclude_ids:
        stmt = stmt.where(~Workspace.id.in_(sorted(exclude_ids)))
    stmt = stmt.order_by(Workspace.created_at.asc(), Workspace.id.asc()).limit(limit)
    if skip_locked:
        stmt = stmt.with_for_update(skip_locked=True, of=Workspace)
    return stmt


def _owned_paths_overlap(left: str, right: str) -> bool:
    left_path = _normalize_owned_path(left)
    right_path = _normalize_owned_path(right)
    if left_path == "" or right_path == "":
        return False
    if _literal_paths_overlap(left_path, right_path):
        return True

    left_prefix = _wildcard_prefix(left_path)
    right_prefix = _wildcard_prefix(right_path)
    if left_prefix is not None and _wildcard_prefix_overlaps(left_prefix, right_path):
        return True
    if right_prefix is not None and _wildcard_prefix_overlaps(right_prefix, left_path):
        return True
    if left_prefix is not None and right_prefix is not None:
        return _wildcard_prefixes_overlap(left_prefix, right_prefix)
    return False


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

    async def get(self, operation_id: str) -> Operation | None:
        return await self._session.get(Operation, operation_id)

    async def get_by_idempotency_key(self, key: str) -> Operation | None:
        stmt = (
            select(Operation)
            .where(Operation.idempotency_key == key)
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
    ) -> Operation:
        operation.status = status.value
        operation.result = result
        operation.error_code = error_code
        operation.error_message = error_message
        operation.finished_at = datetime.now(UTC)
        if operation.started_at is None:
            operation.started_at = operation.finished_at
        await self._session.flush()
        return operation


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
