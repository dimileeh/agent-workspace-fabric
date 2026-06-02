"""Legacy host-port holder tests for WorkspaceRepository."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from tests.postgres import postgres_test_session
from tests.unit.db.test_workspace_repository_parts.test_workspace_repository_host_port_collision import (
    _make_workspace,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


class TestLegacyHostPortHolders:
    @pytest.mark.asyncio
    async def test_legacy_null_node_id_with_released_reservation_blocks_same_node(
        self,
        session: AsyncSession,
    ) -> None:
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
    async def test_legacy_no_reservation_included_via_null_node_fallback(
        self,
        session: AsyncSession,
    ) -> None:
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
