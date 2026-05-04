"""PR monitor adoption service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    PullRequestMetadataError,
    RepoRef,
)
from awf.db.base import Base
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.models import (
    Operation,
    QueueDecision,
    ResourceReservation,
    Task,
    TaskAttempt,
    Workspace,
)
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _metadata(
    *,
    number: int = 277,
    state: str = "OPEN",
    head_ref: str = "feature/ready",
    base_ref: str = "development",
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
    merged: bool | None = None,
    closed: bool | None = None,
) -> PullRequestAdoptionMetadata:
    merged_value = state == "MERGED" if merged is None else merged
    closed_value = state == "CLOSED" if closed is None else closed
    return PullRequestAdoptionMetadata(
        number=number,
        head_ref=head_ref,
        base_ref=base_ref,
        head_sha=head_sha,
        base_sha=base_sha,
        state=state,
        is_draft=False,
        closed=closed_value,
        merged=merged_value,
        author="octocat",
        url=f"https://github.com/dimileeh/aira-web/pull/{number}",
        title="feature: ready",
    )


class _MetadataFetcher:
    def __init__(self, metadata: PullRequestAdoptionMetadata) -> None:
        self.metadata = metadata
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, *, repo: RepoRef, pr_number: int) -> PullRequestAdoptionMetadata:
        self.calls.append((repo.slug(), pr_number))
        return self.metadata


async def _count(session: AsyncSession, model: type[Any]) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


class TestPullRequestMonitorAdoptionService:
    @pytest.mark.unit
    async def test_creates_lineage_and_monitor_owned_request(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                    auto_merge=False,
                    initial_review_grace_period_seconds=12,
                    reason="recover external PR",
                )
            )
            await session.commit()

        assert result.attached_existing is False
        assert result.status == WorkspaceStatus.requested
        assert result.repo_slug == "dimileeh/aira-web"
        assert result.pr_number == 277
        assert result.pr_url == "https://github.com/dimileeh/aira-web/pull/277"
        assert result.head_ref == "feature/ready"
        assert result.base_ref == "development"
        assert result.head_sha == "h" * 40
        assert result.base_sha == "b" * 40
        assert result.auto_merge is False
        assert result.task_id is not None
        assert result.attempt_id is not None
        assert result.candidate_id is None
        assert result.validation_provenance.freshness_status == "unavailable"
        assert result.logs_url.endswith(f"/v1/workspaces/{result.workspace_id}/logs")

        async with factory() as session:
            assert await _count(session, Workspace) == 1
            assert await _count(session, Task) == 1
            assert await _count(session, TaskAttempt) == 1
            assert await _count(session, ResourceReservation) == 1
            assert await _count(session, QueueDecision) == 1
            assert await _count(session, Operation) == 1

            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            assert workspace.task_title == "feature: ready"
            assert workspace.task_kind == "sync_feature_pr"
            assert workspace.pr_url == "https://github.com/dimileeh/aira-web/pull/277"
            assert workspace.pr_number == 277
            assert workspace.monitor_last_commit_sha == "h" * 40
            assert workspace.auto_merge is False
            assert workspace.initial_review_grace_period_seconds == 12
            assert workspace.task_policy["pr_adoption"] == {
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "pr_url": "https://github.com/dimileeh/aira-web/pull/277",
                "head_ref": "feature/ready",
                "base_ref": "development",
                "head_sha": "h" * 40,
                "base_sha": "b" * 40,
                "state": "OPEN",
                "is_draft": False,
                "author": "octocat",
                "title": "feature: ready",
                "operator_reason": "recover external PR",
                "source": "existing_github_pr",
            }
            assert any(
                event.event_type == "workspace.pr_monitor_adoption_requested"
                and event.reason_code == "PR_MONITOR_ADOPTION_REQUESTED"
                for event in workspace.events
            )

            operations = list((await session.execute(select(Operation))).scalars())
            assert operations[0].type == OperationType.adopt_pr.value
            assert operations[0].status == "succeeded"

            task = (await session.execute(select(Task))).scalar_one()
            assert task.title == "feature: ready"

    @pytest.mark.unit
    async def test_idempotent_per_repo_pr_across_slug_and_url(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    pr_url="https://github.com/dimileeh/aira-web/pull/277"
                )
            )
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id
        assert second.task_id == first.task_id
        assert second.attempt_id == first.attempt_id
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            assert await _count(session, Workspace) == 1
            assert await _count(session, TaskAttempt) == 1
            assert await _count(session, Operation) == 1

    @pytest.mark.unit
    async def test_replay_with_changed_monitor_policy_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=False,
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        auto_merge=True,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "initial_kwargs, replay_kwargs, expected_detail",
        [
            (
                {"agent": "codex"},
                {"agent": "claude_code"},
                {
                    "existing_agent": "codex",
                    "requested_agent": "claude_code",
                },
            ),
            (
                {"profile_ref": "auto"},
                {"profile_ref": "python"},
                {
                    "existing_profile_ref": "auto",
                    "requested_profile_ref": "python",
                },
            ),
        ],
    )
    async def test_replay_with_changed_agent_or_profile_policy_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
        initial_kwargs: dict[str, object],
        replay_kwargs: dict[str, object],
        expected_detail: dict[str, object],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    **initial_kwargs,
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        **replay_kwargs,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": excinfo.value.detail["workspace_id"],
            **expected_detail,
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "metadata, error_code",
        [
            (_metadata(state="CLOSED"), "PR_ALREADY_CLOSED"),
            (_metadata(state="MERGED"), "PR_ALREADY_MERGED"),
        ],
    )
    async def test_terminal_pr_rejection_does_not_create_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
        metadata: PullRequestAdoptionMetadata,
        error_code: str,
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(metadata),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )

            assert excinfo.value.error_code == error_code
            assert await _count(session, Workspace) == 0

    @pytest.mark.unit
    async def test_missing_identity_raises_structured_input_error(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(PullRequestMonitorAdoptionRequest())

            assert excinfo.value.error_code == "PR_ADOPTION_INPUT_REQUIRED"
            assert await _count(session, Workspace) == 0

    @pytest.mark.unit
    async def test_metadata_fetch_error_maps_to_structured_adoption_error(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async def _failing_fetcher(
            *,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            raise PullRequestMetadataError(
                reason_code="PR_NOT_FOUND",
                message=f"{repo.slug()}#{pr_number} was not found",
                detail={"repo_slug": repo.slug(), "pr_number": pr_number},
            )

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_failing_fetcher,
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=404,
                    )
                )

        assert excinfo.value.error_code == "PR_NOT_FOUND"
        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == {"repo_slug": "dimileeh/aira-web", "pr_number": 404}

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "metadata, error_code",
        [
            (_metadata(state="CLOSED"), "PR_ALREADY_CLOSED"),
            (_metadata(state="MERGED"), "PR_ALREADY_MERGED"),
        ],
    )
    async def test_fetch_metadata_rejects_terminal_fetcher_results(
        self,
        factory: async_sessionmaker[AsyncSession],
        metadata: PullRequestAdoptionMetadata,
        error_code: str,
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(metadata),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service._fetch_metadata(
                    repo=RepoRef(owner="dimileeh", name="aira-web"),
                    pr_number=277,
                )

        assert excinfo.value.error_code == error_code
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == {
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "state": metadata.state,
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "payload",
        [
            PullRequestMonitorAdoptionRequest(pr_url="https://example.com/x/y/pull/1"),
            PullRequestMonitorAdoptionRequest(
                pr_url="https://github.com/dimileeh/aira-web/pull/277",
                pr_number=278,
            ),
        ],
    )
    async def test_invalid_pr_url_identity_raises_structured_input_error(
        self,
        factory: async_sessionmaker[AsyncSession],
        payload: PullRequestMonitorAdoptionRequest,
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(payload)

        assert excinfo.value.error_code == "PR_ADOPTION_INPUT_REQUIRED"
        assert excinfo.value.status_code == 422

    @pytest.mark.unit
    async def test_invalid_repo_identity_raises_structured_input_error(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_url="https://example.com/not/github",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "INVALID_GITHUB_REPO"
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == {"repo": "https://example.com/not/github"}

    @pytest.mark.unit
    async def test_replay_with_changed_review_grace_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    initial_review_grace_period_seconds=10,
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        initial_review_grace_period_seconds=11,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": excinfo.value.detail["workspace_id"],
            "existing_initial_review_grace_period_seconds": 10,
            "requested_initial_review_grace_period_seconds": 11,
        }

    @pytest.mark.unit
    async def test_response_falls_back_to_workspace_fields_without_policy(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="https://github.com/dimileeh/aira-web.git",
                branch_base="development",
                task_title="manual",
                task_prompt="manual",
                agent="codex",
                test_commands=[],
                task_policy={},
                remote_push_branch="feature/manual",
            )
            workspace.pr_number = 9
            workspace.pr_url = "https://github.com/dimileeh/aira-web/pull/9"
            workspace.monitor_last_commit_sha = "c" * 40

            response = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )._response(workspace, attached_existing=True)

        assert response.attached_existing is True
        assert response.repo_slug == "dimileeh/aira-web"
        assert response.pr_number == 9
        assert response.head_ref == "feature/manual"
        assert response.head_sha == "c" * 40
        assert response.base_sha is None

    @pytest.mark.unit
    async def test_default_metadata_fetcher_delegates_to_github_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, int]] = []

        async def _fake_fetch(
            *,
            runner: object,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            del runner
            calls.append((repo.slug(), pr_number))
            return _metadata(number=pr_number)

        monkeypatch.setattr(
            adoption_module,
            "fetch_pull_request_adoption_metadata",
            _fake_fetch,
        )

        result = await adoption_module._default_metadata_fetcher(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=123,
        )

        assert calls == [("dimileeh/aira-web", 123)]
        assert result.number == 123

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("reason_code", "status_code"),
        [
            ("PR_ALREADY_CLOSED", 409),
            ("PR_ADOPTION_INPUT_REQUIRED", 422),
            ("PR_METADATA_FETCH_FAILED", 502),
            ("PR_METADATA_INVALID", 502),
        ],
    )
    def test_metadata_error_status_code_mapping(
        self,
        reason_code: str,
        status_code: int,
    ) -> None:
        assert adoption_module._metadata_error_status_code(reason_code) == status_code

    @pytest.mark.unit
    def test_public_error_code_contract_covers_metadata_fetch_failures(self) -> None:
        contract_codes = {
            row["error_code"] for row in adoption_module._PR_ADOPTION_ERROR_CODE_CONTRACT
        }

        assert {"PR_METADATA_FETCH_FAILED", "PR_METADATA_INVALID"} <= contract_codes
