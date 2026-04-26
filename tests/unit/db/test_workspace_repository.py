"""Repository tests against in-memory SQLite.

SQLite is fine for pure CRUD + state-transition tests because the ORM layer
hides dialect differences. Any Postgres-only behaviour (native JSONB ops,
row-level locking, FOR UPDATE SKIP LOCKED) needs a dedicated integration test
under tests/integration/ with testcontainers.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.state_machine import InvalidWorkspaceTransitionError
from awf.db.base import Base
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """One fresh in-memory SQLite DB per test, schema created from ORM metadata."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    async with factory() as s:
        yield s

    await engine.dispose()


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
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalarResult:
        return self

    def all(self) -> list[str]:
        return self._values

    def scalar_one_or_none(self) -> str | None:
        return self._values[0] if self._values else None


class _RecordingSchedulerSession:
    def __init__(self, dialect_name: str, values: list[str] | None = None) -> None:
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


class TestMonitorPolicyMigration:
    @pytest.mark.unit
    def test_monitor_policy_columns_backfill_existing_rows(
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

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO workspaces (
                    id, status, version, repo_url, branch_base,
                    task_title, task_prompt, agent, test_commands,
                    requires_database, created_at, updated_at
                )
                VALUES (
                    'ws_old_policy', 'requested', 1, 'git@example.com:repo.git',
                    'development', 'old row', 'do work', 'codex', '[]',
                    0, '2026-04-25 00:00:00', '2026-04-25 00:00:00'
                )
                """
            )
            conn.commit()

        _alembic("upgrade", "head")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT auto_merge, initial_review_grace_period_seconds
                FROM workspaces
                WHERE id = 'ws_old_policy'
                """
            ).fetchone()

        assert row == (1, None)


class TestTaskPolicyMetadataMigration:
    @pytest.mark.unit
    def test_policy_metadata_columns_backfill_existing_rows(
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

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO workspaces (
                    id, status, version, repo_url, branch_base,
                    task_title, task_prompt, agent, test_commands,
                    requires_database, created_at, updated_at
                )
                VALUES (
                    'ws_old_policy_metadata', 'requested', 1, 'git@example.com:repo.git',
                    'development', 'old row', 'do work', 'codex', '[]',
                    0, '2026-04-25 00:00:00', '2026-04-25 00:00:00'
                )
                """
            )
            conn.commit()

        _alembic("upgrade", "head")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT task_class, owned_paths
                FROM workspaces
                WHERE id = 'ws_old_policy_metadata'
                """
            ).fetchone()

        assert row is not None
        assert row[0] is None
        assert row[1] == "[]"


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


class TestOwnedPathOverlapLookup:
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
        session = _RecordingSchedulerSession("postgresql", values=["ws_claimed"])
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
        assert f"workspaces.status = '{status.value}'" in sql
        assert "workspaces.id NOT IN ('ws_active')" in sql

    @pytest.mark.unit
    async def test_sqlite_scheduler_lists_use_portable_select(self) -> None:
        session = _RecordingSchedulerSession("sqlite", values=["ws_claimed"])
        repo = WorkspaceRepository(session, dialect_name="sqlite")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=1,
        )

        assert listed == ["ws_claimed"]
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "FOR UPDATE" not in sql
        assert "SKIP LOCKED" not in sql

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
    async def test_sqlite_get_for_update_uses_portable_select(self) -> None:
        session = _RecordingSchedulerSession("sqlite", values=["ws_locked"])
        repo = WorkspaceRepository(session, dialect_name="sqlite")  # type: ignore[arg-type]

        locked = await repo.get_for_update("ws_locked")

        assert locked == "ws_locked"
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "FOR UPDATE" not in sql

    @pytest.mark.unit
    async def test_session_info_dialect_drives_scheduler_locking(self) -> None:
        session = _RecordingSchedulerSession("postgresql", values=["ws_claimed"])
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
