"""Repository tests against in-memory SQLite.

SQLite is fine for pure CRUD + state-transition tests because the ORM layer
hides dialect differences. Any Postgres-only behaviour (native JSONB ops,
row-level locking, FOR UPDATE SKIP LOCKED) needs a dedicated integration test
under tests/integration/ with testcontainers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.state_machine import InvalidWorkspaceTransitionError
from awf.db.base import Base
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
