"""PR monitor adoption service tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    RepoRef,
)
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.models import (
    Operation,
    QueueDecision,
    ResourceReservation,
    Task,
    TaskAttempt,
    Workspace,
)
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
    _LIVE_ADOPTION_STATUSES,
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _metadata(
    *,
    number: int = 277,
    state: str = "OPEN",
    head_ref: str = "feature/ready",
    head_repo_slug: str = "dimileeh/aira-web",
    base_ref: str = "development",
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
    merged: bool | None = None,
    closed: bool | None = None,
    title: str = "feature: ready",
) -> PullRequestAdoptionMetadata:
    merged_value = state == "MERGED" if merged is None else merged
    closed_value = state == "CLOSED" if closed is None else closed
    return PullRequestAdoptionMetadata(
        number=number,
        head_ref=head_ref,
        head_repo_slug=head_repo_slug,
        base_ref=base_ref,
        head_sha=head_sha,
        base_sha=base_sha,
        state=state,
        is_draft=False,
        closed=closed_value,
        merged=merged_value,
        author="octocat",
        url=f"https://github.com/dimileeh/aira-web/pull/{number}",
        title=title,
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


async def _transition_adoption(
    session: AsyncSession,
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await repo.get(workspace_id)
    assert workspace is not None
    if status == WorkspaceStatus.cancelled:
        await repo.transition(workspace, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL")
        return
    if status == WorkspaceStatus.failed:
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        return
    if status == WorkspaceStatus.completed:
        await repo.transition(
            workspace, to=WorkspaceStatus.provisioning, reason_code="TEST_PROVISION"
        )
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="TEST_READY")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="TEST_RUN")
        await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="TEST_VALIDATE")
        await repo.transition(workspace, to=WorkspaceStatus.completed, reason_code="TEST_COMPLETE")
        return
    if status in {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}:
        await repo.transition(workspace, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL")
        await repo.transition(workspace, to=WorkspaceStatus.destroying, reason_code="TEST_DESTROY")
        if status == WorkspaceStatus.destroyed:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroyed,
                reason_code="TEST_DESTROYED",
            )
        return
    raise AssertionError(f"Unsupported terminal test status: {status.value}")


def _canonical_key() -> str:
    return adoption_module.pr_adoption_idempotency_key(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
    )


async def _adoption_workspaces(
    session: AsyncSession,
    *,
    repo_slug: str = "dimileeh/aira-web",
    pr_number: int = 277,
) -> list[Workspace]:
    task_external_id = adoption_module._adoption_external_id(
        repo_slug=repo_slug,
        pr_number=pr_number,
    )
    idempotency_key = adoption_module.pr_adoption_idempotency_key(
        repo_slug=repo_slug,
        pr_number=pr_number,
    )
    return await WorkspaceRepository(session).list_pr_adoption_history(
        task_external_id=task_external_id,
        idempotency_key=idempotency_key,
        task_kind=adoption_module.PR_ADOPTION_TASK_KIND,
        repo_slug=repo_slug,
        pr_number=pr_number,
    )


class TestPullRequestMonitorAdoptionServicePart001:
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
                    owned_paths=[".github/workflows/publish.yml", "pyproject.toml"],
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
            assert workspace.repo_url == "git@github.com:dimileeh/aira-web.git"
            assert workspace.task_title == "feature: ready"
            assert workspace.task_kind == "sync_feature_pr"
            assert workspace.pr_url == "https://github.com/dimileeh/aira-web/pull/277"
            assert workspace.pr_number == 277
            assert workspace.monitor_last_commit_sha == "h" * 40
            assert workspace.owned_paths == [
                ".github/workflows/publish.yml",
                "pyproject.toml",
            ]
            assert workspace.auto_merge is False
            assert workspace.initial_review_grace_period_seconds == 12
            assert "agent_model" not in workspace.task_policy
            assert "agent_effort" not in workspace.task_policy
            assert workspace.task_policy["pr_adoption"] == {
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "pr_url": "https://github.com/dimileeh/aira-web/pull/277",
                "head_ref": "feature/ready",
                "head_repo_slug": "dimileeh/aira-web",
                "head_repo_url": "git@github.com:dimileeh/aira-web.git",
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
            assert task.owned_paths == [
                ".github/workflows/publish.yml",
                "pyproject.toml",
            ]
            attempt = (await session.execute(select(TaskAttempt))).scalar_one()
            assert attempt.owned_paths == [
                ".github/workflows/publish.yml",
                "pyproject.toml",
            ]

    @pytest.mark.unit
    async def test_attaching_live_adoption_rejects_different_owned_paths(
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
                    owned_paths=["docs/**"],
                )
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        owned_paths=[".github/workflows/publish.yml"],
                    )
                )

        assert first.workspace_id.startswith("ws_")
        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": first.workspace_id,
            "existing_owned_paths": ["docs/**"],
            "requested_owned_paths": [".github/workflows/publish.yml"],
        }

    @pytest.mark.unit
    async def test_attaching_live_adoption_allows_reordered_owned_paths(
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
                    owned_paths=["docs/**", ".github/workflows/publish.yml"],
                )
            )
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    owned_paths=[".github/workflows/publish.yml", "docs/**"],
                )
            )
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("request_kwargs", "expected_policy"),
        [
            (
                {"model": "gpt-5.3-codex", "effort": "high"},
                {"agent_model": "gpt-5.3-codex", "agent_effort": "high"},
            ),
            (
                {"model": "gpt-5.3-codex"},
                {"agent_model": "gpt-5.3-codex", "agent_effort": "xhigh"},
            ),
            (
                {"effort": "low"},
                {"agent_effort": "low"},
            ),
        ],
    )
    async def test_persists_requested_agent_policy(
        self,
        factory: async_sessionmaker[AsyncSession],
        request_kwargs: dict[str, object],
        expected_policy: dict[str, str],
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
                    **request_kwargs,
                )
            )
            await session.commit()

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            for key, value in expected_policy.items():
                assert workspace.task_policy[key] == value
            if "agent_model" not in expected_policy:
                assert "agent_model" not in workspace.task_policy

    @pytest.mark.unit
    def test_model_only_policy_omits_effort_when_agent_default_has_no_effort(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(adoption_module, "defaults_with_model_overrides", lambda _models: {})

        policy = adoption_module._requested_agent_policy(
            PullRequestMonitorAdoptionRequest(
                repo_slug="dimileeh/aira-web",
                pr_number=277,
                model="local-model-without-effort-policy",
            )
        )

        assert policy == {"agent_model": "local-model-without-effort-policy"}

    @pytest.mark.unit
    async def test_persists_head_repo_identity_for_fork_pr(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata(head_repo_slug="contributor/aira-web"))
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            adoption = workspace.task_policy["pr_adoption"]
            assert workspace.remote_push_branch == "feature/ready"
            assert adoption["repo_slug"] == "dimileeh/aira-web"
            assert adoption["head_ref"] == "feature/ready"
            assert adoption["head_repo_slug"] == "contributor/aira-web"
            assert adoption["head_repo_url"] == "git@github.com:contributor/aira-web.git"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "request_kwargs",
        [
            {"repo_slug": "dimileeh/aira-web", "pr_number": 277},
            {"pr_url": "https://github.com/dimileeh/aira-web/pull/277"},
        ],
    )
    async def test_adoption_without_repo_url_uses_ssh_clone_url(
        self,
        factory: async_sessionmaker[AsyncSession],
        request_kwargs: dict[str, object],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(PullRequestMonitorAdoptionRequest(**request_kwargs))
            await session.commit()

        assert result.repo_url == "git@github.com:dimileeh/aira-web.git"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            assert workspace.repo_url == "git@github.com:dimileeh/aira-web.git"

    @pytest.mark.unit
    async def test_explicit_repo_url_preserves_clone_transport(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata(head_repo_slug="contributor/aira-web"))
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_url="https://github.com/dimileeh/aira-web.git",
                    pr_number=277,
                )
            )
            await session.commit()

        assert result.repo_url == "https://github.com/dimileeh/aira-web.git"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            adoption = workspace.task_policy["pr_adoption"]
            assert workspace.repo_url == "https://github.com/dimileeh/aira-web.git"
            assert adoption["head_repo_url"] == "https://github.com/contributor/aira-web.git"

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
    async def test_replay_with_same_model_only_policy_attaches_to_live_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        metadata = _metadata()
        fetcher = _MetadataFetcher(metadata)

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    model="gpt-5.3-codex",
                )
            )
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    pr_url="https://github.com/dimileeh/aira-web/pull/277",
                    model="gpt-5.3-codex",
                )
            )
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

    @pytest.mark.unit
    @pytest.mark.parametrize("legacy_task_policy", [None, "legacy"])
    async def test_replay_treats_legacy_non_object_task_policy_as_empty_agent_policy(
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

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("initial_kwargs", "expected_detail"),
        [
            (
                {"model": "gpt-5.3-codex"},
                {
                    "existing_agent_model": "gpt-5.3-codex",
                    "requested_agent_model": None,
                },
            ),
            (
                {"effort": "high"},
                {
                    "existing_agent_effort": "high",
                    "requested_agent_effort": None,
                },
            ),
        ],
    )
    async def test_replay_omitting_agent_policy_conflicts_with_policy_bearing_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
        initial_kwargs: dict[str, object],
        expected_detail: dict[str, object],
    ) -> None:
        metadata = _metadata()
        fetcher = _MetadataFetcher(metadata)

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    **initial_kwargs,
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        pr_url="https://github.com/dimileeh/aira-web/pull/277"
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": first.workspace_id,
            **expected_detail,
        }
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

    @pytest.mark.unit
    async def test_replay_with_different_model_policy_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    model="gpt-5.3-codex",
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        model="gpt-5.4",
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": first.workspace_id,
            "existing_agent_model": "gpt-5.3-codex",
            "requested_agent_model": "gpt-5.4",
        }

    @pytest.mark.unit
    async def test_replay_with_different_effort_policy_conflicts_after_model_defaulting(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    model="gpt-5.3-codex",
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        model="gpt-5.3-codex",
                        effort="high",
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": first.workspace_id,
            "existing_agent_effort": "xhigh",
            "requested_agent_effort": "high",
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "terminal_status",
        [
            WorkspaceStatus.cancelled,
            WorkspaceStatus.completed,
            WorkspaceStatus.destroyed,
            WorkspaceStatus.failed,
        ],
    )
    async def test_terminal_adoption_record_allows_fresh_pr_monitor(
        self,
        factory: async_sessionmaker[AsyncSession],
        terminal_status: WorkspaceStatus,
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            original_key = workspace.idempotency_key
            original_external_id = workspace.task_external_id
            workspace.status = terminal_status.value
            await session.flush()

            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        assert second.status == WorkspaceStatus.requested
        assert fetcher.calls == [("dimileeh/aira-web", 277), ("dimileeh/aira-web", 277)]

        async with factory() as session:
            first_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            second_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            assert first_workspace is not None
            assert second_workspace is not None
            assert first_workspace.idempotency_key != original_key
            assert first_workspace.idempotency_key is not None
            assert first_workspace.idempotency_key.startswith(f"{original_key}:superseded:")
            assert second_workspace.idempotency_key == original_key
            assert original_external_id is not None
            assert second_workspace.task_external_id == original_external_id
            assert await _count(session, Workspace) == 2
            assert await _count(session, Task) == 2
            assert await _count(session, TaskAttempt) == 2

    @pytest.mark.unit
    async def test_terminal_adoption_record_allows_fresh_pr_monitor_after_title_change(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata(title="feature: first title"))
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            original_key = workspace.idempotency_key
            original_external_id = workspace.task_external_id
            workspace.status = WorkspaceStatus.failed.value
            await session.flush()

            fetcher.metadata = _metadata(title="feature: revised title")
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        assert second.task_id != first.task_id

        async with factory() as session:
            first_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            second_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            assert first_workspace is not None
            assert second_workspace is not None
            assert original_key is not None
            assert original_external_id is not None
            assert first_workspace.idempotency_key is not None
            assert first_workspace.idempotency_key.startswith(f"{original_key}:superseded:")
            assert second_workspace.idempotency_key == original_key
            assert second_workspace.task_title == "feature: revised title"
            assert second_workspace.task_external_id == original_external_id
            superseded_external_id = adoption_module._superseded_adoption_external_id(
                external_id=original_external_id,
                workspace_id=first.workspace_id,
            )
            tasks = list((await session.execute(select(Task))).scalars())
            assert len(tasks) == 2
            assert {task.title for task in tasks} == {
                "feature: first title",
                "feature: revised title",
            }
            assert {task.external_id for task in tasks} == {
                superseded_external_id,
                second_workspace.task_external_id,
            }
            assert {task.idempotency_key for task in tasks} == {
                first_workspace.idempotency_key,
                original_key,
            }

    @pytest.mark.unit
    async def test_adopt_rechecks_existing_workspace_after_idempotency_lock(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        metadata = _metadata()
        lock_keys: list[str] = []
        inserted_workspace_id: str | None = None

        async def _insert_concurrent_adoption(
            self: WorkspaceRepository,
            key: str,
        ) -> None:
            nonlocal inserted_workspace_id
            lock_keys.append(key)
            repo = WorkspaceRepository(self._session)
            workspace = await repo.create(
                repo_url="https://github.com/dimileeh/aira-web.git",
                branch_base=metadata.base_ref,
                task_title=metadata.title,
                task_prompt="existing adoption",
                agent="codex",
                test_commands=[],
                task_policy={
                    "task_kind": "sync_feature_pr",
                    "pr_adoption": {
                        "repo_slug": "dimileeh/aira-web",
                        "pr_number": metadata.number,
                        "pr_url": metadata.url,
                        "head_ref": metadata.head_ref,
                        "head_repo_slug": metadata.head_repo_slug,
                        "head_repo_url": "https://github.com/dimileeh/aira-web.git",
                        "base_ref": metadata.base_ref,
                        "head_sha": metadata.head_sha,
                        "base_sha": metadata.base_sha,
                    },
                },
                auto_merge=True,
                profile_ref="auto",
                idempotency_key=key,
                task_kind="sync_feature_pr",
                remote_push_branch=metadata.head_ref,
            )
            workspace.pr_url = metadata.url
            workspace.pr_number = metadata.number
            workspace.base_commit = metadata.base_sha
            workspace.monitor_last_commit_sha = metadata.head_sha
            await self._session.flush()
            inserted_workspace_id = workspace.id

        monkeypatch.setattr(
            WorkspaceRepository,
            "acquire_idempotency_key_lock",
            _insert_concurrent_adoption,
            raising=False,
        )

        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(metadata),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert result.attached_existing is True
        assert result.workspace_id == inserted_workspace_id
        assert lock_keys == [
            adoption_module.pr_adoption_idempotency_key(
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )
        ]

        async with factory() as session:
            assert await _count(session, Workspace) == 1
            assert await _count(session, TaskAttempt) == 0

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "live_status",
        [
            WorkspaceStatus.requested.value,
            WorkspaceStatus.monitoring_pr.value,
        ],
    )
    async def test_replay_attaches_to_live_adoption_without_refetching_metadata(
        self,
        factory: async_sessionmaker[AsyncSession],
        live_status: str,
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
            workspace.status = live_status

            result = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    pr_url="https://github.com/dimileeh/aira-web/pull/277",
                )
            )
            await session.commit()

        assert result.attached_existing is True
        assert result.workspace_id == first.workspace_id
        assert result.status == WorkspaceStatus(live_status)
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            assert await _count(session, Workspace) == 1
            assert await _count(session, TaskAttempt) == 1
            assert await _count(session, Operation) == 1

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "terminal_status",
        [
            WorkspaceStatus.destroyed.value,
            WorkspaceStatus.cancelled.value,
            WorkspaceStatus.failed.value,
            "superseded",
        ],
    )
    async def test_terminal_prior_adoption_creates_fresh_monitor_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
        terminal_status: str,
    ) -> None:
        metadata = _metadata()
        logical_key = adoption_module.pr_adoption_idempotency_key(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(metadata),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            assert old_workspace.idempotency_key == logical_key
            old_workspace.status = terminal_status
            await session.commit()

        fetcher = _MetadataFetcher(metadata)
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert result.attached_existing is False
        assert result.workspace_id != first.workspace_id
        assert result.status == WorkspaceStatus.requested
        assert result.task_id != first.task_id
        assert result.attempt_id != first.attempt_id
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            workspaces = await _adoption_workspaces(session)
            assert len(workspaces) == 2
            old = next(workspace for workspace in workspaces if workspace.id == first.workspace_id)
            fresh = next(
                workspace for workspace in workspaces if workspace.id == result.workspace_id
            )
            live_workspaces = [
                workspace for workspace in workspaces if workspace.status in _LIVE_ADOPTION_STATUSES
            ]
            assert [workspace.id for workspace in live_workspaces] == [result.workspace_id]
            assert old.status == terminal_status
            assert old.idempotency_key is not None
            assert old.idempotency_key.startswith(f"{logical_key}:superseded:")
            assert fresh.status == WorkspaceStatus.requested.value
            assert fresh.idempotency_key == logical_key
            lineage = fresh.task_policy["pr_adoption"]["lineage"]
            assert lineage["logical_idempotency_key"] == logical_key
            assert lineage["previous_terminal_adoptions"] == [
                {
                    "workspace_id": first.workspace_id,
                    "status": terminal_status,
                    "task_id": first.task_id,
                    "attempt_id": first.attempt_id,
                }
            ]
            adoption_event = next(
                event
                for event in fresh.events
                if event.event_type == "workspace.pr_monitor_adoption_requested"
            )
            assert adoption_event.payload["logical_idempotency_key"] == logical_key
            assert adoption_event.payload["workspace_idempotency_key"] == fresh.idempotency_key
            assert adoption_event.payload["previous_terminal_adoptions"] == [
                {
                    "workspace_id": first.workspace_id,
                    "status": terminal_status,
                    "task_id": first.task_id,
                    "attempt_id": first.attempt_id,
                }
            ]

    @pytest.mark.unit
    async def test_terminal_prior_policy_mismatch_does_not_block_fresh_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=False,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=True,
                )
            )
            await session.commit()

        assert result.attached_existing is False
        assert result.workspace_id != first.workspace_id
        assert result.auto_merge is True

    @pytest.mark.unit
    async def test_terminal_prior_task_scope_change_supersedes_old_task_slot(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
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
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
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
        assert result.attempt_id != first.attempt_id

        async with factory() as session:
            assert await _count(session, Task) == 2
            old_task = await session.get(Task, first.task_id)
            fresh_task = await session.get(Task, result.task_id)
            fresh_workspace = await WorkspaceRepository(session).get(result.workspace_id)

            assert old_task is not None
            assert fresh_task is not None
            assert fresh_workspace is not None
            workspaces = await _adoption_workspaces(session)
            assert old_task.external_id == adoption_module._superseded_adoption_external_id(
                external_id=logical_task_external_id,
                workspace_id=first.workspace_id,
            )
            assert old_task.title == "feature: ready"
            assert fresh_task.external_id == logical_task_external_id
            assert fresh_task.title == "feature: retitled"
            assert fresh_workspace.task_external_id == fresh_task.external_id
            assert [workspace.id for workspace in workspaces] == [
                first.workspace_id,
                result.workspace_id,
            ]
            assert fresh_workspace.task_policy["pr_adoption"]["lineage"][
                "previous_terminal_adoptions"
            ] == [
                {
                    "workspace_id": first.workspace_id,
                    "status": WorkspaceStatus.destroyed.value,
                    "task_id": first.task_id,
                    "attempt_id": first.attempt_id,
                }
            ]

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

    @pytest.mark.unit
    async def test_concurrent_terminal_history_adoptions_create_one_live_monitor(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
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
            await session.commit()

        fetcher = _MetadataFetcher(_metadata())

        async def _adopt_once() -> Any:
            async with factory() as session:
                result = await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=fetcher,
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )
                await session.commit()
                return result

        first_result, second_result = await asyncio.gather(_adopt_once(), _adopt_once())

        assert {first_result.workspace_id, second_result.workspace_id} != {first.workspace_id}
        assert first_result.workspace_id == second_result.workspace_id
        assert sorted(
            [first_result.attached_existing, second_result.attached_existing],
        ) == [False, True]
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            workspaces = await _adoption_workspaces(session)
            live_workspaces = [
                workspace for workspace in workspaces if workspace.status in _LIVE_ADOPTION_STATUSES
            ]
            assert len(workspaces) == 2
            assert [workspace.id for workspace in live_workspaces] == [first_result.workspace_id]

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
        assert excinfo.value.detail == {
            "workspace_id": excinfo.value.detail["workspace_id"],
            "existing_auto_merge": False,
            "requested_auto_merge": True,
        }
