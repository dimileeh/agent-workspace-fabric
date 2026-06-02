"""PR monitor adoption service tests — forge-support gate (issue #345).

Split out of ``test_pr_monitor_adoption_part_002`` to keep that module under the
first-party line limit. These cases reuse the adoption scaffolding defined in
part 002 (``_MetadataFetcher``, ``_metadata``, ``_count``) and cover the forge
detection / FORGE_NOT_SUPPORTED rejection paths plus the legacy
``forge``-less-snapshot replay attach.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from tests.postgres import postgres_test_engine
from tests.unit.service.test_pr_monitor_adoption_parts.test_pr_monitor_adoption_part_002 import (
    _count,
    _metadata,
    _MetadataFetcher,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestPullRequestMonitorAdoptionForgeGate:
    @pytest.mark.unit
    async def test_replay_with_legacy_inline_profile_missing_forge_attaches(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An adopted workspace persisted before the ``forge`` field (issue #345)
        has a requested_profile without the key; an identical replay now dumps
        ``forge="auto"`` and must still attach to the existing monitor rather than
        raise a spurious inline-profile policy conflict."""
        inline_profile = {
            "name": "inline-a",
            "monitor": {"initial_review_grace_period_seconds": 10},
        }
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    profile=inline_profile,
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            assert workspace.requested_profile is not None
            assert workspace.requested_profile.get("forge") == "auto"
            workspace.requested_profile = {
                key: value for key, value in workspace.requested_profile.items() if key != "forge"
            }
            await session.commit()

        async with factory() as session:
            replay = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    profile=inline_profile,
                )
            )
            await session.commit()

        assert replay.attached_existing is True
        assert replay.workspace_id == first.workspace_id

    @pytest.mark.unit
    async def test_bitbucket_repo_url_rejected_before_metadata_fetch(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Regression: forge detection (issue #345) makes ``RepoRef.from_url``
        # accept a ``bitbucket.org`` URL as ``RepoRef(forge="bitbucket")``.
        # Adoption must fail fast on the unsupported forge BEFORE fetching
        # metadata — otherwise the GitHub-only ``gh pr view --repo owner/repo``
        # path silently queries GitHub for the same slug and can adopt the wrong
        # PR (the executor forge gate runs too late, after this metadata fetch).
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_url="https://bitbucket.org/workspace/repo",
                        pr_number=277,
                    )
                )

            assert await _count(session, Workspace) == 0

        assert excinfo.value.error_code == "FORGE_NOT_SUPPORTED"
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == {"repo_slug": "workspace/repo", "forge": "bitbucket"}
        assert "BitBucket forge support is not yet implemented" in excinfo.value.message
        assert fetcher.calls == []

    @pytest.mark.unit
    async def test_bitbucket_pr_url_rejected_with_forge_not_supported(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Regression: ``parse_github_pull_request_url`` rejects any non-github.com
        # host with a bare ``ValueError`` (read as PR_ADOPTION_INPUT_REQUIRED), so a
        # BitBucket ``pr_url`` would otherwise never reach the forge gate. The
        # ``_PR_ADOPTION_ERROR_CODE_CONTRACT`` documents FORGE_NOT_SUPPORTED as
        # reachable here, so a well-formed BitBucket PR URL must surface it (the
        # same code as the ``repo_url``/``repo_slug`` path), not the generic input
        # error — and never fetch metadata against the wrong forge.
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        pr_url="https://bitbucket.org/workspace/repo/pull-requests/42",
                    )
                )

            assert await _count(session, Workspace) == 0

        assert excinfo.value.error_code == "FORGE_NOT_SUPPORTED"
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == {"repo_slug": "workspace/repo", "forge": "bitbucket"}
        assert "BitBucket forge support is not yet implemented" in excinfo.value.message
        assert fetcher.calls == []

    @pytest.mark.unit
    async def test_github_pr_url_with_same_slug_bitbucket_repo_url_rejected(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Regression: a GitHub ``pr_url`` paired with a ``bitbucket.org``
        # ``repo_url`` of the SAME owner/repo slug must be rejected up front.
        # The canonical ref parsed from the PR URL is GitHub (supported), so the
        # canonical-only forge gate passes; identity-conflict detection must also
        # compare forge or the Bitbucket URL is persisted and the executor forge
        # gate fails the workspace too late (before any metadata fetch).
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        pr_url="https://github.com/dimileeh/aira-web/pull/277",
                        repo_url="https://bitbucket.org/dimileeh/aira-web",
                    )
                )

            assert await _count(session, Workspace) == 0

        assert excinfo.value.error_code == "PR_ADOPTION_INPUT_REQUIRED"
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == {
            "expected_repo_slug": "dimileeh/aira-web",
            "actual_repo_slug": "dimileeh/aira-web",
            "expected_forge": "github",
            "actual_forge": "bitbucket",
            "field": "repo_url",
        }
        assert fetcher.calls == []
