"""PR monitor adoption terminal task generation tests."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.db.enums import WorkspaceStatus
from awf.db.models import Task
from awf.db.repositories import WorkspaceRepository
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from tests.unit.service.test_pr_monitor_adoption_parts.test_pr_monitor_adoption_part_001 import (
    _count,
    _metadata,
    _MetadataFetcher,
    factory,
)

_IMPORTED_FIXTURES = (factory,)


class TestPullRequestMonitorAdoptionTerminalTaskGeneration:
    @pytest.mark.unit
    async def test_terminal_prior_with_stale_task_idempotency_uses_task_generation_key(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        logical_key = adoption_module.pr_adoption_idempotency_key(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        logical_task_external_id = adoption_module._adoption_external_id(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )

        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: ready")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            assert old_workspace is not None
            assert old_task is not None
            assert old_workspace.idempotency_key == logical_key
            assert old_task.idempotency_key == logical_key
            superseded_external_id = adoption_module._superseded_adoption_external_id(
                external_id=logical_task_external_id,
                workspace_id=first.workspace_id,
            )
            old_workspace.status = WorkspaceStatus.destroyed.value
            old_workspace.idempotency_key = None
            old_workspace.task_external_id = superseded_external_id
            old_task.external_id = superseded_external_id
            await session.commit()

        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: retitled")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert result.attached_existing is False
        assert result.workspace_id != first.workspace_id
        assert result.task_id != first.task_id

        async with factory() as session:
            assert await _count(session, Task) == 2
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            fresh_workspace = await WorkspaceRepository(session).get(result.workspace_id)
            fresh_task = await session.get(Task, result.task_id)

            assert old_workspace is not None
            assert old_task is not None
            assert fresh_workspace is not None
            assert fresh_task is not None
            assert old_workspace.idempotency_key is None
            assert old_task.idempotency_key == logical_key
            assert old_task.external_id == adoption_module._superseded_adoption_external_id(
                external_id=logical_task_external_id,
                workspace_id=first.workspace_id,
            )
            assert fresh_workspace.idempotency_key == logical_key
            assert fresh_workspace.task_external_id == f"{logical_task_external_id}:g1"
            assert fresh_task.external_id == fresh_workspace.task_external_id
            assert fresh_task.idempotency_key == f"{logical_key}:g1"
            assert fresh_task.title == "feature: retitled"

    @pytest.mark.unit
    async def test_terminal_prior_reserves_generated_task_external_id_without_task_key(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        logical_key = adoption_module.pr_adoption_idempotency_key(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        logical_task_external_id = adoption_module._adoption_external_id(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )

        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: ready")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            first_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            first_task = await session.get(Task, first.task_id)
            assert first_workspace is not None
            assert first_task is not None
            first_workspace.status = WorkspaceStatus.destroyed.value
            first_workspace.idempotency_key = None
            first_task.idempotency_key = logical_key
            await session.commit()

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: retitled")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            second_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            second_task = await session.get(Task, second.task_id)
            assert second_workspace is not None
            assert second_task is not None
            assert second_workspace.idempotency_key == logical_key
            assert second_task.external_id == f"{logical_task_external_id}:g1"
            second_workspace.status = WorkspaceStatus.destroyed.value
            second_workspace.idempotency_key = None
            second_task.idempotency_key = None
            await session.commit()

        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: retitled again")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert result.attached_existing is False
        assert result.task_id not in {first.task_id, second.task_id}

        async with factory() as session:
            assert await _count(session, Task) == 3
            fresh_workspace = await WorkspaceRepository(session).get(result.workspace_id)
            fresh_task = await session.get(Task, result.task_id)

            assert fresh_workspace is not None
            assert fresh_task is not None
            assert fresh_workspace.idempotency_key == logical_key
            assert fresh_workspace.task_external_id == f"{logical_task_external_id}:g2"
            assert fresh_task.external_id == fresh_workspace.task_external_id
            assert fresh_task.idempotency_key == f"{logical_key}:g2"
            assert fresh_task.title == "feature: retitled again"
