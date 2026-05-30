"""Repository tests against isolated PostgreSQL schemas."""

from __future__ import annotations

import inspect
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

import awf.db.repositories as repositories
from awf.control.state_machine import InvalidWorkspaceTransitionError
from awf.db.base import Base
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import AgentRuntime, TaskClass, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceEventRepository,
    WorkspaceRepository,
    _schedulable_workspace_ids_stmt,
)
from awf.service.scheduler import SchedulerOrderCursor, scheduler_score_from_workspace
from tests.postgres import (
    postgres_test_session,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an isolated PostgreSQL test session."""
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
    resolved_profile: dict | None = None,
) -> Workspace:
    workspace = await repo.create(
        repo_url=repo_url,
        branch_base=branch_base,
        task_title="policy test",
        task_prompt="do policy-sensitive work",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=list(owned_paths or []),
        resolved_profile=resolved_profile,
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


class TestOwnedPathOverlapLookup:
    """Owned-path overlap lookup scheduling and repository behavior tests."""

    @pytest.mark.unit
    async def test_scheduler_orders_by_class_priority_then_score_then_age(
        self,
        session: AsyncSession,
    ) -> None:
        """Verify scheduler ordering combines class, score, and age priority."""
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
        """Verify integer-valued decimal policy strings affect ordering."""
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
        """Verify oversized scheduler strings are bounded before integer casts."""
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
                "scheduler": {"base_priority": "999999999999999999999999999999999999999999999999"}
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
                "scheduler": {"base_priority": "-999999999999999999999999999999999999999999999999"}
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
    async def test_scheduler_ignores_policy_strings_above_python_int_limit(
        self,
        session: AsyncSession,
    ) -> None:
        """Verify scheduler scoring ignores strings beyond Python's int limit."""
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(640)
        try:
            oversized_high = await repo.create(
                repo_url="git@github.com:example/app.git",
                branch_base="development",
                task_title="oversized high priority",
                task_prompt="p",
                agent=AgentRuntime.codex.value,
                test_commands=[],
                task_class=TaskClass.docs_task.value,
                task_policy={"scheduler": {"base_priority": "9" * 641}},
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
            oversized_high.created_at = scoring_at
            normal.created_at = scoring_at
            await session.commit()

            listed = await repo.list_schedulable_ids(
                status=WorkspaceStatus.requested,
                limit=1,
                scoring_at=scoring_at,
            )

            assert listed == [normal.id]
        finally:
            sys.set_int_max_str_digits(previous_limit)

    @pytest.mark.unit
    def test_sqlite_scheduler_ignores_policy_strings_above_python_int_limit_before_limit(
        self,
    ) -> None:
        """Verify SQLite scheduling avoids parsing oversized priority strings."""
        engine = create_engine("sqlite:///:memory:", future=True)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(640)
        try:
            Base.metadata.create_all(engine)
            with SyncSession(engine) as session:
                oversized_high = Workspace(
                    id="ws_sqlite_oversized_priority",
                    status=WorkspaceStatus.requested.value,
                    version=1,
                    repo_url="git@github.com:example/app.git",
                    branch_base="development",
                    task_title="oversized high priority",
                    task_prompt="p",
                    agent=AgentRuntime.codex.value,
                    test_commands=[],
                    task_class=TaskClass.docs_task.value,
                    task_policy={"scheduler": {"base_priority": "9" * 641}},
                    owned_paths=[],
                    created_at=scoring_at,
                    updated_at=scoring_at,
                )
                normal = Workspace(
                    id="ws_sqlite_normal_priority",
                    status=WorkspaceStatus.requested.value,
                    version=1,
                    repo_url="git@github.com:example/app.git",
                    branch_base="development",
                    task_title="normal priority",
                    task_prompt="p",
                    agent=AgentRuntime.codex.value,
                    test_commands=[],
                    task_class=TaskClass.docs_task.value,
                    task_policy={"scheduler": {"base_priority": 50}},
                    owned_paths=[],
                    created_at=scoring_at,
                    updated_at=scoring_at,
                )
                session.add_all([oversized_high, normal])
                session.commit()

                stmt = _schedulable_workspace_ids_stmt(
                    status=WorkspaceStatus.requested,
                    limit=1,
                    scoring_at=scoring_at,
                    dialect_name="sqlite",
                    skip_locked=False,
                )

                listed = session.execute(stmt).scalars().all()

            assert [workspace.id for workspace in listed] == ["ws_sqlite_normal_priority"]
        finally:
            sys.set_int_max_str_digits(previous_limit)
            engine.dispose()

    @pytest.mark.unit
    async def test_scheduler_cursor_recomputes_database_score_for_page_boundary(
        self,
        session: AsyncSession,
    ) -> None:
        """Verify cursor pagination recomputes database-side scheduler scores."""
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        before_cursor = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="before cursor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        cursor = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="cursor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        after_cursor = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="after cursor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        before_cursor.created_at = scoring_at - timedelta(seconds=1)
        cursor.created_at = scoring_at
        after_cursor.created_at = scoring_at + timedelta(seconds=1)
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=10,
            scoring_at=scoring_at,
            after=SchedulerOrderCursor(
                class_priority=0,
                effective_score=999,
                queued_at=cursor.created_at,
                workspace_id=cursor.id,
                scoring_at=scoring_at,
            ),
        )

        assert listed == [after_cursor.id]

    @pytest.mark.unit
    async def test_scheduler_cursor_tie_breaks_equal_score_and_created_at_by_workspace_id(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify scheduler cursors break equal score and age ties by ID."""
        ids = iter(("ws_scheduler_tie_001", "ws_scheduler_tie_002", "ws_scheduler_tie_003"))
        monkeypatch.setattr(repositories, "new_workspace_id", lambda: next(ids))
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        shared_created_at = datetime(2026, 5, 2, 11, 55, tzinfo=UTC)
        first = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="first tie",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        second = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="second tie",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        third = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="third tie",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 50}},
        )
        for workspace in (first, second, third):
            workspace.created_at = shared_created_at
        await session.commit()

        page_one = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=2,
            scoring_at=scoring_at,
        )
        cursor_score = scheduler_score_from_workspace(second, now=scoring_at)
        page_two = await repo.list_schedulable_ids(
            status=WorkspaceStatus.requested,
            limit=10,
            scoring_at=scoring_at,
            after=SchedulerOrderCursor(
                class_priority=cursor_score.class_priority,
                effective_score=cursor_score.effective_score,
                queued_at=second.created_at,
                workspace_id=second.id,
                scoring_at=scoring_at,
            ),
        )

        assert page_one == [first.id, second.id]
        assert page_two == [third.id]

    @pytest.mark.unit
    async def test_scheduler_keeps_owned_path_overlap_advisory_only(
        self,
        session: AsyncSession,
    ) -> None:
        """Verify owned-path overlap does not block scheduler admission."""
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
        """Verify Postgres scheduler queries use skip-locked row claims."""
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
        """Verify schedulable workspace rows honor the candidate limit."""
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
    async def test_postgres_scheduler_workspace_rows_can_scope_to_node_id(self) -> None:
        """Verify schedulable workspace rows can be scoped to a node."""
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_local", status=WorkspaceStatus.requested)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.requested,
            limit=3,
            node_id="worker-node-a",
        )

        assert [workspace.id for workspace in listed] == ["ws_local"]
        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "workspaces.status = 'requested'" in sql
        assert "workspaces.node_id = 'worker-node-a'" in sql
        assert "workspaces.node_id IS NULL" in sql
        assert "LIMIT 3" in sql

    @pytest.mark.unit
    async def test_list_schedulable_workspaces_returns_empty_for_non_positive_limit(self) -> None:
        """Verify non-positive scheduler limits avoid database execution."""
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
        """Verify scheduler cursor pagination uses keysets instead of offsets."""
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
        assert "WITH scheduler_cursor_order AS" in sql
        assert "scheduler_cursor_order.class_priority" in sql
        assert "scheduler_cursor_order.effective_score" in sql
        assert sql.count("scheduler_cursor_workspace.id = 'ws_cursor'") == 2
        assert "scheduler_cursor_workspace" in sql
        assert "workspaces.created_at >" in sql
        assert "workspaces.created_at =" in sql
        assert "workspaces.id > 'ws_cursor'" in sql

    @pytest.mark.unit
    async def test_postgres_scheduler_cursor_age_boost_uses_timestamp_thresholds(
        self,
    ) -> None:
        """Verify scheduler age boost uses timestamp thresholds."""
        session = _RecordingSchedulerSession(
            "postgresql",
            values=[_recorded_workspace_row("ws_after", status=WorkspaceStatus.ready)],
        )
        repo = WorkspaceRepository(session, dialect_name="postgresql")  # type: ignore[arg-type]

        await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.ready,
            limit=1,
            after=SchedulerOrderCursor(
                class_priority=2,
                effective_score=42,
                queued_at=datetime(2026, 1, 1, tzinfo=UTC),
                workspace_id="ws_cursor",
                scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
            ),
        )

        assert len(session.executed) == 1
        sql = str(
            session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "EXTRACT(epoch" not in sql
        assert "INTERVAL '" not in sql
        assert "make_interval(secs => 900.0)" in sql

        compiled = session.executed[0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
        age_boost_seconds = {
            value for key, value in compiled.params.items() if key.startswith("seconds_")
        }
        assert age_boost_seconds == {
            float(repositories.AGE_BOOST_INTERVAL_SECONDS * boost)
            for boost in range(1, repositories.AGE_BOOST_MAX + 1)
        }

    @pytest.mark.unit
    def test_postgres_scheduler_age_boost_does_not_use_raw_interval_text(self) -> None:
        """Verify Postgres age boost construction avoids raw interval text."""
        source = "\n".join(
            inspect.getsource(function)
            for function in (
                repositories._postgresql_scheduler_age_boost_expr,
                repositories._postgresql_interval_seconds_expr,
            )
        )
        forbidden_text_call = "text(" + 'f"INTERVAL'

        assert forbidden_text_call not in source
        assert "INTERVAL '" not in source
        assert 'column("secs")' not in source

    @pytest.mark.unit
    def test_scheduler_json_int_expr_handles_unbounded_digits_and_unknown_dialect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify scheduler JSON integer expressions handle safe fallbacks."""
        monkeypatch.setattr(repositories.sys, "get_int_max_str_digits", lambda: 0)

        postgres_expr = repositories._scheduler_json_int_expr(  # noqa: SLF001
            ("scheduler", "base_priority"),
            "postgresql",
        )
        sqlite_expr = repositories._scheduler_json_int_expr(  # noqa: SLF001
            ("scheduler", "base_priority"),
            "sqlite",
        )
        fallback_expr = repositories._scheduler_json_int_expr(  # noqa: SLF001
            ("scheduler", "base_priority"),
            "unknown",
        )
        zero_boost_expr = repositories._scheduler_age_boost_expr(  # noqa: SLF001
            scoring_at=datetime(2026, 1, 1, tzinfo=UTC),
            dialect_name="unknown",
        )

        assert postgres_expr.compile(dialect=postgresql.dialect()) is not None
        assert sqlite_expr.compile(dialect=sqlite.dialect()) is not None
        assert fallback_expr is not None
        assert zero_boost_expr is not None

    @pytest.mark.unit
    async def test_postgres_scheduler_cursor_reuses_cursor_scoring_timestamp(
        self,
    ) -> None:
        """Verify workspace cursor queries reuse the cursor scoring timestamp."""
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
        """Verify ID cursor queries reuse the cursor scoring timestamp."""
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
        """Verify scheduler cursor queries reject mismatched scoring times."""
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
        """Verify Postgres get-for-update locks the workspace row."""
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
        """Verify session dialect metadata enables scheduler locking."""
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
        """Verify empty requested owned paths produce no overlap."""
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
        """Verify non-overlapping requested paths produce no overlap."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(session, repo, owned_paths=["src/awf/api/**"])

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/**"],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Plan-artifact-only matches are excluded from repository overlaps."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/existing/**", "docs/awf-plans/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/requested/**", "docs/awf-plans/**"],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Profile-configured planning artifacts are excluded from repository overlaps."""
        monkeypatch.setattr(
            repositories,
            "new_workspace_id",
            lambda: "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
        )
        custom_profile = {
            "planning": {
                "plan_path": "docs/alternate/{workspace_id}.md",
                "conformance_report_path": "docs/alternate/{workspace_id}.json",
            },
        }
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            owned_paths=[
                "src/existing/**",
                "docs/alternate/ws_aaaaaaaaaaaaaaaaaaaaaaaa.md",
            ],
            resolved_profile=custom_profile,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=[
                "src/requested/**",
                "docs/alternate/ws_bbbbbbbbbbbbbbbbbbbbbbbb.md",
            ],
            resolved_profile=custom_profile,
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Requested real docs matching ws_* keep overlap checks before id assignment."""
        custom_profile = {"planning": {"plan_path": "docs/{workspace_id}.md"}}
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/ws_protocol.md"],
            resolved_profile=custom_profile,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/ws_protocol.md"],
            resolved_profile=custom_profile,
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/ws_protocol.md",
                requested_path="docs/ws_protocol.md",
            )
        ]

    @pytest.mark.unit
    async def test_internal_plan_artifact_filter_does_not_hide_real_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Real source overlaps are preserved when plan artifacts also match."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/shared/**", "docs/awf-plans/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/shared/module.py", "docs/awf-plans/**"],
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="src/shared/**",
                requested_path="src/shared/module.py",
            )
        ]

    @pytest.mark.unit
    async def test_real_docs_owned_paths_still_report_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Repository documentation paths outside AWF internals still overlap."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/runbooks/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/runbooks/deploy.md"],
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/runbooks/**",
                requested_path="docs/runbooks/deploy.md",
            )
        ]

    @pytest.mark.unit
    async def test_awf_plans_readme_owned_paths_still_report_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """The tracked awf-plans README is not filtered as generated metadata."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/awf-plans/README.md"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/awf-plans/README.md"],
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/awf-plans/README.md",
                requested_path="docs/awf-plans/README.md",
            )
        ]

    @pytest.mark.unit
    async def test_same_paths_on_different_repo_or_base_branch_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        """Verify overlap checks are scoped by repository and base branch."""
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
        """Verify terminal and teardown workspaces do not overlap."""
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
        """Verify active exact, ancestor, and wildcard paths report overlap."""
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
    """Workspace transition repository tests."""

    @pytest.mark.unit
    async def test_valid_transition_updates_status_and_bumps_version(
        self, session: AsyncSession
    ) -> None:
        """Verify valid transitions update status, version, and events."""
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
        """Verify transitions into PR monitoring stamp monitor start time."""
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
    async def test_atomic_transition_to_monitoring_pr_stamps_monitor_start(
        self, session: AsyncSession
    ) -> None:
        """Verify atomic PR-monitoring transitions stamp monitor start time."""
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/atomic.git",
            branch_base="development",
            task_title="atomic",
            task_prompt="transition",
            agent="codex",
            test_commands=[],
        )
        ws.idempotency_key = "atomic-transition-key"
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ):
            await repo.transition(ws, to=target, reason_code="X")
        await session.flush()

        atomic_repo = WorkspaceRepository(session, dialect_name="sqlite")
        assert await atomic_repo.has_idempotency_key("atomic-transition-key")
        transitioned = await atomic_repo.transition_if_current(
            ws.id,
            from_status=WorkspaceStatus.pushing,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_CREATED",
        )

        assert transitioned is not None
        assert transitioned.monitor_started_at is not None
        assert transitioned.status == WorkspaceStatus.monitoring_pr.value

    @pytest.mark.unit
    async def test_invalid_transition_raises_and_does_not_mutate(
        self, session: AsyncSession
    ) -> None:
        """Verify invalid transitions fail without mutating the workspace."""
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


class TestAddEvents:
    """Workspace event append and ordering tests."""

    @pytest.mark.unit
    async def test_transition_if_current_reserves_event_order_through_shared_helper(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify guarded transitions reserve event order through the helper."""
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        calls: list[tuple[str, int, bool]] = []
        original_reserve = WorkspaceRepository._reserve_workspace_event_orders

        async def _recording_reserve(
            self: WorkspaceRepository,
            reserved_workspace: Workspace,
            *,
            count: int,
            bump_version: bool = False,
        ) -> int:
            calls.append((reserved_workspace.id, count, bump_version))
            return await original_reserve(
                self,
                reserved_workspace,
                count=count,
                bump_version=bump_version,
            )

        monkeypatch.setattr(
            WorkspaceRepository,
            "_reserve_workspace_event_orders",
            _recording_reserve,
        )

        transitioned = await repo.transition_if_current(
            workspace.id,
            from_status=WorkspaceStatus.requested,
            to=WorkspaceStatus.provisioning,
            reason_code="CLAIMED",
        )

        assert transitioned is not None
        assert calls == [(workspace.id, 1, True)]
        state_event = next(event for event in transitioned.events if event.reason_code == "CLAIMED")
        assert state_event.event_order == 2
        assert transitioned.version == 2
        assert transitioned.event_sequence == 2

    @pytest.mark.unit
    async def test_transition_if_current_non_postgres_claim_uses_status_guarded_update(
        self,
    ) -> None:
        """Verify non-Postgres guarded transitions use a status-guarded update."""

        class EmptyResult:
            def one_or_none(self) -> None:
                return None

            def scalar_one_or_none(self) -> None:
                return None

        class RecordingSession:
            info: dict[str, str] = {}
            bind = None

            def __init__(self) -> None:
                self.executed: list[object] = []

            async def execute(self, statement: object) -> EmptyResult:
                self.executed.append(statement)
                return EmptyResult()

        recording_session = RecordingSession()
        repo = WorkspaceRepository(recording_session, dialect_name="sqlite")  # type: ignore[arg-type]

        transitioned = await repo.transition_if_current(
            "ws_claim",
            from_status=WorkspaceStatus.requested,
            to=WorkspaceStatus.provisioning,
            reason_code="CLAIMED",
        )

        assert transitioned is None
        assert len(recording_session.executed) == 1
        sql = " ".join(str(recording_session.executed[0].compile(dialect=sqlite.dialect())).split())
        assert sql.startswith("UPDATE workspaces SET ")
        assert "status=?" in sql
        assert "event_sequence=(workspaces.event_sequence + ?)" in sql
        assert "version=(workspaces.version + ?)" in sql
        assert "WHERE workspaces.id = ? AND workspaces.status = ?" in sql
        assert "RETURNING event_sequence, version" in sql

    @pytest.mark.unit
    async def test_batch_reserves_event_order_without_advancing_workspace_version(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify batch event appends reserve order without bumping version."""
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_version = workspace.version
        workspace_updated_at = workspace.updated_at
        committed_attrs: list[str] = []
        original_set_committed_value = repositories.set_committed_value

        def _record_committed_value(target: object, key: str, value: object) -> None:
            if target is workspace:
                committed_attrs.append(key)
            original_set_committed_value(target, key, value)

        monkeypatch.setattr(
            repositories,
            "set_committed_value",
            _record_committed_value,
        )

        events = await repo.add_events(
            workspace,
            events=[
                WorkspaceEventCreate(
                    event_type="workspace.phase_started",
                    reason_code="FIRST",
                ),
                WorkspaceEventCreate(
                    event_type="workspace.phase_finished",
                    reason_code="SECOND",
                ),
            ],
        )

        assert [event.event_order for event in events] == [
            workspace_version + 1,
            workspace_version + 2,
        ]
        assert workspace.version == workspace_version
        assert workspace.event_sequence == workspace_version + 2
        assert workspace.updated_at == workspace_updated_at

        next_event_sequence = workspace.event_sequence
        event = await repo.add_event(
            workspace,
            event_type="workspace.phase_finished",
            reason_code="THIRD",
        )

        assert event.event_order == next_event_sequence + 1
        assert workspace.version == workspace_version
        assert workspace.event_sequence == next_event_sequence + 1
        assert workspace.updated_at == workspace_updated_at
        assert committed_attrs == ["event_sequence", "event_sequence"]

    @pytest.mark.unit
    async def test_add_event_with_states_reserves_order_and_uses_explicit_states(
        self, session: AsyncSession
    ) -> None:
        """Verify stateful events reserve order and preserve explicit states."""
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/a.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_version = workspace.version
        workspace_updated_at = workspace.updated_at

        event = await repo.add_event_with_states(
            workspace,
            event_type="workspace.remonitor_requested",
            old_state=WorkspaceStatus.failed,
            new_state=WorkspaceStatus.monitoring_pr,
            reason_code="OPERATOR_REMONITOR",
            payload={"state_reset": True},
        )

        assert event.workspace_id == workspace.id
        assert event.old_state == WorkspaceStatus.failed.value
        assert event.new_state == WorkspaceStatus.monitoring_pr.value
        assert event.event_order == workspace_version + 1
        assert workspace.version == workspace_version
        assert workspace.event_sequence == workspace_version + 1
        assert workspace.updated_at == workspace_updated_at


class TestListEvents:
    """Workspace event listing repository tests."""

    @pytest.mark.unit
    async def test_uses_event_id_as_stable_timestamp_tie_breaker(
        self, session: AsyncSession
    ) -> None:
        """Verify event listing uses event ID as a stable timestamp tie breaker."""
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
