"""Task and task-attempt persistence tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import TaskAttempt
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    async with factory() as s:
        yield s

    await engine.dispose()


async def _workspace(
    session: AsyncSession,
    *,
    title: str,
    repo_url: str = "git@github.com:example/app.git",
    branch_base: str = "development",
) -> object:
    workspace = await WorkspaceRepository(session).create(
        repo_url=repo_url,
        branch_base=branch_base,
        task_title=title,
        task_prompt="Do the work.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    await session.flush()
    return workspace


async def _task(session: AsyncSession, *, external_id: str = "TICKET-LINEAGE") -> object:
    from awf.db.repositories import TaskRepository

    return await TaskRepository(session).create_or_get(
        repo_url="git@github.com:example/app.git",
        base_branch="development",
        title="Merge lineage task",
        prompt="Do the work.",
        external_id=external_id,
        idempotency_key=None,
        task_class="test_task",
        owned_paths=["src/awf/**"],
    )


async def _drive_to_monitoring_pr(
    session: AsyncSession,
    workspace: object,
    *,
    pr_number: int,
    branch_name: str,
) -> None:
    repo = WorkspaceRepository(session)
    for target in (
        WorkspaceStatus.provisioning,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
    ):
        await repo.transition(workspace, to=target, reason_code="TEST")
    workspace.branch_name = branch_name
    workspace.remote_push_branch = branch_name
    workspace.base_commit = "a" * 40
    workspace.pr_url = f"https://github.com/example/app/pull/{pr_number}"
    workspace.pr_number = pr_number
    await repo.transition(workspace, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED")


class TestTaskAttemptRepository:
    @pytest.mark.unit
    def test_attempt_number_sequence_lock_uses_postgres_for_update(self) -> None:
        from awf.db.repositories import TaskAttemptRepository

        stmt = TaskAttemptRepository._attempt_number_sequence_lock_stmt("task-123")
        compiled = str(stmt.compile(dialect=postgresql.dialect()))

        assert "FROM tasks" in compiled
        assert "FOR UPDATE" in compiled

    @pytest.mark.unit
    async def test_create_or_get_task_reuses_external_id(self, session: AsyncSession) -> None:
        from awf.db.repositories import TaskRepository

        repo = TaskRepository(session)

        first = await repo.create_or_get(
            repo_url="git@github.com:example/app.git",
            base_branch="development",
            title="First title",
            prompt="First prompt",
            external_id="TICKET-123",
            idempotency_key=None,
            task_class="refactor_task",
            owned_paths=["src/awf/**"],
        )
        second = await repo.create_or_get(
            repo_url="git@github.com:example/app.git",
            base_branch="development",
            title="Updated title should not duplicate",
            prompt="Updated prompt",
            external_id="TICKET-123",
            idempotency_key=None,
            task_class="refactor_task",
            owned_paths=["src/awf/**"],
        )

        assert second.id == first.id
        assert second.external_id == "TICKET-123"
        assert second.repo_url == "git@github.com:example/app.git"
        assert second.base_branch == "development"

    @pytest.mark.unit
    async def test_attempt_numbers_increment_for_same_task(
        self,
        session: AsyncSession,
    ) -> None:
        from awf.db.repositories import TaskAttemptRepository, TaskRepository

        task = await TaskRepository(session).create_or_get(
            repo_url="git@github.com:example/app.git",
            base_branch="development",
            title="Retryable task",
            prompt="Do the work.",
            external_id="TICKET-RETRY",
            idempotency_key=None,
            task_class=None,
            owned_paths=[],
        )
        first_workspace = await _workspace(session, title="first attempt")
        second_workspace = await _workspace(session, title="second attempt")

        repo = TaskAttemptRepository(session)
        first_attempt = await repo.create_for_workspace(task=task, workspace=first_workspace)
        second_attempt = await repo.create_for_workspace(task=task, workspace=second_workspace)

        assert first_attempt.attempt_number == 1
        assert first_attempt.workspace_id == first_workspace.id
        assert first_attempt.status == WorkspaceStatus.requested.value
        assert second_attempt.attempt_number == 2
        assert second_attempt.workspace_id == second_workspace.id
        assert second_attempt.agent == AgentRuntime.codex.value

    @pytest.mark.unit
    async def test_database_allows_only_one_canonical_attempt_per_task(
        self,
        session: AsyncSession,
    ) -> None:
        from awf.db.repositories import TaskAttemptRepository

        task = await _task(session)
        first_workspace = await _workspace(session, title="first attempt")
        second_workspace = await _workspace(session, title="second attempt")

        attempt_repo = TaskAttemptRepository(session)
        first_attempt = await attempt_repo.create_for_workspace(
            task=task,
            workspace=first_workspace,
        )
        second_attempt = await attempt_repo.create_for_workspace(
            task=task,
            workspace=second_workspace,
        )

        first_attempt.is_canonical_for_merge = True
        second_attempt.is_canonical_for_merge = True

        with pytest.raises(IntegrityError):
            await session.flush()

    @pytest.mark.unit
    async def test_retry_pr_ready_attempt_supersedes_canonical_and_closes_old_candidate(
        self,
        session: AsyncSession,
    ) -> None:
        from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository

        task = await _task(session, external_id="TICKET-SUPERSEDE")
        first_workspace = await _workspace(session, title="first attempt")
        second_workspace = await _workspace(session, title="retry attempt")

        attempt_repo = TaskAttemptRepository(session)
        first_attempt = await attempt_repo.create_for_workspace(
            task=task,
            workspace=first_workspace,
        )
        second_attempt = await attempt_repo.create_for_workspace(
            task=task,
            workspace=second_workspace,
            parent_attempt_id=first_attempt.id,
            redispatch_from_attempt_id=first_attempt.id,
        )

        await _drive_to_monitoring_pr(
            session,
            first_workspace,
            pr_number=11,
            branch_name="awf/first-attempt",
        )
        first_candidate = await MergeCandidateRepository(session).get_by_attempt_id(
            first_attempt.id
        )
        assert first_candidate is not None
        assert first_candidate.status == "open"

        await _drive_to_monitoring_pr(
            session,
            second_workspace,
            pr_number=12,
            branch_name="awf/retry-attempt",
        )

        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )
        candidates = await MergeCandidateRepository(session).list_for_task(task.id, limit=10)

        assert [attempt.id for attempt in attempts] == [first_attempt.id, second_attempt.id]
        assert attempts[0].is_canonical_for_merge is False
        assert attempts[0].superseded_by_attempt_id == second_attempt.id
        assert attempts[1].is_canonical_for_merge is True
        assert attempts[1].parent_attempt_id == first_attempt.id
        assert attempts[1].redispatch_from_attempt_id == first_attempt.id

        first_candidate = next(
            candidate for candidate in candidates if candidate.attempt_id == first_attempt.id
        )
        second_candidate = next(
            candidate for candidate in candidates if candidate.attempt_id == second_attempt.id
        )
        assert first_candidate.status == "closed"
        assert first_candidate.close_reason == "CANONICAL_CHANGED"
        assert first_candidate.not_canonical is True
        assert second_candidate.status == "open"
        assert second_candidate.close_reason is None


class TestTaskAttemptMigration:
    @pytest.mark.unit
    def test_task_attempt_migration_creates_tables(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        db_path = tmp_path / "awf.db"
        env = {
            **os.environ,
            "AWF_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        }

        monkeypatch.chdir(repo_root)
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            task_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            attempt_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(task_attempts)").fetchall()
            }

        assert "tasks" in tables
        assert "task_attempts" in tables
        assert {"id", "external_id", "repo_url", "base_branch", "title"} <= task_columns
        assert {
            "id",
            "task_id",
            "workspace_id",
            "attempt_number",
            "agent",
            "status",
        } <= attempt_columns
