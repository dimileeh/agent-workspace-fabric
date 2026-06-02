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
from awf.db.repositories.base import (
    PRE_LAUNCH_FAILURE_EVENT_TYPE,
    PRE_LAUNCH_FAILURE_REASON_CODE,
)
from tests.postgres import postgres_test_session
from tests.unit.db.test_workspace_repository_parts.test_workspace_repository_host_port_collision import (
    _make_workspace,
    _reserve_workspace_node,
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

    @pytest.mark.asyncio
    async def test_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """Legacy terminal rows with null runtime metadata may still hold host ports."""
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
            resolved_profile={
                "name": "legacy-null-runtime-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 15432]],
                    }
                ],
            },
        )
        ws.node_id = None
        ws.compose_project_name = None
        ws.compose_file_path = None
        await session.commit()

        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080, 15432],
            excluding_workspace_id=None,
            node_id="local",
        )

        assert sorted((conflict.host_port, conflict.workspace_id) for conflict in conflicts) == [
            (8080, ws.id),
            (15432, ws.id),
        ]

    @pytest.mark.asyncio
    async def test_node_stamped_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """Node-stamped legacy null-runtime rows may still hold ports on that node."""
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            node_id="node-a",
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
                "name": "node-stamped-legacy-null-runtime-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 15432]],
                    }
                ],
            },
        )
        ws.compose_project_name = None
        ws.compose_file_path = None
        await session.commit()

        conflicts_a = await repo.find_host_port_conflicts(
            host_ports=[8080, 15432],
            excluding_workspace_id=None,
            node_id="node-a",
        )
        assert sorted((conflict.host_port, conflict.workspace_id) for conflict in conflicts_a) == [
            (8080, ws.id),
            (15432, ws.id),
        ]

        conflicts_b = await repo.find_host_port_conflicts(
            host_ports=[8080, 15432],
            excluding_workspace_id=None,
            node_id="node-b",
        )
        assert conflicts_b == []

    @pytest.mark.asyncio
    async def test_reserved_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """Reserved legacy null-compose rows may still hold ports after launch."""
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
            resolved_profile={
                "name": "reserved-legacy-null-runtime-profile",
                "services": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "ports": [[5432, 15432]],
                    }
                ],
            },
        )
        ws.node_id = None
        ws.compose_project_name = None
        ws.compose_file_path = None
        await _reserve_workspace_node(session, ws, node_id="node-a", release=True)

        conflicts_a = await repo.find_host_port_conflicts(
            host_ports=[8080, 15432],
            excluding_workspace_id=None,
            node_id="node-a",
        )
        assert sorted((conflict.host_port, conflict.workspace_id) for conflict in conflicts_a) == [
            (8080, ws.id),
            (15432, ws.id),
        ]

        conflicts_b = await repo.find_host_port_conflicts(
            host_ports=[8080, 15432],
            excluding_workspace_id=None,
            node_id="node-b",
        )
        assert conflicts_b == []

    @pytest.mark.asyncio
    async def test_pre_launch_reserved_null_runtime_metadata_does_not_block_host_ports(
        self,
        session: AsyncSession,
    ) -> None:
        """Explicit pre-launch evidence lets reserved null-compose rows reuse ports."""
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            node_id="node-a",
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
        ws.compose_project_name = None
        ws.compose_file_path = None
        await repo.add_event(
            ws,
            event_type=PRE_LAUNCH_FAILURE_EVENT_TYPE,
            reason_code=PRE_LAUNCH_FAILURE_REASON_CODE,
        )
        await _reserve_workspace_node(session, ws, node_id="node-a")

        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
            node_id="node-a",
        )
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_compose_project_name_null_invariant_distinguishes_port_holders(
        self,
        session: AsyncSession,
    ) -> None:
        """Modern null-runtime rows are distinct from real port holders."""
        repo = WorkspaceRepository(session)
        companions = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 8080]],
                }
            ]
        }
        await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy=companions,
            node_id="node-a",
        )
        holder = await _make_workspace(
            session,
            repo,
            status=WorkspaceStatus.failed,
            task_policy=companions,
            compose_project_name="awf_invariant_holder",
            node_id="node-a",
        )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 1, (
            "only the workspace that actually launched a stack must hold the port"
        )
        assert conflicts[0].host_port == 8080
        assert conflicts[0].workspace_id == holder.id
