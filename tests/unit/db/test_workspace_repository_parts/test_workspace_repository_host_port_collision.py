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
