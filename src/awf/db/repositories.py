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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from awf.common.ids import (
    new_event_id,
    new_log_stream_id,
    new_operation_id,
    new_task_attempt_id,
    new_task_id,
    new_workspace_id,
)
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import (
    Operation,
    Task,
    TaskAttempt,
    Workspace,
    WorkspaceEvent,
    WorkspaceLogStream,
)

ACTIVE_OWNED_PATH_CONFLICT_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.requested.value,
    WorkspaceStatus.provisioning.value,
    WorkspaceStatus.ready.value,
    WorkspaceStatus.running.value,
    WorkspaceStatus.validating.value,
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
)


@dataclass(frozen=True)
class OwnedPathConflict:
    workspace_id: str
    existing_path: str
    requested_path: str


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
    ) -> TaskAttempt:
        await self._lock_attempt_number_sequence(task.id)
        max_attempt_number = (
            await self._session.execute(
                select(func.max(TaskAttempt.attempt_number)).where(
                    TaskAttempt.task_id == task.id
                )
            )
        ).scalar_one()
        attempt_number = (max_attempt_number or 0) + 1
        attempt = TaskAttempt(
            id=new_task_attempt_id(),
            task_id=task.id,
            workspace_id=workspace.id,
            attempt_number=attempt_number,
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
                    TaskAttempt.attempt_number
                    == latest_attempt_numbers.c.attempt_number,
                ),
            )
            .join(Workspace, TaskAttempt.workspace_id == Workspace.id)
            .options(
                selectinload(TaskAttempt.task),
                selectinload(TaskAttempt.workspace),
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
        auto_merge: bool = True,
        initial_review_grace_period_seconds: float | None = None,
        env_profile: str | None = None,
        profile_ref: str | None = None,
        requested_profile: dict[str, Any] | None = None,
        resolved_profile: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
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
            task_title=task_title,
            task_prompt=task_prompt,
            task_external_id=task_external_id,
            task_class=task_class,
            owned_paths=list(owned_paths or []),
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
                Workspace.status.in_(ACTIVE_OWNED_PATH_CONFLICT_STATUSES),
            )
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        conflicts: list[OwnedPathConflict] = []
        for workspace in rows:
            for existing_path in workspace.owned_paths:
                if _normalize_owned_path(existing_path) == "":
                    continue
                for requested_path in requested_paths:
                    if _owned_paths_overlap(existing_path, requested_path):
                        conflicts.append(
                            OwnedPathConflict(
                                workspace_id=workspace.id,
                                existing_path=existing_path,
                                requested_path=requested_path,
                            )
                        )
        return conflicts

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
        execution, the durable claim happens when the dispatcher reloads each
        row with ``get_for_update()`` and performs the state transition in that
        same transaction.
        """
        if limit <= 0:
            return []

        stmt = _schedulable_workspace_ids_stmt(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            skip_locked=self._dialect_name == "postgresql",
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

    async def add_event(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkspaceEvent:
        event = WorkspaceEvent(
            id=new_event_id(),
            workspace_id=workspace.id,
            event_type=event_type,
            old_state=workspace.status,
            new_state=workspace.status,
            reason_code=reason_code,
            payload=payload,
        )
        workspace.events.append(event)
        await self._session.flush()
        return event


def _schedulable_workspace_ids_stmt(
    *,
    status: WorkspaceStatus,
    limit: int,
    exclude_ids: set[str] | None = None,
    skip_locked: bool,
) -> Select[tuple[str]]:
    stmt = select(Workspace.id).where(Workspace.status == status.value)
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

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
