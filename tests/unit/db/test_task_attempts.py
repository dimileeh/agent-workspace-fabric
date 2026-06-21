"""Task and task-attempt persistence tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import TaskAttempt
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine
from tests.postgres import (
    postgres_alembic_subprocess_lock,
    postgres_empty_test_url,
    postgres_test_session,
)


def _run_alembic(repo_root: Path, env: dict[str, str], *args: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(
            "alembic command failed: "
            f"{' '.join(args)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            pytrace=False,
        )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


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
            title="First title",
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
    async def test_create_or_get_task_rejects_external_id_scope_collision(
        self, session: AsyncSession
    ) -> None:
        from awf.db.repositories import TaskExternalIdConflictError, TaskRepository

        repo = TaskRepository(session)

        await repo.create_or_get(
            repo_url="git@github.com:example/app.git",
            base_branch="development",
            title="Docs prompt",
            prompt="First prompt",
            external_id="WAVE-1",
            idempotency_key=None,
            task_class="docs_task",
            owned_paths=["docs/**"],
        )

        with pytest.raises(TaskExternalIdConflictError) as excinfo:
            await repo.create_or_get(
                repo_url="git@github.com:example/app.git",
                base_branch="development",
                title="API prompt",
                prompt="Second prompt",
                external_id="WAVE-1",
                idempotency_key=None,
                task_class="docs_task",
                owned_paths=["src/awf/api/**"],
            )

        assert excinfo.value.external_id == "WAVE-1"
        assert "already belongs to a different task scope" in str(excinfo.value)

    @pytest.mark.unit
    async def test_create_or_get_task_rejects_external_id_owned_paths_collision(
        self, session: AsyncSession
    ) -> None:
        from awf.db.repositories import TaskExternalIdConflictError, TaskRepository

        repo = TaskRepository(session)

        await repo.create_or_get(
            repo_url="git@github.com:example/app.git",
            base_branch="development",
            title="docs(onboarding): add prompts",
            prompt="First prompt",
            external_id="WAVE-1",
            idempotency_key=None,
            task_class="docs_task",
            owned_paths=["docs/**", "README.md"],
        )

        with pytest.raises(TaskExternalIdConflictError) as excinfo:
            await repo.create_or_get(
                repo_url="git@github.com:example/app.git",
                base_branch="development",
                title="docs(onboarding): add prompts",
                prompt="Second prompt",
                external_id="WAVE-1",
                idempotency_key=None,
                task_class="docs_task",
                owned_paths=["src/**"],
            )

        assert excinfo.value.external_id == "WAVE-1"
        assert "already belongs to a different task scope" in str(excinfo.value)

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
    async def test_mark_canonical_for_merge_returns_previous_canonical_attempt(
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

        initial_previous = await attempt_repo.mark_canonical_for_merge(first_attempt)
        superseded_previous = await attempt_repo.mark_canonical_for_merge(second_attempt)

        assert initial_previous is None
        assert superseded_previous is not None
        assert superseded_previous.id == first_attempt.id
        assert first_attempt.is_canonical_for_merge is False
        assert first_attempt.superseded_by_attempt_id == second_attempt.id
        assert second_attempt.is_canonical_for_merge is True

    @pytest.mark.unit
    async def test_inplace_retry_keeps_single_canonical_candidate(
        self,
        session: AsyncSession,
    ) -> None:
        """An in-place provider retry keeps the SAME workspace + attempt id.

        Because the auto-retry reuses the running workspace (recovering → running)
        instead of forking a fresh relaunch, there is no retry-lineage to break
        canonicalization (#609). When the single workspace later reaches
        ``monitoring_pr`` its merge candidate is canonical and auto-merge fires —
        the exact continuity #612's in-place retry guarantees, contrasted with
        ``test_retry_pr_ready_attempt_supersedes_canonical_and_closes_old_candidate``
        (fresh relaunch → superseded canonical → closed candidate).
        """
        from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository

        task = await _task(session, external_id="TICKET-INPLACE")
        workspace = await _workspace(session, title="in-place retry")
        workspace.auto_merge = True

        attempt_repo = TaskAttemptRepository(session)
        attempt = await attempt_repo.create_for_workspace(
            task=task,
            workspace=workspace,
        )

        # Drive the SAME workspace through an in-place recovering hop: the provider
        # stalled mid-run, the workspace paused (recovering) holding its warm stack,
        # then resumed in place (recovering → running) — no fresh relaunch, no
        # new attempt — before proceeding to the PR.
        repo = WorkspaceRepository(session)
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.recovering,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ):
            await repo.transition(workspace, to=target, reason_code="TEST")
        workspace.branch_name = "awf/in-place"
        workspace.remote_push_branch = "awf/in-place"
        workspace.base_commit = "a" * 40
        workspace.pr_url = "https://github.com/example/app/pull/21"
        workspace.pr_number = 21
        await repo.transition(workspace, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED")

        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace.id)
        assert candidate is not None
        assert candidate.status == "open"
        assert candidate.attempt_id == attempt.id
        assert candidate.workspace_id == workspace.id

        attempts = list(
            (
                await session.execute(select(TaskAttempt).where(TaskAttempt.task_id == task.id))
            ).scalars()
        )
        # The in-place retry did NOT fork the lineage: exactly one attempt exists.
        assert [item.id for item in attempts] == [attempt.id]
        assert attempt.is_canonical_for_merge is True
        assert attempt.parent_attempt_id is None
        assert attempt.redispatch_from_attempt_id is None
        assert candidate.not_canonical is False
        # Auto-merge fires: monitoring_pr + auto_merge + canonical + not blocked/stale.
        assert candidate.ready is True

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
        queued_candidates = await MergeCandidateRepository(session).list_queue(limit=10)

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
        assert [candidate.attempt_id for candidate in queued_candidates] == [second_attempt.id]


class TestTaskAttemptMigration:
    @pytest.mark.unit
    async def test_task_attempt_migration_creates_tables(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        async with postgres_empty_test_url() as database_url:
            env = {
                **os.environ,
                "AWF_DATABASE_URL": database_url,
            }

            monkeypatch.chdir(repo_root)
            with postgres_alembic_subprocess_lock(database_url):
                try:
                    _run_alembic(repo_root, env, "upgrade", "head")
                except subprocess.TimeoutExpired as exc:
                    pytest.fail(f"Alembic upgrade timed out after {exc.timeout} seconds")

            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    tables = set(
                        await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
                    )
                    task_columns = set(
                        await conn.run_sync(
                            lambda sync_conn: [
                                column["name"] for column in inspect(sync_conn).get_columns("tasks")
                            ]
                        )
                    )
                    attempt_columns = set(
                        await conn.run_sync(
                            lambda sync_conn: [
                                column["name"]
                                for column in inspect(sync_conn).get_columns("task_attempts")
                            ]
                        )
                    )
            finally:
                await engine.dispose()

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
