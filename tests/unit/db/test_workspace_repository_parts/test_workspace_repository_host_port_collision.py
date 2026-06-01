"""Host-port collision detection tests for WorkspaceRepository.

Tests the ``find_host_port_conflicts`` repository method that detects
when a companion's host_port is already mapped by a non-terminal
workspace on the same repo/branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from tests.postgres import postgres_test_session

_H2 = "git@github.com:example/hostport.git"
_B = "main"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _make_workspace(
    session: AsyncSession,
    repo: WorkspaceRepository,
    *,
    status: WorkspaceStatus = WorkspaceStatus.requested,
    task_policy: dict | None = None,
    resolved_profile: dict | None = None,
    compose_project_name: str | None = None,
) -> Workspace:
    ws = await repo.create(
        repo_url=_H2,
        branch_base=_B,
        task_title="host-port test",
        task_prompt="test host-port collision detection",
        agent="codex",
        test_commands=[],
        task_policy=task_policy or {},
        resolved_profile=resolved_profile,
    )
    ws.status = status.value
    if compose_project_name is not None:
        ws.compose_project_name = compose_project_name
    await session.commit()
    return ws


class TestFindHostPortConflicts:
    """Repository-level tests for host-port conflict detection."""

    @pytest.mark.asyncio
    async def test_collision_returns_conflict(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        existing_ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == existing_ws.id

    @pytest.mark.asyncio
    async def test_no_collision_succeeds(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[9090],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_revoked_terminal_runtime_release_blocks_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """A terminal workspace whose runtime release was revoked still blocks host ports.

        When ``stop_project_containers`` fails during orphan cleanup after a
        ``terminal_runtime_released`` event was already committed, the
        provisioner records a ``terminal_runtime_release_revoked`` event.
        Until the background cleanup sweep removes the orphan containers,
        ``find_host_port_conflicts`` must still treat those ports as occupied
        so that a new workspace does not claim them only to collide at
        ``compose up`` time.
        """
        from awf.db.repositories.base import (
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )

        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.destroyed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_revoked_release",
        )
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        await session.commit()

        conflicts_before = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts_before == [], "after genuine release, ports should be free"

        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
            payload={"orphan_stop_error": "docker stop failed"},
        )
        await session.commit()

        conflicts_after = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts_after) == 1, (
            "after revocation, ports must be treated as still occupied"
        )
        assert conflicts_after[0].host_port == 8080
        assert conflicts_after[0].workspace_id == ws.id

    @pytest.mark.asyncio
    async def test_later_release_supersedes_revocation_frees_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """A later terminal_runtime_released event supersedes an earlier revocation.

        When orphan cleanup fails and records a revoke event, a subsequent
        stop/destroy that successfully releases the stack records a new
        terminal_runtime_released event.  The later release must supersede
        the earlier revocation so that the host port is treated as free.
        """
        from datetime import UTC, datetime

        import sqlalchemy as sa

        from awf.db.models import WorkspaceEvent
        from awf.db.repositories.base import (
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )

        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.destroyed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_later_release_supersedes",
        )
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        released_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
                .order_by(WorkspaceEvent.occurred_at.desc())
                .limit(1)
            )
        ).scalar_one()
        released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
        await session.commit()

        conflicts_after_release = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts_after_release == [], "after initial release, ports should be free"

        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
            payload={"orphan_stop_error": "docker stop failed"},
        )
        revoked_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
                .order_by(WorkspaceEvent.occurred_at.desc())
                .limit(1)
            )
        ).scalar_one()
        revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
        await session.commit()

        conflicts_after_revoke = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts_after_revoke) == 1, (
            "after revocation, ports must be treated as still occupied"
        )

        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        rerelease_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
                .order_by(WorkspaceEvent.occurred_at.desc())
                .limit(1)
            )
        ).scalar_one()
        rerelease_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 2, tzinfo=UTC)
        await session.commit()

        conflicts_after_rerelease = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts_after_rerelease == [], (
            "after later successful release supersedes revocation, ports should be free"
        )


@pytest.mark.asyncio
async def test_has_terminal_runtime_released_event_revoked(
    session: AsyncSession,
) -> None:
    """has_terminal_runtime_released_event returns False when release is revoked."""
    from awf.db.repositories.base import (
        TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
        TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        has_terminal_runtime_released_event,
    )

    repo = WorkspaceRepository(session)
    ws = await _make_workspace(
        session,
        repo,
        status=WorkspaceStatus.destroyed,
        task_policy={},
        compose_project_name="awf_test_has_revoked",
    )
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )
    await session.commit()

    assert await has_terminal_runtime_released_event(session, ws.id) is True

    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        payload={"orphan_stop_error": "docker stop failed"},
    )
    await session.commit()

    assert await has_terminal_runtime_released_event(session, ws.id) is False, (
        "revoked release must cause has_terminal_runtime_released_event to return False"
    )


class TestCrossNodeAndEdgeCases:
    """Cross-node filtering, terminal release statuses, profile service ports,
    and pre-runtime terminal workspace tests for host-port conflict detection.
    """

    @pytest.mark.asyncio
    async def test_queued_workspace_not_cross_node_port_holder(
        self,
        session: AsyncSession,
    ) -> None:
        """A queued workspace with a reservation for node A must not block node B.

        When a workspace is still requested/queued, ``Workspace.node_id`` is
        NULL — the worker claim fills it later.  But the planned node is
        already recorded in ``ResourceReservation.node_id`` at create time.
        Treating ``Workspace.node_id IS NULL`` as matching all nodes would
        incorrectly report HOST_PORT_CONFLICT on node B for a workspace that
        is actually destined for node A, because Docker host ports are
        node-local.
        """
        repo = WorkspaceRepository(session)
        ws_a = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.requested,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=ws_a.repo_url,
            base_branch=ws_a.branch_base,
            title=ws_a.task_title,
            prompt=ws_a.task_prompt,
            external_id=None,
            idempotency_key=f"hostport-cross-node:{ws_a.id}",
            task_class=ws_a.task_class,
            owned_paths=list(ws_a.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=ws_a,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=ws_a.id,
            attempt_id=attempt.id,
            node_id="node-a",
            steady_cpu=1.0,
            steady_memory_gb=1.0,
            peak_cpu=2.0,
            peak_memory_gb=2.0,
            disk_mb=512,
            phase="active",
        )
        await session.commit()

        conflicts_b = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="node-b",
        )
        assert conflicts_b == []

        conflicts_a = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="node-a",
        )
        assert len(conflicts_a) == 1
        assert conflicts_a[0].host_port == 8080
        assert conflicts_a[0].workspace_id == ws_a.id

    @pytest.mark.asyncio
    async def test_terminal_workspace_with_runtime_released_not_blocking(
        self,
        session: AsyncSession,
    ) -> None:
        """A completed workspace whose runtime has been released does not block host ports.

        Once the terminal-runtime release sweep records a
        ``workspace.terminal_runtime_released`` event, the compose stack
        is gone and the host port is free for reuse.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.completed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_completed_released",
        )
        await repo.add_event(
            ws,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_destroying_workspace_is_blocking(self, session: AsyncSession) -> None:
        """A destroying workspace still holds host ports until cleanup finishes."""
        repo = WorkspaceRepository(session)
        existing_ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.destroying,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == existing_ws.id

    @pytest.mark.asyncio
    async def test_multiple_ports_one_companion(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        existing = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.ready,
            task_policy={
                "companions": [
                    {
                        "name": "svc",
                        "repo_url": "git@github.com:example/svc.git",
                        "ports": [[80, 8080], [443, 8443]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == existing.id

    @pytest.mark.asyncio
    async def test_multiple_companions(self, session: AsyncSession) -> None:
        """When the new request has two companions but only one collides."""
        repo = WorkspaceRepository(session)
        existing = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.provisioning,
            task_policy={
                "companions": [
                    {
                        "name": "redis",
                        "repo_url": "git@github.com:example/redis.git",
                        "ports": [[6379, 6379]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[6379, 8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 6379
        assert conflicts[0].workspace_id == existing.id

    @pytest.mark.asyncio
    async def test_idempotent_replay_no_collision(self, session: AsyncSession) -> None:
        """When the same workspace is the caller, it should not block itself."""
        repo = WorkspaceRepository(session)
        existing = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.requested,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        # The existing workspace itself should be excluded
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=existing.id,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_terminal_statuses_with_runtime_released_excluded(
        self,
        session: AsyncSession,
    ) -> None:
        """Terminal workspaces whose runtime has been released are excluded from port conflict detection.

        Once ``workspace.terminal_runtime_released`` is recorded, the
        compose stack is gone and the host port is free.  This applies to
        ``completed`` and ``destroyed`` workspaces alike.
        """
        repo = WorkspaceRepository(session)
        terminal_statuses = [
            WorkspaceStatus.completed,
            WorkspaceStatus.destroyed,
        ]
        for status in terminal_statuses:
            ws = await _make_workspace(
                session,
                repo,
                status=status,
                task_policy={
                    "companions": [
                        {
                            "name": "web",
                            "repo_url": "git@github.com:example/web.git",
                            "ports": [[80, 8080]],
                        }
                    ]
                },
                compose_project_name=f"awf_test_{status.value}_released",
            )
            await repo.add_event(
                ws,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
            )
        await session.commit()
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_failed_workspace_blocks_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """A failed workspace whose runtime has not been released blocks host ports.

        When a workspace fails after its compose stack has started, it transitions
        to ``failed`` without tearing the stack down. Because its containers may
        still hold host ports, ``find_host_port_conflicts`` must treat it as a
        port conflict so that a new create returns ``HOST_PORT_CONFLICT``
        instead of 202 followed by a compose-up failure.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_failed_blocks",
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == ws.id

    @pytest.mark.asyncio
    async def test_cancelled_workspace_blocks_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """A cancelled workspace retains its compose stack and blocks host ports.

        Like ``failed``, a ``cancelled`` workspace's containers may still hold
        host ports until cleanup/destroy releases them.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.cancelled,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_cancelled_blocks",
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == ws.id

    @pytest.mark.asyncio
    async def test_completed_workspace_without_release_blocks_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """A completed workspace whose runtime has not been released blocks host ports.

        A ``completed`` workspace may still have a running compose stack
        until the terminal-runtime release sweep tears it down.  Without
        a ``workspace.terminal_runtime_released`` event, the host port is
        still in use and must block new workspaces.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.completed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_completed_blocks",
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == ws.id

    @pytest.mark.asyncio
    async def test_failed_workspace_with_runtime_released_not_blocking(
        self,
        session: AsyncSession,
    ) -> None:
        """A failed workspace whose runtime has been released does not block host ports.

        After the terminal-runtime release sweep tears down the compose stack
        and records ``workspace.terminal_runtime_released``, the host port is
        free for reuse even though the workspace status remains ``failed``.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_failed_released",
        )
        await repo.add_event(
            ws,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_cancelled_workspace_with_runtime_released_not_blocking(
        self,
        session: AsyncSession,
    ) -> None:
        """A cancelled workspace whose runtime has been released does not block host ports."""
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.cancelled,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_cancelled_released",
        )
        await repo.add_event(
            ws,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_destroyed_workspace_without_release_blocks_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """A destroyed workspace whose runtime has not been released blocks host ports."""
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.destroyed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_destroyed_blocks",
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == ws.id

    @pytest.mark.asyncio
    async def test_multiple_conflicting_ports(self, session: AsyncSession) -> None:
        """Multiple host_ports mapping to different existing workspaces."""
        repo = WorkspaceRepository(session)
        ws1 = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={
                "companions": [
                    {
                        "name": "api",
                        "repo_url": "git@github.com:example/api.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        ws2 = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.validating,
            task_policy={
                "companions": [
                    {
                        "name": "worker",
                        "repo_url": "git@github.com:example/worker.git",
                        "ports": [[8080, 9090]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080, 9090],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 2
        conflict_ports = {c.host_port for c in conflicts}
        assert conflict_ports == {8080, 9090}
        conflict_ws_ids = {c.workspace_id for c in conflicts}
        assert ws1.id in conflict_ws_ids
        assert ws2.id in conflict_ws_ids

    @pytest.mark.asyncio
    async def test_no_companions_in_existing_workspace(self, session: AsyncSession) -> None:
        """An existing workspace without companions should not cause conflicts."""
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={},
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_companion_without_ports(self, session: AsyncSession) -> None:
        """An existing companion with no ports should not block."""
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={
                "companions": [
                    {
                        "name": "worker",
                        "repo_url": "git@github.com:example/worker.git",
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_empty_host_ports_query(self, session: AsyncSession) -> None:
        """If the new request has no host_ports, there are no conflicts."""
        repo = WorkspaceRepository(session)
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_profile_service_port_conflict(self, session: AsyncSession) -> None:
        """Profile service host ports must be included in the conflict scan.

        An existing workspace whose resolved_profile includes a service
        with a host port should block a new workspace requesting the same
        host port, even if the existing workspace has no companions.
        """
        repo = WorkspaceRepository(session)
        existing_ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            resolved_profile={
                "name": "test-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 5432]],
                    }
                ],
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[5432],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 5432
        assert conflicts[0].workspace_id == existing_ws.id

    @pytest.mark.asyncio
    async def test_profile_service_no_conflict(self, session: AsyncSession) -> None:
        """Profile service with a different host port should not block."""
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            resolved_profile={
                "name": "test-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 5432]],
                    }
                ],
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[9090],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_both_companion_and_profile_service_ports_scanned(
        self,
        session: AsyncSession,
    ) -> None:
        """Both companion and profile service ports from the same workspace are scanned."""
        repo = WorkspaceRepository(session)
        existing = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            resolved_profile={
                "name": "test-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 5432]],
                    }
                ],
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080, 5432],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 2
        conflict_ports = {c.host_port for c in conflicts}
        assert conflict_ports == {8080, 5432}
        assert all(c.workspace_id == existing.id for c in conflicts)

    @pytest.mark.asyncio
    async def test_duplicate_port_in_companion_and_profile_deduped(
        self,
        session: AsyncSession,
    ) -> None:
        """Same host port in companion and profile service produces one conflict, not two."""
        repo = WorkspaceRepository(session)
        existing = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 5432]],
                    }
                ]
            },
            resolved_profile={
                "name": "test-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 5432]],
                    }
                ],
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[5432],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 5432
        assert conflicts[0].workspace_id == existing.id

    @pytest.mark.asyncio
    async def test_profile_service_without_ports_no_conflict(
        self,
        session: AsyncSession,
    ) -> None:
        """A profile service with no ports should not cause conflicts."""
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
            resolved_profile={
                "name": "test-profile",
                "services": [
                    {
                        "name": "worker",
                        "image": "worker:latest",
                    }
                ],
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_no_resolved_profile_no_conflict(self, session: AsyncSession) -> None:
        """An existing workspace with no resolved_profile should not cause conflicts from services."""
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.running,
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_terminal_workspace_wrong_reason_code_still_blocking(
        self,
        session: AsyncSession,
    ) -> None:
        """A terminal workspace with a terminal_runtime_released event of the right
        event_type but wrong reason_code still blocks host ports.

        The conflict-check query must match on both event_type AND reason_code
        to stay semantically identical to the worker cleanup sweep.  If an
        event is recorded with the correct event_type but a different
        reason_code, the port must still be considered in use.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.completed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_test_wrong_reason",
        )
        await repo.add_event(
            ws,
            event_type="workspace.terminal_runtime_released",
            reason_code="SOME_OTHER_REASON",
        )
        await session.commit()
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == ws.id

    @pytest.mark.asyncio
    async def test_failed_pre_runtime_no_compose_project_not_blocking(
        self,
        session: AsyncSession,
    ) -> None:
        """A failed workspace with no compose_project_name does not block host ports.

        When a workspace fails during provisioning before the compose stack
        is launched (e.g. git failure, profile resolution failure, egress
        policy failure), ``compose_project_name`` stays NULL because no
        container ever bound the host port.  Such pre-runtime terminal
        workspaces must not block port reuse even without a
        ``terminal_runtime_released`` event.
        """
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_cancelled_pre_runtime_no_compose_project_not_blocking(
        self,
        session: AsyncSession,
    ) -> None:
        """A cancelled workspace with no compose_project_name does not block host ports.

        A workspace cancelled before the compose stack launched never bound
        a host port, so it must not block port reuse.
        """
        repo = WorkspaceRepository(session)
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.cancelled,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_legacy_terminal_null_node_id_with_released_reservation_blocks_same_node(
        self,
        session: AsyncSession,
    ) -> None:
        """A terminal workspace with null node_id and released reservation still blocks its node.

        Legacy rows created before the provisioner started stamping
        ``Workspace.node_id`` on failure can have
        ``compose_project_name IS NOT NULL`` and
        ``Workspace.node_id IS NULL``.  After ``transition()`` releases
        the active reservation, the active-reservation subquery returns
        NULL.  The ``latest_reservation_node`` fallback must pick up the
        released reservation's node_id so that the workspace is still
        detected on its actual node, preventing a compose-up collision
        on an upgraded single-node install.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_legacy_null_node",
        )
        ws.node_id = None
        await session.flush()
        task = await TaskRepository(session).create_or_get(
            repo_url=ws.repo_url,
            base_branch=ws.branch_base,
            title=ws.task_title,
            prompt=ws.task_prompt,
            external_id=None,
            idempotency_key=f"hostport-legacy-null-node:{ws.id}",
            task_class=ws.task_class,
            owned_paths=list(ws.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=ws,
        )
        res_repo = ResourceReservationRepository(session)
        await res_repo.create(
            workspace_id=ws.id,
            attempt_id=attempt.id,
            node_id="node-a",
            steady_cpu=1.0,
            steady_memory_gb=1.0,
            peak_cpu=2.0,
            peak_memory_gb=2.0,
            disk_mb=512,
            phase="active",
        )
        await res_repo.release_active_for_workspace(ws.id)
        await session.commit()

        conflicts_a = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="node-a",
        )
        assert len(conflicts_a) == 1
        assert conflicts_a[0].host_port == 8080
        assert conflicts_a[0].workspace_id == ws.id

        conflicts_b = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="node-b",
        )
        assert conflicts_b == []

    @pytest.mark.asyncio
    async def test_legacy_terminal_no_reservation_included_via_null_node_fallback(
        self,
        session: AsyncSession,
    ) -> None:
        """A legacy terminal workspace with no node_id and no reservation is included on all nodes.

        Legacy rows created before the provisioner started stamping
        ``Workspace.node_id`` and before the reservation system existed
        can have ``compose_project_name IS NOT NULL`` but
        ``Workspace.node_id IS NULL`` and zero ``ResourceReservation``
        rows.  The null-node fallback clause must include such rows
        when *node_id* is provided so that their unreleased Docker
        stack is not missed by admission on an upgraded install.
        """
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy={
                "companions": [
                    {
                        "name": "web",
                        "repo_url": "git@github.com:example/web.git",
                        "ports": [[80, 8080]],
                    }
                ]
            },
            compose_project_name="awf_legacy_no_reservation",
        )
        ws.node_id = None
        await session.commit()

        conflicts_a = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="local",
        )
        assert len(conflicts_a) == 1
        assert conflicts_a[0].host_port == 8080
        assert conflicts_a[0].workspace_id == ws.id

        conflicts_b = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="other-node",
        )
        assert len(conflicts_b) == 1
