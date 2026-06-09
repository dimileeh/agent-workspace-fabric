"""Workspace repository transition and event tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession

import awf.db.repositories as repositories
from awf.control.state_machine import InvalidWorkspaceTransitionError
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an isolated PostgreSQL test session."""
    async with postgres_test_session() as s:
        yield s


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
