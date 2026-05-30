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
from awf.db.repositories import WorkspaceRepository
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
) -> Workspace:
    ws = await repo.create(
        repo_url=_H2,
        branch_base=_B,
        task_title="host-port test",
        task_prompt="test host-port collision detection",
        agent="codex",
        test_commands=[],
        task_policy=task_policy or {},
    )
    ws.status = status.value
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
    async def test_terminal_workspace_not_blocking(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        await _make_workspace(
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
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

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
            host_ports=[6379],
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
    async def test_all_terminal_statuses_excluded(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        terminal_statuses = [
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroying,
            WorkspaceStatus.destroyed,
        ]
        for status in terminal_statuses:
            await _make_workspace(
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
            )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert conflicts == []

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
