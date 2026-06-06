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
from awf.common.github_client import RepoRef
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.pr_monitor_adoption import (
    PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY,
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
    _default_metadata_fetcher,
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
    async def test_bitbucket_repo_url_passes_forge_gate_after_part2_flip(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Part 2 flip (issue #345): bitbucket is now a supported forge, so the
        # adoption forge gate no longer rejects a ``bitbucket.org`` repo_url — the
        # request proceeds to the metadata fetch and creates a workspace.
        #
        # FLAGGED GAP (validation doc / PR): the *default* adoption metadata fetcher
        # (``fetch_pull_request_adoption_metadata``) is GitHub-only (``gh pr view``).
        # A real bitbucket adoption via the default fetcher would mis-route to
        # GitHub; a BitBucket-aware adoption metadata fetcher is follow-up work
        # outside the 10-method ForgeClient surface. The injected fetcher here
        # stands in for that future BitBucket path.
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            result = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_url="https://bitbucket.org/workspace/repo",
                    pr_number=277,
                )
            )
            await session.commit()
            assert result.workspace_id
            assert await _count(session, Workspace) == 1
        # The forge gate passed (no FORGE_NOT_SUPPORTED) and metadata was fetched.
        assert fetcher.calls

    @pytest.mark.unit
    async def test_default_metadata_fetcher_rejects_non_github_forge(self) -> None:
        # The *default* (production) adoption metadata fetcher shells
        # ``gh pr view --repo owner/repo``, which always targets github.com. After
        # the Part 2 gate flip a ``bitbucket.org`` repo passes the adoption forge
        # gate, so the default fetcher must fail closed for a non-GitHub forge
        # rather than silently querying GitHub for the same owner/repo slug. A
        # BitBucket-aware adoption metadata fetcher must be injected explicitly.
        with pytest.raises(PRMonitorAdoptionError) as excinfo:
            await _default_metadata_fetcher(
                repo=RepoRef.from_url("https://bitbucket.org/workspace/repo"),
                pr_number=277,
            )

        assert excinfo.value.error_code == PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == {"repo_slug": "workspace/repo", "forge": "bitbucket"}

    @pytest.mark.unit
    async def test_bitbucket_pr_url_falls_back_to_input_required(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Part 2 flip: a bitbucket ``pr_url`` is no longer rejected with
        # FORGE_NOT_SUPPORTED. AWF has no BitBucket PR-URL parser
        # (``parse_github_pull_request_url`` is github.com-only), so a well-formed
        # bitbucket PR URL now surfaces PR_ADOPTION_INPUT_REQUIRED — provide
        # ``repo_url``/``repo_slug`` + ``pr_number`` for BitBucket adoption instead.
        # (A BitBucket PR-URL parser is follow-up work, flagged in the PR.)
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

        assert excinfo.value.error_code == "PR_ADOPTION_INPUT_REQUIRED"
        assert excinfo.value.status_code == 422
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
