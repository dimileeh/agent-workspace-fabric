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
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    validation_command_set_hash,
)
from awf.db.session import make_engine, make_session_factory
from tests.postgres import (
    create_postgres_test_engine,
    postgres_alembic_subprocess_lock,
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

    @pytest.mark.unit
    async def test_create_replacement_from_copies_request_fields_and_requires_profile_reresolution(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="recover me",
            task_prompt="recover this task",
            task_external_id="task-123",
            task_class="migration_task",
            owned_paths=["src/awf/**"],
            task_policy={"provider": {"model": "gpt-5.5"}},
            auto_merge=False,
            initial_review_grace_period_seconds=30.0,
            agent="codex",
            env_profile="python",
            profile_ref="repo-profile",
            requested_profile={"profile": {"name": "requested"}},
            resolved_profile={"profile": {"name": "resolved"}},
            test_commands=["uv run pytest"],
            requires_database=True,
            idempotency_key="source-key",
            task_kind="sync_release_pr",
            task_tag="PROJ-123",
            remote_push_branch="development",
        )
        source.branch_name = "release-sync/source"
        source.base_commit = "a" * 40
        source.pr_url = "https://github.com/example/a/pull/1"
        source.monitor_last_commit_sha = "b" * 40

        replacement = await repo.create_replacement_from(
            source,
            idempotency_key="replacement-key",
        )

        assert replacement.status == WorkspaceStatus.requested.value
        assert replacement.id != source.id
        assert replacement.idempotency_key == "replacement-key"
        assert replacement.repo_url == source.repo_url
        assert replacement.branch_base == source.branch_base
        assert replacement.task_title == source.task_title
        assert replacement.task_prompt == source.task_prompt
        assert replacement.task_external_id == source.task_external_id
        assert replacement.task_tag == "PROJ-123"
        assert replacement.task_class == source.task_class
        assert replacement.owned_paths == ["src/awf/**"]
        assert replacement.task_policy == {"provider": {"model": "gpt-5.5"}}
        assert replacement.auto_merge is False
        assert replacement.initial_review_grace_period_seconds == 30.0
        assert replacement.agent == source.agent
        assert replacement.env_profile == source.env_profile
        assert replacement.profile_ref == source.profile_ref
        assert replacement.requested_profile == {"profile": {"name": "requested"}}
        assert replacement.resolved_profile is None
        assert replacement.test_commands == ["uv run pytest"]
        assert replacement.requires_database is True
        assert replacement.task_kind == "sync_release_pr"
        assert replacement.remote_push_branch is None
        assert replacement.branch_name is None
        assert replacement.base_commit is None
        assert replacement.pr_url is None
        assert replacement.monitor_last_commit_sha is None
        assert replacement.owned_paths is not source.owned_paths
        assert replacement.test_commands is not source.test_commands
        assert replacement.task_policy is not source.task_policy
        assert replacement.task_policy["provider"] is not source.task_policy["provider"]
        assert replacement.requested_profile is not source.requested_profile


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
                    "failing_test_node_ids": ["tests/unit/test_widget.py::test_handles_edges"],
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
        repo_root = Path(__file__).resolve().parents[4]
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
            with postgres_alembic_subprocess_lock(database_url):
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
        repo_root = Path(__file__).resolve().parents[4]
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
            with postgres_alembic_subprocess_lock(database_url):
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
    async def test_list_idempotency_replay_keys_is_bounded(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        base_time = datetime(2026, 1, 1, tzinfo=UTC)
        null_key_workspace = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="null key",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            idempotency_key=None,
        )
        null_key_workspace.created_at = base_time - timedelta(seconds=1)
        for index, idempotency_key in enumerate(
            [
                "idem-replay-bound-a",
                "idem-replay-bound-b",
                "idem-replay-bound-c",
            ],
        ):
            workspace = await repo.create(
                repo_url="git@github.com:example/a.git",
                branch_base="development",
                task_title=f"bounded replay key {index}",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                idempotency_key=idempotency_key,
            )
            workspace.created_at = base_time + timedelta(seconds=index)
        await session.commit()

        assert await repo.list_idempotency_replay_keys(limit=2) == [
            "idem-replay-bound-a",
            "idem-replay-bound-b",
        ]

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
