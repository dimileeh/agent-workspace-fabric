"""Repository tests against isolated PostgreSQL schemas."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.state_machine import InvalidWorkspaceTransitionError
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import AgentRuntime, TaskClass, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace, WorkspaceEvent
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
    validation_command_set_hash,
)
from awf.db.session import make_engine, make_session_factory
from awf.service.scheduler import SchedulerOrderCursor
from tests.postgres import (
    create_postgres_test_engine,
    postgres_empty_test_url,
    postgres_test_session,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _create_policy_workspace(
    session: AsyncSession,
    repo: WorkspaceRepository,
    *,
    repo_url: str = "git@github.com:example/app.git",
    branch_base: str = "development",
    owned_paths: list[str] | None = None,
    status: WorkspaceStatus = WorkspaceStatus.requested,
) -> Workspace:
    workspace = await repo.create(
        repo_url=repo_url,
        branch_base=branch_base,
        task_title="policy test",
        task_prompt="do policy-sensitive work",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=list(owned_paths or []),
    )
    workspace.status = status.value
    await session.flush()
    return workspace


class _FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalarResult:
        return self

    def all(self) -> list[object]:
        return self._values

    def scalar_one_or_none(self) -> object | None:
        return self._values[0] if self._values else None


class _RecordingSchedulerSession:
    def __init__(self, dialect_name: str, values: list[object] | None = None) -> None:
        del dialect_name
        self.info: dict[str, object] = {}
        self.values = list(values or [])
        self.executed: list[object] = []

    async def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeScalarResult:
        del parameters
        self.executed.append(statement)
        return _FakeScalarResult(self.values)


def _recorded_workspace_row(
    workspace_id: str,
    *,
    status: WorkspaceStatus = WorkspaceStatus.requested,
) -> Workspace:
    queued_at = datetime(2026, 1, 1, tzinfo=UTC)
    return Workspace(
        id=workspace_id,
        status=status.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="scheduler row",
        task_prompt="p",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        created_at=queued_at,
        updated_at=queued_at,
        owned_paths=[],
        task_policy={},
    )


class TestCreate:
    @pytest.mark.unit
    async def test_create_returns_workspace_in_requested_state(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest"],
        )

        assert ws.status == WorkspaceStatus.requested.value
        assert ws.version == 1
        assert ws.id.startswith("ws_")
        assert ws.auto_merge is True
        assert ws.initial_review_grace_period_seconds is None
        assert ws.task_class is None
        assert ws.owned_paths == []

    @pytest.mark.unit
    async def test_create_persists_policy_metadata(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest"],
            task_class="migration_task",
            owned_paths=["migrations/**", "src/awf/db/**"],
        )

        assert ws.task_class == "migration_task"
        assert ws.owned_paths == ["migrations/**", "src/awf/db/**"]

    @pytest.mark.unit
    async def test_create_emits_creation_event(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

        # Re-read with events loaded.
        reloaded = await repo.get(ws.id)
        assert reloaded is not None
        events = reloaded.events
        assert len(events) == 1
        assert events[0].event_type == "workspace.created"
        assert events[0].new_state == WorkspaceStatus.requested.value
        assert events[0].reason_code == "CREATED"


class TestRelationshipLoading:
    @pytest.mark.unit
    async def test_list_does_not_eager_load_secret_leases(self) -> None:
        engine = await create_postgres_test_engine()

        factory = make_session_factory(engine)
        async with factory() as s:
            workspace = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="secret lease loading",
                task_prompt="avoid unrelated lease queries",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            task = await TaskRepository(s).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=None,
                owned_paths=[],
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await SecretLeaseRepository(s).issue_declared_leases(
                workspace,
                leases=[
                    SecretLeaseIssue(
                        secret_name="api-token",
                        kind="env",
                        target="API_TOKEN",
                        mode="ro",
                        required=True,
                        provider="env",
                        ref_digest="sha256:" + "a" * 64,
                        expires_at=None,
                        issue_metadata={},
                        attempt_id=attempt.id,
                    )
                ],
                now=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
            )
            await s.commit()

        statements: list[str] = []

        def record_sql(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            async with factory() as s:
                rows = await WorkspaceRepository(s).list()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
            await engine.dispose()

        assert len(rows) == 1
        assert not [
            statement for statement in statements if "from workspace_secret_leases" in statement
        ]

    @pytest.mark.unit
    async def test_list_does_not_eager_load_validation_runs(self) -> None:
        engine = await create_postgres_test_engine()

        factory = make_session_factory(engine)
        async with factory() as s:
            workspace = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="validation provenance",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            task = await TaskRepository(s).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=None,
                owned_paths=[],
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await ValidationRunRepository(s).start(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                tier=1,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
            )
            await s.commit()

        statements: list[str] = []

        def record_sql(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            async with factory() as s:
                rows = await WorkspaceRepository(s).list()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
            await engine.dispose()

        assert len(rows) == 1
        assert not [statement for statement in statements if "from validation_runs" in statement]


class TestValidationRunRepository:
    @pytest.mark.unit
    def test_validation_command_set_hash_ignores_evidence_annotations(self) -> None:
        executed = [
            {
                "phase": "coverage",
                "command": "pytest --cov=awf",
                "evidence_status": "executed",
                "evidence_reason_code": "VALIDATION_EXECUTED",
            }
        ]
        reused = [
            {
                "phase": "coverage",
                "command": "pytest --cov=awf",
                "evidence_status": "reused",
                "evidence_reason_code": "VALIDATION_EVIDENCE_REUSED",
            }
        ]

        assert validation_command_set_hash(executed) == validation_command_set_hash(reused)

    @pytest.mark.unit
    async def test_find_reusable_coverage_evidence_matches_exact_fresh_identity(self) -> None:
        engine = await create_postgres_test_engine()

        now = datetime(2026, 1, 2, tzinfo=UTC)
        commands = [{"phase": "coverage", "command": "pytest --cov=awf"}]
        async with make_session_factory(engine)() as s:
            workspace = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="coverage",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            repo = ValidationRunRepository(s)
            run = await repo.start(
                workspace_id=workspace.id,
                attempt_id=None,
                tier=1,
                commands=commands,
                base_commit="base",
                target_branch="main",
                target_head_sha=None,
                workspace_head_sha="head",
                resolved_profile_digest="profile",
                environment_identity_digest="env",
                log_stream_refs={},
                started_at=now,
            )
            await repo.finish(
                run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage={"status": "passed", "reason_code": "COVERAGE_OK", "percent": 99.0},
                finished_at=now,
            )
            run.log_stream_refs = {
                "coverage": {
                    "status": "failed",
                    "reason_code": "COVERAGE_BELOW_THRESHOLD",
                    "failing_test_node_ids": ["tests/unit/test_widget.py::test_failed"],
                }
            }
            await s.flush()

            reused = await repo.find_reusable_coverage_evidence(
                workspace_id=workspace.id,
                tier=1,
                commands=commands,
                workspace_head_sha="head",
                resolved_profile_digest="profile",
                environment_identity_digest="env",
                max_age_seconds=3600,
                now=now,
            )

        assert reused is not None
        assert reused.id == run.id

    @pytest.mark.unit
    async def test_find_reusable_coverage_evidence_rejects_pytest_failures(self) -> None:
        engine = await create_postgres_test_engine()

        now = datetime(2026, 1, 2, tzinfo=UTC)
        commands = [{"phase": "coverage", "command": "pytest --cov=awf"}]
        async with make_session_factory(engine)() as s:
            workspace = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="coverage",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            repo = ValidationRunRepository(s)
            run = await repo.start(
                workspace_id=workspace.id,
                attempt_id=None,
                tier=1,
                commands=commands,
                base_commit="base",
                target_branch="main",
                target_head_sha=None,
                workspace_head_sha="head",
                resolved_profile_digest="profile",
                environment_identity_digest="env",
                log_stream_refs={},
                started_at=now,
            )
            await repo.finish(
                run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage={
                    "status": "passed",
                    "reason_code": "COVERAGE_OK",
                    "percent": 99.0,
                    "failing_test_node_ids": [
                        "tests/unit/test_widget.py::test_handles_edges"
                    ],
                    "failing_test_evidence": [
                        "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"
                    ],
                },
                finished_at=now,
            )

            reused = await repo.find_reusable_coverage_evidence(
                workspace_id=workspace.id,
                tier=1,
                commands=commands,
                workspace_head_sha="head",
                resolved_profile_digest="profile",
                environment_identity_digest="env",
                max_age_seconds=3600,
                now=now,
            )

        assert reused is None

    @pytest.mark.unit
    async def test_find_reusable_coverage_evidence_rejects_changed_stale_or_failed_identity(
        self,
    ) -> None:
        engine = await create_postgres_test_engine()

        now = datetime(2026, 1, 2, tzinfo=UTC)
        old = datetime(2026, 1, 1, tzinfo=UTC)
        commands = [{"phase": "coverage", "command": "pytest --cov=awf"}]
        async with make_session_factory(engine)() as s:
            workspace = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="coverage",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            repo = ValidationRunRepository(s)
            stale = await repo.start(
                workspace_id=workspace.id,
                attempt_id=None,
                tier=1,
                commands=commands,
                base_commit="base",
                target_branch="main",
                target_head_sha=None,
                workspace_head_sha="head",
                resolved_profile_digest="profile",
                environment_identity_digest="env",
                log_stream_refs={},
                started_at=old,
            )
            await repo.finish(
                stale.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage={"status": "passed", "reason_code": "COVERAGE_OK", "percent": 99.0},
                finished_at=old,
            )
            failed = await repo.start(
                workspace_id=workspace.id,
                attempt_id=None,
                tier=1,
                commands=commands,
                base_commit="base",
                target_branch="main",
                target_head_sha=None,
                workspace_head_sha="failed-head",
                resolved_profile_digest="profile",
                environment_identity_digest="env",
                log_stream_refs={},
                started_at=now,
            )
            await repo.finish(
                failed.id,
                status="failed",
                reason_code="COVERAGE_BELOW_THRESHOLD",
                coverage={"status": "failed", "reason_code": "COVERAGE_BELOW_THRESHOLD"},
                finished_at=now,
            )

            assert (
                await repo.find_reusable_coverage_evidence(
                    workspace_id=workspace.id,
                    tier=1,
                    commands=commands,
                    workspace_head_sha="changed",
                    resolved_profile_digest="profile",
                    environment_identity_digest="env",
                    max_age_seconds=3600,
                    now=now,
                )
                is None
            )
            assert (
                await repo.find_reusable_coverage_evidence(
                    workspace_id=workspace.id,
                    tier=1,
                    commands=commands,
                    workspace_head_sha="head",
                    resolved_profile_digest="profile",
                    environment_identity_digest="env",
                    max_age_seconds=60,
                    now=now,
                )
                is None
            )
            assert (
                await repo.find_reusable_coverage_evidence(
                    workspace_id=workspace.id,
                    tier=1,
                    commands=commands,
                    workspace_head_sha="failed-head",
                    resolved_profile_digest="profile",
                    environment_identity_digest="env",
                    max_age_seconds=3600,
                    now=now,
                )
                is None
            )

    @pytest.mark.unit
    async def test_find_reusable_coverage_evidence_requires_workspace_head_sha(self) -> None:
        engine = await create_postgres_test_engine()

        async with make_session_factory(engine)() as s:
            repo = ValidationRunRepository(s)

            assert (
                await repo.find_reusable_coverage_evidence(
                    workspace_id="ws_missing_head",
                    tier=1,
                    commands=[{"phase": "coverage", "command": "pytest --cov=awf"}],
                    workspace_head_sha=None,
                    resolved_profile_digest="profile",
                    environment_identity_digest="env",
                    max_age_seconds=3600,
                )
                is None
            )

        await engine.dispose()

    @pytest.mark.unit
    async def test_list_by_workspace_ids_can_filter_by_status(
        self,
    ) -> None:
        engine = await create_postgres_test_engine()

        factory = make_session_factory(engine)
        async with factory() as s:
            ws_repo = WorkspaceRepository(s)
            workspace = await ws_repo.create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="filtered",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            second_workspace = await ws_repo.create(
                repo_url="git@github.com:example/b.git",
                branch_base="main",
                task_title="second",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            validation_repo = ValidationRunRepository(s)
            failed = await validation_repo.start(
                workspace_id=workspace.id,
                attempt_id=None,
                tier=3,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            await validation_repo.finish(
                failed.id,
                status="failed",
                reason_code="TESTS_FAILED",
                finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            )
            succeeded = await validation_repo.start(
                workspace_id=workspace.id,
                attempt_id=None,
                tier=2,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            await validation_repo.finish(
                succeeded.id,
                status="succeeded",
                reason_code=None,
                finished_at=datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
            )
            running = await validation_repo.start(
                workspace_id=second_workspace.id,
                attempt_id=None,
                tier=1,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 3, tzinfo=UTC),
            )
            workspace_id = workspace.id
            second_workspace_id = second_workspace.id
            succeeded_id = succeeded.id
            running_id = running.id
            await s.commit()

        statements: list[str] = []

        def record_sql(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            async with factory() as s:
                rows = await ValidationRunRepository(s).list_by_workspace_ids(
                    [workspace_id, second_workspace_id],
                    status="succeeded",
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
            await engine.dispose()

        assert [run.id for run in rows[workspace_id]] == [succeeded_id]
        assert rows[second_workspace_id] == []
        assert running_id not in {run.id for runs in rows.values() for run in runs}
        assert any("validation_runs.status =" in statement for statement in statements)

    @pytest.mark.unit
    async def test_latest_by_workspace_ids_uses_window_query(
        self,
    ) -> None:
        engine = await create_postgres_test_engine()

        factory = make_session_factory(engine)
        async with factory() as s:
            ws_repo = WorkspaceRepository(s)
            first_workspace = await ws_repo.create(
                repo_url="git@github.com:example/a.git",
                branch_base="main",
                task_title="first",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            second_workspace = await ws_repo.create(
                repo_url="git@github.com:example/b.git",
                branch_base="main",
                task_title="second",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            ignored_workspace = await ws_repo.create(
                repo_url="git@github.com:example/c.git",
                branch_base="main",
                task_title="ignored",
                task_prompt="run validation",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            validation_repo = ValidationRunRepository(s)
            await validation_repo.start(
                workspace_id=first_workspace.id,
                attempt_id=None,
                tier=1,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            latest_first = await validation_repo.start(
                workspace_id=first_workspace.id,
                attempt_id=None,
                tier=1,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            latest_second = await validation_repo.start(
                workspace_id=second_workspace.id,
                attempt_id=None,
                tier=1,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 3, tzinfo=UTC),
            )
            await validation_repo.start(
                workspace_id=ignored_workspace.id,
                attempt_id=None,
                tier=1,
                commands=[],
                base_commit=None,
                target_branch=None,
                target_head_sha=None,
                log_stream_refs={},
                started_at=datetime(2026, 1, 4, tzinfo=UTC),
            )
            first_workspace_id = first_workspace.id
            second_workspace_id = second_workspace.id
            latest_first_id = latest_first.id
            latest_second_id = latest_second.id
            await s.commit()

        statements: list[str] = []

        def record_sql(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            async with factory() as s:
                latest = await ValidationRunRepository(s).latest_by_workspace_ids(
                    [first_workspace_id, second_workspace_id, first_workspace_id]
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
            await engine.dispose()

        assert set(latest) == {first_workspace_id, second_workspace_id}
        assert latest[first_workspace_id].id == latest_first_id
        assert latest[second_workspace_id].id == latest_second_id
        assert len([obj for obj in latest.values() if isinstance(obj, ValidationRun)]) == 2
        assert any("row_number() over" in statement for statement in statements)


class TestMonitorPolicyMigration:
    @pytest.mark.unit
    async def test_monitor_policy_columns_backfill_existing_rows(
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

            def _alembic(*args: str) -> None:
                subprocess.run(
                    [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            monkeypatch.chdir(repo_root)
            _alembic("upgrade", "e5f6a1b2c3d4")

            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspaces (
                                id, status, version, repo_url, branch_base,
                                task_title, task_prompt, agent, test_commands,
                                requires_database, created_at, updated_at
                            )
                            VALUES (
                                'ws_old_policy', 'requested', 1, 'git@example.com:repo.git',
                                'development', 'old row', 'do work', 'codex', '[]'::json,
                                false, '2026-04-25 00:00:00', '2026-04-25 00:00:00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", "head")

            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    row = (
                        await conn.execute(
                            text(
                                """
                                SELECT auto_merge, initial_review_grace_period_seconds
                                FROM workspaces
                                WHERE id = 'ws_old_policy'
                                """
                            )
                        )
                    ).one()
            finally:
                await engine.dispose()

        assert row == (True, None)


class TestTaskPolicyMetadataMigration:
    @pytest.mark.unit
    async def test_policy_metadata_columns_backfill_existing_rows(
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

            def _alembic(*args: str) -> None:
                subprocess.run(
                    [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            monkeypatch.chdir(repo_root)
            _alembic("upgrade", "f6a1b2c3d4e5")

            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspaces (
                                id, status, version, repo_url, branch_base,
                                task_title, task_prompt, agent, test_commands,
                                requires_database, created_at, updated_at
                            )
                            VALUES (
                                'ws_old_policy_metadata', 'requested', 1,
                                'git@example.com:repo.git', 'development', 'old row',
                                'do work', 'codex', '[]'::json, false,
                                '2026-04-25 00:00:00', '2026-04-25 00:00:00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", "head")

            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    row = (
                        await conn.execute(
                            text(
                                """
                                SELECT task_class, owned_paths
                                FROM workspaces
                                WHERE id = 'ws_old_policy_metadata'
                                """
                            )
                        )
                    ).one()
            finally:
                await engine.dispose()

        assert row is not None
        assert row[0] is None
        assert row[1] == []


class TestIdempotency:
    @pytest.mark.unit
    async def test_get_by_idempotency_key_returns_existing(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            idempotency_key="same-key-abc",
        )
        await session.commit()

        found = await repo.get_by_idempotency_key("same-key-abc")
        assert found is not None
        assert found.id == ws.id

    @pytest.mark.unit
    async def test_get_by_idempotency_key_returns_none_for_unknown(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        assert await repo.get_by_idempotency_key("never-used") is None

    @pytest.mark.unit
    async def test_list_idempotency_key_family_returns_exact_and_generation_keys(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        logical_key = "adopt_%:key"
        for index, idempotency_key in enumerate(
            [
                logical_key,
                f"{logical_key}:g1",
                f"{logical_key}:g2",
                f"{logical_key}:retry",
                "adoptX%:key:g1",
                "adopt_%:other:g1",
            ],
            start=1,
        ):
            await repo.create(
                repo_url="git@github.com:example/a.git",
                branch_base="development",
                task_title=f"t {index}",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                idempotency_key=idempotency_key,
            )
        await session.commit()

        assert await repo.list_idempotency_key_family(logical_key) == [
            logical_key,
            f"{logical_key}:g1",
            f"{logical_key}:g2",
        ]


class TestPrAdoptionHistory:
    @pytest.mark.unit
    async def test_pr_number_fallback_is_scoped_to_adoption_repo_policy(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        target = await repo.create(
            repo_url="https://github.com/dimileeh/aira-web.git",
            branch_base="development",
            task_title="adopt target",
            task_prompt="monitor target PR",
            agent="codex",
            test_commands=[],
            task_external_id="adopt-target-external",
            idempotency_key="adopt-target-key",
            task_kind="sync_feature_pr",
            task_policy={"pr_adoption": {"repo_slug": "DIMILEEH/AIRA-WEB", "pr_number": 277}},
        )
        target.pr_number = 277
        unrelated = await repo.create(
            repo_url="https://github.com/example/other.git",
            branch_base="development",
            task_title="adopt unrelated",
            task_prompt="monitor unrelated PR",
            agent="codex",
            test_commands=[],
            task_external_id="adopt-other-external",
            idempotency_key="adopt-other-key",
            task_kind="sync_feature_pr",
            task_policy={"pr_adoption": {"repo_slug": "example/other", "pr_number": 277}},
        )
        unrelated.pr_number = 277
        await session.commit()
        session.expunge_all()

        loaded_workspace_ids: list[str] = []

        def capture_workspace_load(workspace: Workspace, _context: object) -> None:
            loaded_workspace_ids.append(workspace.id)

        event.listen(Workspace, "load", capture_workspace_load)
        try:
            history = await WorkspaceRepository(session).list_pr_adoption_history(
                task_external_id="adopt-target-external",
                idempotency_key="adopt-target-key",
                task_kind="sync_feature_pr",
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )
        finally:
            event.remove(Workspace, "load", capture_workspace_load)

        assert [workspace.id for workspace in history] == [target.id]
        assert target.id in loaded_workspace_ids
        assert unrelated.id not in loaded_workspace_ids


class TestExists:
    @pytest.mark.unit
    async def test_returns_boolean_existence(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

        assert await repo.exists(ws.id) is True
        assert await repo.exists("ws_missing") is False

    @pytest.mark.unit
    async def test_does_not_hydrate_workspace_entity(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
        workspace_id = ws.id
        session.expunge_all()

        assert await repo.exists(workspace_id) is True
        assert not any(isinstance(obj, Workspace) for obj in session.identity_map.values())


class TestListWorkspaces:
    @pytest.mark.unit
    async def test_combines_status_agent_and_repo_filters(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        matching = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="matching",
            task_prompt="p",
            agent=AgentRuntime.gemini.value,
            test_commands=[],
        )
        await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="wrong agent",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        await repo.create(
            repo_url="git@github.com:example/other.git",
            branch_base="development",
            task_title="wrong repo",
            task_prompt="p",
            agent=AgentRuntime.gemini.value,
            test_commands=[],
        )

        rows = await repo.list(
            status=WorkspaceStatus.requested,
            agent=AgentRuntime.gemini,
            repo_url="git@github.com:example/app.git",
            limit=10,
        )

        assert [row.id for row in rows] == [matching.id]

    @pytest.mark.unit
    async def test_accepts_string_status_and_agent_filters(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        matching = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="matching",
            task_prompt="p",
            agent=AgentRuntime.gemini.value,
            test_commands=[],
        )
        await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="wrong agent",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )

        rows = await repo.list(
            status=WorkspaceStatus.requested.value,
            agent=AgentRuntime.gemini.value,
            limit=10,
        )

        assert [row.id for row in rows] == [matching.id]

    @pytest.mark.unit
    async def test_applies_filters_before_limit(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        matching = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="older matching",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        newer_non_matching = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="newer non-matching",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        await repo.transition(matching, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(matching, to=WorkspaceStatus.ready, reason_code="TEST")
        matching.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        newer_non_matching.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        await session.commit()

        rows = await repo.list(status=WorkspaceStatus.ready, limit=1)

        assert [row.id for row in rows] == [matching.id]

    @pytest.mark.unit
    async def test_orders_created_at_ties_by_id_desc(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        rows = [
            await repo.create(
                repo_url=f"git@github.com:example/app-{index}.git",
                branch_base="development",
                task_title=f"workspace {index}",
                task_prompt="p",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            for index in range(3)
        ]
        tied_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        for row in rows:
            row.created_at = tied_created_at
        await session.commit()

        listed = await repo.list(limit=3)

        assert [row.id for row in listed] == sorted((row.id for row in rows), reverse=True)

    @pytest.mark.unit
    async def test_list_applies_created_at_cursor_bound(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        oldest = await repo.create(
            repo_url="git@github.com:example/oldest.git",
            branch_base="development",
            task_title="oldest",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        middle = await repo.create(
            repo_url="git@github.com:example/middle.git",
            branch_base="development",
            task_title="middle",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        newest = await repo.create(
            repo_url="git@github.com:example/newest.git",
            branch_base="development",
            task_title="newest",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        oldest.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        middle.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        newest.created_at = datetime(2026, 1, 3, tzinfo=UTC)
        await session.commit()

        rows = await repo.list(
            before_created_at=newest.created_at,
            before_workspace_id=newest.id,
            limit=2,
        )

        assert [row.id for row in rows] == [middle.id, oldest.id]


class TestOwnedPathOverlapLookup:
    @pytest.mark.unit
    async def test_scheduler_orders_by_class_priority_then_score_then_age(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        now = datetime.now(UTC)
        docs = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="docs",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 100}},
        )
        old_refactor = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="old refactor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 45}},
        )
        young_refactor = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="young refactor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 55}},
        )
        migration = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="migration",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.migration_task.value,
            task_policy={"scheduler": {"base_priority": 0}},
        )
        old_refactor.created_at = now - timedelta(hours=6)
        young_refactor.created_at = now
        docs.created_at = now - timedelta(days=1)
        migration.created_at = now
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=4,
        )

        assert listed == [migration.id, old_refactor.id, young_refactor.id, docs.id]

    @pytest.mark.unit
    async def test_scheduler_orders_integer_valued_decimal_policy_strings(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        decimal_string = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="decimal string priority",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": "100.0", "human_boost": "5.00"}},
        )
        lower_priority = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="lower priority",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 20}},
        )
        decimal_string.created_at = scoring_at
        lower_priority.created_at = scoring_at
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=2,
            scoring_at=scoring_at,
        )

        assert listed == [decimal_string.id, lower_priority.id]

    @pytest.mark.unit
    async def test_scheduler_clamps_oversized_policy_strings_before_postgres_integer_cast(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        oversized_high = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="oversized high priority",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={
                "scheduler": {
                    "base_priority": "999999999999999999999999999999999999999999999999"
                }
            },
        )
        normal = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="normal priority",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        oversized_low = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="oversized low priority",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={
                "scheduler": {
                    "base_priority": "-999999999999999999999999999999999999999999999999"
                }
            },
        )
        oversized_high.created_at = scoring_at
        normal.created_at = scoring_at
        oversized_low.created_at = scoring_at
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=3,
            scoring_at=scoring_at,
        )

        assert listed == [oversized_high.id, normal.id, oversized_low.id]

    @pytest.mark.unit
    async def test_scheduler_keeps_owned_path_overlap_advisory_only(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/awf/api/schemas.py"],
        )
        requested = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="overlap",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            owned_paths=["src/awf/api/schemas.py"],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 80}},
        )
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=10,
        )

        assert existing.id in listed
        assert requested.id in listed
        assert await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/awf/api/schemas.py"],
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.requested,
            WorkspaceStatus.ready,
            WorkspaceStatus.monitoring_pr,
        ],
    )
    async def test_postgres_scheduler_lists_skip_locked_rows(
        self,
        status: WorkspaceStatus,
    ) -> None:
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_claimed", status=status)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_ids(
            status=status,
            limit=1,
            exclude_ids={"ws_active"},
        )

        assert listed == ["ws_claimed"]
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql
        assert "LIMIT 1" in sql
        assert f"workspaces.status = '{status.value}'" in sql
        assert "workspaces.id NOT IN ('ws_active')" in sql

    @pytest.mark.unit
    async def test_postgres_scheduler_workspace_rows_apply_candidate_limit(self) -> None:
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[
                _recorded_workspace_row("ws_first", status=WorkspaceStatus.ready),
                _recorded_workspace_row("ws_second", status=WorkspaceStatus.ready),
            ],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.ready,
            limit=2,
            exclude_ids={"ws_active"},
        )

        assert [workspace.id for workspace in listed] == ["ws_first", "ws_second"]
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql
        assert "LIMIT 2" in sql
        assert "workspaces.id NOT IN ('ws_active')" in sql

    @pytest.mark.unit
    async def test_list_schedulable_workspaces_returns_empty_for_non_positive_limit(self) -> None:
        session = _RecordingSchedulerSession("postgresql", values=[])
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.ready,
            limit=0,
        )

        assert listed == []
        assert session.executed == []

    @pytest.mark.unit
    async def test_postgres_scheduler_cursor_uses_scheduler_order_keyset_without_offset(
        self,
    ) -> None:
        cursor_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_after", status=WorkspaceStatus.ready)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.ready,
            limit=1,
            after=SchedulerOrderCursor(
                class_priority=2,
                effective_score=42,
                queued_at=cursor_created_at,
                workspace_id="ws_cursor",
                scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
            ),
        )

        assert [workspace.id for workspace in listed] == ["ws_after"]
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql
        assert "OFFSET" not in sql
        assert "< 2" in sql
        assert "< 42" in sql
        assert "workspaces.created_at >" in sql
        assert "workspaces.created_at =" in sql
        assert "workspaces.id > 'ws_cursor'" in sql

    @pytest.mark.unit
    async def test_postgres_scheduler_cursor_reuses_cursor_scoring_timestamp(
        self,
    ) -> None:
        cursor_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        cursor_scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_after", status=WorkspaceStatus.ready)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.ready,
            limit=1,
            after=SchedulerOrderCursor(
                class_priority=2,
                effective_score=42,
                queued_at=cursor_created_at,
                workspace_id="ws_cursor",
                scoring_at=cursor_scoring_at,
            ),
        )

        assert [workspace.id for workspace in listed] == ["ws_after"]
        assert len(session.executed) == 1
        compiled = session.executed[0].compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        )
        assert cursor_scoring_at in compiled.params.values()

    @pytest.mark.unit
    async def test_postgres_scheduler_id_cursor_reuses_cursor_scoring_timestamp(
        self,
    ) -> None:
        cursor_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        cursor_scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_after", status=WorkspaceStatus.ready)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.ready,
            limit=1,
            after=SchedulerOrderCursor(
                class_priority=2,
                effective_score=42,
                queued_at=cursor_created_at,
                workspace_id="ws_cursor",
                scoring_at=cursor_scoring_at,
            ),
        )

        assert listed == ["ws_after"]
        assert len(session.executed) == 1
        compiled = session.executed[0].compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        )
        assert cursor_scoring_at in compiled.params.values()

    @pytest.mark.unit
    async def test_postgres_scheduler_cursor_rejects_mismatched_scoring_timestamp(
        self,
    ) -> None:
        cursor_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        cursor_scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_after", status=WorkspaceStatus.ready)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="scoring_at must match after.scoring_at"):
            await repo.list_schedulable_workspaces(
                status=WorkspaceStatus.ready,
                limit=1,
                scoring_at=cursor_scoring_at + timedelta(seconds=1),
                after=SchedulerOrderCursor(
                    class_priority=2,
                    effective_score=42,
                    queued_at=cursor_created_at,
                    workspace_id="ws_cursor",
                    scoring_at=cursor_scoring_at,
                ),
            )

        assert session.executed == []

    @pytest.mark.unit
    async def test_postgres_get_for_update_locks_workspace_row(self) -> None:
        session = _RecordingSchedulerSession("postgresql", values=["ws_locked"])
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        locked = await repo.get_for_update("ws_locked")

        assert locked == "ws_locked"
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "FOR UPDATE" in sql
        assert "workspaces.id = 'ws_locked'" in sql

    @pytest.mark.unit
    async def test_session_info_dialect_drives_scheduler_locking(self) -> None:
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_claimed")],
        )
        session.info[SESSION_DIALECT_NAME_KEY] = "postgresql"
        repo = WorkspaceRepository(session)  # type: ignore[arg-type]

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=1,
        )

        assert listed == ["ws_claimed"]
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "SKIP LOCKED" in sql

    @pytest.mark.unit
    @pytest.mark.unit
    async def test_empty_requested_owned_paths_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(session, repo, owned_paths=["src/awf/api/**"])

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=[],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_non_overlapping_owned_paths_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(session, repo, owned_paths=["src/awf/api/**"])

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/**"],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_same_paths_on_different_repo_or_base_branch_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            repo_url="git@github.com:example/other.git",
            branch_base="development",
            owned_paths=["src/awf/api/**"],
        )
        await _create_policy_workspace(
            session,
            repo,
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            owned_paths=["src/awf/api/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/awf/api/routes/workspaces.py"],
        )

        assert overlaps == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroying,
            WorkspaceStatus.destroyed,
        ],
    )
    async def test_terminal_and_teardown_statuses_do_not_report_overlap(
        self,
        session: AsyncSession,
        status: WorkspaceStatus,
    ) -> None:
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/awf/api/**"],
            status=status,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/awf/api/routes/workspaces.py"],
        )

        assert overlaps == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("status", "existing_path", "requested_path"),
        [
            (
                WorkspaceStatus.requested,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.provisioning,
                "src/awf/api",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.ready,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api",
            ),
            (
                WorkspaceStatus.running,
                "src/awf/api/**",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.validating,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/**",
            ),
            (
                WorkspaceStatus.pushing,
                "src/awf/api/*.py",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.monitoring_pr,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/*.py",
            ),
            (
                WorkspaceStatus.running,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/../api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.validating,
                "src/awf/api/**",
                "src/awf/service/../api/routes/workspaces.py",
            ),
        ],
    )
    async def test_active_exact_ancestor_and_wildcard_paths_report_overlap(
        self,
        session: AsyncSession,
        status: WorkspaceStatus,
        existing_path: str,
        requested_path: str,
    ) -> None:
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=[existing_path],
            status=status,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=[requested_path],
        )

        assert len(overlaps) == 1
        assert overlaps[0].workspace_id == existing.id
        assert overlaps[0].existing_path == existing_path
        assert overlaps[0].requested_path == requested_path


class TestTransition:
    @pytest.mark.unit
    async def test_valid_transition_updates_status_and_bumps_version(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="WORKER_CLAIMED")
        await session.commit()

        assert ws.status == WorkspaceStatus.provisioning.value
        assert ws.version == 2
        assert len(ws.events) == 2
        assert ws.events[-1].old_state == WorkspaceStatus.requested.value
        assert ws.events[-1].new_state == WorkspaceStatus.provisioning.value
        assert ws.events[-1].reason_code == "WORKER_CLAIMED"

    @pytest.mark.unit
    async def test_transition_to_monitoring_pr_stamps_monitor_start(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(ws, to=target, reason_code="X")
        await session.commit()

        assert ws.monitor_started_at is not None

    @pytest.mark.unit
    async def test_invalid_transition_raises_and_does_not_mutate(
        self, session: AsyncSession
    ) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

        with pytest.raises(InvalidWorkspaceTransitionError):
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="BAD")

        # Nothing changed.
        assert ws.status == WorkspaceStatus.requested.value
        assert ws.version == 1


class TestListEvents:
    @pytest.mark.unit
    async def test_uses_event_id_as_stable_timestamp_tie_breaker(
        self, session: AsyncSession
    ) -> None:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.flush()

        occurred_at = datetime(2100, 1, 1, tzinfo=UTC)
        session.add_all(
            [
                WorkspaceEvent(
                    id="evt_aaa",
                    workspace_id=workspace.id,
                    event_type="workspace.state_changed",
                    old_state=WorkspaceStatus.requested.value,
                    new_state=WorkspaceStatus.provisioning.value,
                    reason_code="A",
                    occurred_at=occurred_at,
                ),
                WorkspaceEvent(
                    id="evt_zzz",
                    workspace_id=workspace.id,
                    event_type="workspace.state_changed",
                    old_state=WorkspaceStatus.provisioning.value,
                    new_state=WorkspaceStatus.ready.value,
                    reason_code="B",
                    occurred_at=occurred_at,
                ),
            ]
        )
        await session.commit()

        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id, limit=2)

        assert [event.id for event in events] == ["evt_zzz", "evt_aaa"]
