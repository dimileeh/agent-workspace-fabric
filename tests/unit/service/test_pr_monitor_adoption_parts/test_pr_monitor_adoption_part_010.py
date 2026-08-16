"""PR monitor adoption — legacy idempotency replay requires persisted PR identity.

Hardens the ``bd202bc84`` task-kind-only fallback: a ``sync_feature_pr`` row that
preclaims the canonical adoption key must not attach without independent
``pr_url`` / ``pr_number`` / ``repo_url`` proof. Cross-PR history protection from
``3607441de`` stays unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from tests.postgres import postgres_test_engine
from tests.unit.service.test_pr_monitor_adoption_parts.test_pr_monitor_adoption_part_001 import (
    _metadata,
    _MetadataFetcher,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _canonical_key(*, repo_slug: str = "dimileeh/aira-web", pr_number: int = 277) -> str:
    return adoption_module.pr_adoption_idempotency_key(
        repo_slug=repo_slug,
        pr_number=pr_number,
    )


def _identity_conflict_detail(*, workspace_id: str) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "repo_slug": "dimileeh/aira-web",
        "pr_number": 277,
        "existing_task_kind": adoption_module.PR_ADOPTION_TASK_KIND,
        "existing_pr_adoption_repo_slug": None,
        "existing_pr_adoption_pr_number": None,
    }


async def _preclaim_sync_feature_pr(
    session: AsyncSession,
    *,
    repo_url: str = "https://github.com/dimileeh/aira-web.git",
    pr_url: str | None = None,
    pr_number: int | None = None,
    task_policy: Any = None,
) -> Workspace:
    """Ordinary sync_feature_pr workspace owning the canonical adoption key.

    Policy fields match a default adopt replay (generated external id, codex,
    auto profile, legacy auto_merge=True) so a task-kind-only fallback would
    attach despite missing/wrong persisted PR identity.
    """
    workspace = await WorkspaceRepository(session).create(
        repo_url=repo_url,
        branch_base="development",
        task_title="ordinary sync_feature_pr",
        task_prompt="Not a PR adoption.",
        agent="codex",
        test_commands=[],
        idempotency_key=_canonical_key(),
        task_policy={},
        profile_ref="auto",
        task_kind=adoption_module.PR_ADOPTION_TASK_KIND,
        task_external_id=adoption_module._adoption_external_id(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        ),
        auto_merge=True,
    )
    workspace.task_policy = task_policy  # type: ignore[assignment]
    workspace.pr_url = pr_url
    workspace.pr_number = pr_number
    await session.flush()
    return workspace


class TestLegacyAdoptionPersistedPrIdentity:
    @pytest.mark.unit
    async def test_non_adoption_sync_feature_pr_canonical_key_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """task_kind alone must not attach a preclaimed canonical key."""
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            workspace = await _preclaim_sync_feature_pr(session, task_policy=None)
            workspace_id = workspace.id
            await session.commit()

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=fetcher,
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == _identity_conflict_detail(workspace_id=workspace_id)
        assert fetcher.calls == []

    @pytest.mark.unit
    async def test_same_repo_wrong_pr_persisted_identity_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            workspace = await _preclaim_sync_feature_pr(
                session,
                task_policy="legacy",
                pr_url="https://github.com/dimileeh/aira-web/pull/999",
                pr_number=999,
            )
            workspace_id = workspace.id
            await session.commit()

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=fetcher,
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == _identity_conflict_detail(workspace_id=workspace_id)
        assert fetcher.calls == []

    @pytest.mark.unit
    async def test_wrong_repo_same_pr_persisted_identity_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            workspace = await _preclaim_sync_feature_pr(
                session,
                repo_url="https://github.com/other-org/other-repo.git",
                task_policy=None,
                pr_url="https://github.com/other-org/other-repo/pull/277",
                pr_number=277,
            )
            workspace_id = workspace.id
            await session.commit()

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=fetcher,
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == _identity_conflict_detail(workspace_id=workspace_id)
        assert fetcher.calls == []

    @pytest.mark.unit
    async def test_malformed_persisted_pr_url_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            workspace = await _preclaim_sync_feature_pr(
                session,
                task_policy=None,
                pr_url="not-a-valid-pr-url",
                pr_number=277,
            )
            workspace_id = workspace.id
            await session.commit()

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=fetcher,
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == _identity_conflict_detail(workspace_id=workspace_id)
        assert fetcher.calls == []

    @pytest.mark.unit
    @pytest.mark.parametrize("legacy_task_policy", [None, "legacy"])
    async def test_genuine_legacy_replay_still_attaches_with_persisted_identity(
        self,
        factory: async_sessionmaker[AsyncSession],
        legacy_task_policy: object,
    ) -> None:
        metadata = _metadata()
        fetcher = _MetadataFetcher(metadata)

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            workspace.task_policy = legacy_task_policy  # type: ignore[assignment]
            workspace.auto_merge = True

            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id
        assert fetcher.calls == [("dimileeh/aira-web", 277)]
