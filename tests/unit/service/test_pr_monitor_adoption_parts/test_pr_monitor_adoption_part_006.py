"""PR monitor adoption terminal lineage key tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from tests.postgres import postgres_test_engine
from tests.unit.service.test_pr_monitor_adoption_parts.test_pr_monitor_adoption_part_002 import (
    _metadata,
    _MetadataFetcher,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestPullRequestMonitorAdoptionTerminalLineageKeys:
    @pytest.mark.unit
    async def test_terminal_prior_reuses_canonical_key_despite_unrelated_generated_key(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        logical_key = adoption_module.pr_adoption_idempotency_key(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )

        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            await WorkspaceRepository(session).create(
                repo_url="git@github.com:dimileeh/unrelated.git",
                branch_base="main",
                task_title="unrelated stale adoption key",
                task_prompt="stale key collision fixture",
                task_external_id="unrelated-stale-adoption-key",
                agent="codex",
                test_commands=[],
                idempotency_key=f"{logical_key}:g1",
            )
            await session.commit()

        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert result.attached_existing is False
        assert result.workspace_id != first.workspace_id

        async with factory() as session:
            fresh_workspace = await WorkspaceRepository(session).get(result.workspace_id)

        assert fresh_workspace is not None
        assert fresh_workspace.idempotency_key == logical_key
