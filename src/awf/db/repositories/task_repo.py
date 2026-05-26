"""Task and TaskAttempt database repositories."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from awf.common.ids import (
    new_task_attempt_id,
    new_task_id,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Task, TaskAttempt, Workspace
from awf.db.repositories.base import (
    resolve_session_dialect_name,
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
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

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

    async def get_by_workspace_ids(self, workspace_ids: Iterable[str]) -> dict[str, TaskAttempt]:
        unique_workspace_ids = tuple(dict.fromkeys(workspace_ids))
        if not unique_workspace_ids:
            return {}

        stmt = select(TaskAttempt).where(TaskAttempt.workspace_id.in_(unique_workspace_ids))
        return {
            attempt.workspace_id: attempt
            for attempt in (await self._session.execute(stmt)).scalars()
        }

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
