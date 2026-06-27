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
from awf.db.base import Base
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import AgentRuntime, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkerHeartbeatRepository,
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


async def _reserve_policy_workspace(
    session: AsyncSession,
    workspace: Workspace,
    *,
    node_id: str,
) -> None:
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=None,
        idempotency_key=f"scheduler-node-scope:{workspace.id}",
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    await ResourceReservationRepository(session).create(
        workspace_id=workspace.id,
        attempt_id=attempt.id,
        node_id=node_id,
        steady_cpu=1.0,
        steady_memory_gb=1.0,
        peak_cpu=1.0,
        peak_memory_gb=1.0,
        disk_mb=None,
        dind_slots=0,
        phase="workspace_lifecycle",
    )


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
    async def test_monitoring_pr_scheduler_excludes_active_execution_claims_before_limit(
        self,
        session: AsyncSession,
    ) -> None:
        """A deferred monitor row must not consume the limited recovery slot."""
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        deferred = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="deferred monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 100}},
        )
        eligible = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="eligible monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 1}},
        )
        for workspace in (deferred, eligible):
            workspace.status = WorkspaceStatus.monitoring_pr.value
            workspace.created_at = scoring_at
            workspace.monitor_claimed_by = None
            workspace.monitor_claim_expires_at = None
        deferred.execution_claimed_by = "live-execution-worker"
        deferred.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        heartbeat_at = datetime.now(UTC)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="live-execution-worker",
            node_id="local",
            started_at=heartbeat_at - timedelta(minutes=1),
            last_heartbeat_at=heartbeat_at,
            poll_interval_seconds=1.0,
        )
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.monitoring_pr,
            limit=1,
            scoring_at=scoring_at,
        )

        assert listed == [eligible.id]

    @pytest.mark.unit
    async def test_monitoring_pr_scheduler_allows_unexpired_execution_claim_from_stale_heartbeat_owner(
        self,
        session: AsyncSession,
    ) -> None:
        """Restart recovery should not wait for a dead worker's execution lease expiry."""
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        stale_owner = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="stale owner monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 100}},
        )
        lower_priority = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="lower priority monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 1}},
        )
        for workspace in (stale_owner, lower_priority):
            workspace.status = WorkspaceStatus.monitoring_pr.value
            workspace.created_at = scoring_at
            workspace.monitor_claimed_by = None
            workspace.monitor_claim_expires_at = None
        stale_owner.execution_claimed_by = "stale-execution-worker"
        stale_owner.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="stale-execution-worker",
            node_id="local",
            started_at=scoring_at - timedelta(minutes=10),
            last_heartbeat_at=scoring_at - timedelta(minutes=10),
            poll_interval_seconds=1.0,
        )
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.monitoring_pr,
            limit=1,
            scoring_at=scoring_at,
            execution_claim_owner_id="new-worker-after-restart",
        )

        assert listed == [stale_owner.id]

    @pytest.mark.unit
    async def test_monitoring_pr_deferred_active_execution_claims_require_fresh_owner(
        self,
        session: AsyncSession,
    ) -> None:
        repo = WorkspaceRepository(session)
        claim_cutoff = datetime.now(UTC)
        live_owner = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="live owner monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 100}},
        )
        stale_owner = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="stale owner monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 90}},
        )
        for workspace in (live_owner, stale_owner):
            workspace.status = WorkspaceStatus.monitoring_pr.value
            workspace.created_at = claim_cutoff
            workspace.monitor_claimed_by = None
            workspace.monitor_claim_expires_at = None
            workspace.execution_claim_expires_at = claim_cutoff + timedelta(minutes=5)
        live_owner.execution_claimed_by = "live-execution-worker"
        stale_owner.execution_claimed_by = "stale-execution-worker"
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="live-execution-worker",
            node_id="local",
            started_at=claim_cutoff - timedelta(minutes=1),
            last_heartbeat_at=claim_cutoff,
            poll_interval_seconds=1.0,
        )
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="stale-execution-worker",
            node_id="local",
            started_at=claim_cutoff - timedelta(minutes=10),
            last_heartbeat_at=claim_cutoff - timedelta(minutes=10),
            poll_interval_seconds=1.0,
        )
        await session.commit()

        deferred = await repo.list_monitoring_pr_deferred_active_execution_claim_workspaces(
            limit=10,
            claim_cutoff=claim_cutoff,
            owner_id="new-worker-after-restart",
            scoring_at=claim_cutoff,
        )

        assert [workspace.id for workspace in deferred] == [live_owner.id]

    @pytest.mark.unit
    async def test_monitoring_pr_scheduler_allows_current_execution_claim_owner(
        self,
        session: AsyncSession,
    ) -> None:
        """The listing predicate matches the monitor claim CAS owner allowance."""
        repo = WorkspaceRepository(session)
        scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        current_owner = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="current owner monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.refactor_task.value,
            task_policy={"scheduler": {"base_priority": 100}},
        )
        lower_priority = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            task_title="lower priority monitor",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            task_class=TaskClass.docs_task.value,
            task_policy={"scheduler": {"base_priority": 1}},
        )
        for workspace in (current_owner, lower_priority):
            workspace.status = WorkspaceStatus.monitoring_pr.value
            workspace.created_at = scoring_at
            workspace.monitor_claimed_by = None
            workspace.monitor_claim_expires_at = None
        current_owner.execution_claimed_by = "worker-current"
        current_owner.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

        listed = await repo.list_schedulable_ids(
            status=WorkspaceStatus.monitoring_pr,
            limit=1,
            scoring_at=scoring_at,
            execution_claim_owner_id="worker-current",
        )

        assert listed == [current_owner.id]

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
        assert "coalesce(workspaces.node_id, (SELECT resource_reservations.node_id" in sql
        assert "resource_reservations.workspace_id = workspaces.id" in sql
        assert "resource_reservations.released_at IS NULL" in sql
        assert "= 'worker-node-a'" in sql
        assert "LIMIT 3" in sql

    @pytest.mark.unit
    async def test_requested_scheduler_scopes_null_workspace_node_to_active_reservation_node(
        self,
        session: AsyncSession,
    ) -> None:
        """Verify requested rows use active reservations for planned node placement."""
        repo = WorkspaceRepository(session)
        reserved_for_node_a = await _create_policy_workspace(session, repo)
        reserved_for_node_b = await _create_policy_workspace(session, repo)
        unreserved = await _create_policy_workspace(session, repo)
        already_stamped = await _create_policy_workspace(session, repo)
        already_stamped.node_id = "worker-node-a"
        await _reserve_policy_workspace(
            session,
            reserved_for_node_a,
            node_id="worker-node-a",
        )
        await _reserve_policy_workspace(
            session,
            reserved_for_node_b,
            node_id="worker-node-b",
        )
        await session.commit()

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.requested,
            limit=10,
            node_id="worker-node-a",
            scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )

        listed_ids = {workspace.id for workspace in listed}
        assert reserved_for_node_a.id in listed_ids
        assert unreserved.id in listed_ids
        assert already_stamped.id in listed_ids
        assert reserved_for_node_b.id not in listed_ids

    @pytest.mark.unit
    async def test_requested_scheduler_local_scope_adopts_legacy_reservation_hostname(
        self,
        session: AsyncSession,
    ) -> None:
        """Local workers can recover requested rows reserved with old hostnames."""
        repo = WorkspaceRepository(session)
        legacy_reserved = await _create_policy_workspace(session, repo)
        await _reserve_policy_workspace(
            session,
            legacy_reserved,
            node_id="legacy-container-hostname",
        )
        await session.commit()

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.requested,
            limit=10,
            node_id="local",
            scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )

        assert legacy_reserved.id in {workspace.id for workspace in listed}

    @pytest.mark.unit
    async def test_requested_scheduler_local_scope_ignores_named_reservation_node(
        self,
        session: AsyncSession,
    ) -> None:
        """Local workers must not claim requested rows reserved for named nodes."""
        repo = WorkspaceRepository(session)
        reserved_for_node_a = await _create_policy_workspace(session, repo)
        await _reserve_policy_workspace(
            session,
            reserved_for_node_a,
            node_id="worker-node-a",
        )
        await session.commit()

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.requested,
            limit=10,
            node_id="local",
            scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )

        assert reserved_for_node_a.id not in {workspace.id for workspace in listed}

    @pytest.mark.unit
    async def test_requested_scheduler_named_scope_ignores_legacy_reservation_hostname(
        self,
        session: AsyncSession,
    ) -> None:
        """Named workers still ignore requested rows reserved for another node."""
        repo = WorkspaceRepository(session)
        legacy_reserved = await _create_policy_workspace(session, repo)
        await _reserve_policy_workspace(
            session,
            legacy_reserved,
            node_id="legacy-container-hostname",
        )
        await session.commit()

        listed = await repo.list_schedulable_workspaces(
            status=WorkspaceStatus.requested,
            limit=10,
            node_id="worker-node-b",
            scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )

        assert legacy_reserved.id not in {workspace.id for workspace in listed}

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
