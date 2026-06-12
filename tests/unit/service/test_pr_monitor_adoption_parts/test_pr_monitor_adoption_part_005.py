"""PR monitor adoption service tests — task-tag policy conflicts (#526).

Split out of ``test_pr_monitor_adoption_part_001`` to keep that module under the
first-party line limit. These cases reuse the adoption scaffolding defined in
part 002 (``_MetadataFetcher``, ``_metadata``) and cover the task_tag mismatch
gate on adoption replay (reject divergent tag, attach on matching tag).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.db.session import make_session_factory
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from tests.postgres import postgres_test_engine
from tests.unit.service.test_pr_monitor_adoption_parts.test_pr_monitor_adoption_part_002 import (
    _metadata,
    _MetadataFetcher,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestPullRequestMonitorAdoptionTaskTagPolicy:
    @pytest.mark.unit
    async def test_attaching_live_adoption_rejects_different_task_tag(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        task_tag="PROJ-123",
                    )
                )

        assert first.workspace_id.startswith("ws_")
        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": first.workspace_id,
            "existing_task_tag": None,
            "requested_task_tag": "PROJ-123",
        }

    @pytest.mark.unit
    async def test_attaching_live_adoption_allows_matching_task_tag(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    task_tag="PROJ-123",
                )
            )
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    task_tag="PROJ-123",
                )
            )
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id
