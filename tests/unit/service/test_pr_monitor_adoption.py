"""PR monitor adoption service tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    PullRequestMetadataError,
    RepoRef,
)
from awf.db import repositories as repository_module
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.models import (
    Operation,
    QueueDecision,
    ResourceReservation,
    Task,
    TaskAttempt,
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories import TaskAttemptRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from tests.postgres import postgres_test_engine

_LIVE_ADOPTION_STATUSES = {
    WorkspaceStatus.requested.value,
    WorkspaceStatus.provisioning.value,
    WorkspaceStatus.ready.value,
    WorkspaceStatus.running.value,
    WorkspaceStatus.validating.value,
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
}


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
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST_PROVISION")
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
            assert workspace.repo_url == "git@github.com:dimileeh/aira-web.git"
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
            WorkspaceStatus.destroying.value,
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
            fresh = next(workspace for workspace in workspaces if workspace.id == result.workspace_id)
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
            assert old_task.external_id == adoption_module._superseded_adoption_external_id(
                external_id=logical_task_external_id,
                workspace_id=first.workspace_id,
            )
            assert old_task.title == "feature: ready"
            assert fresh_task.external_id == logical_task_external_id
            assert fresh_task.title == "feature: retitled"
            assert fresh_workspace.task_external_id == fresh_task.external_id
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
            old_workspace.status = WorkspaceStatus.destroyed.value
            old_workspace.idempotency_key = None
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
            assert fresh_workspace.task_external_id == f"{logical_task_external_id}:g1"
            assert fresh_task.external_id == fresh_workspace.task_external_id
            assert fresh_task.idempotency_key == f"{logical_key}:g1"
            assert fresh_task.title == "feature: retitled"

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
            assert [workspace.id for workspace in live_workspaces] == [
                first_result.workspace_id
            ]

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

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "terminal_status",
        [
            WorkspaceStatus.cancelled,
            WorkspaceStatus.failed,
            WorkspaceStatus.destroying,
            WorkspaceStatus.destroyed,
        ],
    )
    async def test_terminal_existing_adoption_is_superseded_by_fresh_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
        terminal_status: WorkspaceStatus,
    ) -> None:
        canonical_key = _canonical_key()
        old_metadata = _metadata(
            head_ref="feature/stale",
            base_ref="development-old",
            head_sha="a" * 40,
            base_sha="1" * 40,
        )
        current_metadata = _metadata(
            head_ref="feature/current",
            base_ref="development",
            head_sha="c" * 40,
            base_sha="2" * 40,
        )
        old_fetcher = _MetadataFetcher(old_metadata)
        current_fetcher = _MetadataFetcher(current_metadata)

        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=old_fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=False,
                )
            )
            await _transition_adoption(session, first.workspace_id, terminal_status)
            await session.commit()

        async with factory() as session:
            fresh = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=current_fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=True,
                )
            )
            await session.commit()

        assert fresh.attached_existing is False
        assert fresh.workspace_id != first.workspace_id
        assert fresh.status == WorkspaceStatus.requested
        assert fresh.head_ref == "feature/current"
        assert fresh.base_ref == "development"
        assert fresh.head_sha == "c" * 40
        assert fresh.base_sha == "2" * 40
        assert fresh.auto_merge is True
        assert current_fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            workspaces = list(
                (
                    await session.execute(
                        select(Workspace).order_by(Workspace.created_at.asc(), Workspace.id.asc())
                    )
                ).scalars()
            )
            assert len(workspaces) == 2
            old = next(workspace for workspace in workspaces if workspace.id == first.workspace_id)
            new = next(workspace for workspace in workspaces if workspace.id == fresh.workspace_id)
            canonical_external_id = adoption_module._adoption_external_id(
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )
            superseded_external_id = adoption_module._superseded_adoption_external_id(
                external_id=canonical_external_id,
                workspace_id=old.id,
            )
            assert old.status == terminal_status.value
            assert old.idempotency_key != canonical_key
            assert old.idempotency_key is not None
            assert old.idempotency_key.startswith(f"{canonical_key}:superseded:")
            assert old.task_external_id == superseded_external_id
            assert new.idempotency_key == canonical_key
            assert new.task_external_id == canonical_external_id
            assert new.task_policy["pr_adoption"]["head_ref"] == "feature/current"
            assert old.task_policy["pr_adoption"]["head_ref"] == "feature/stale"

    @pytest.mark.unit
    async def test_active_existing_adoption_still_attaches_without_metadata_fetch(
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
                    auto_merge=False,
                )
            )
            replay_fetcher = _MetadataFetcher(_metadata(head_ref="feature/should-not-fetch"))
            replay = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=replay_fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    pr_url="https://github.com/dimileeh/aira-web/pull/277",
                    auto_merge=False,
                )
            )
            await session.commit()

        assert replay.attached_existing is True
        assert replay.workspace_id == first.workspace_id
        assert replay_fetcher.calls == []
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            assert await _count(session, Workspace) == 1

    @pytest.mark.unit
    def test_unknown_existing_adoption_status_is_treated_as_resumable(self) -> None:
        workspace = Workspace(status="monitoring_review_repair")

        assert adoption_module._adoption_workspace_is_resumable(workspace) is True

    @pytest.mark.unit
    async def test_unknown_existing_adoption_status_attaches_with_raw_status(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        unknown_status = "monitoring_review_repair"
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=False,
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            workspace.status = unknown_status
            await session.commit()

        async with factory() as session:
            replay_fetcher = _MetadataFetcher(_metadata(head_ref="feature/should-not-fetch"))
            replay = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=replay_fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    pr_url="https://github.com/dimileeh/aira-web/pull/277",
                    auto_merge=False,
                )
            )
            await session.commit()

        assert replay.attached_existing is True
        assert replay.workspace_id == first.workspace_id
        assert replay.status == unknown_status
        assert replay_fetcher.calls == []
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

    @pytest.mark.unit
    async def test_completed_existing_adoption_refetches_and_rejects_merged_pr(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    auto_merge=False,
                )
            )
            await _transition_adoption(session, first.workspace_id, WorkspaceStatus.completed)
            await session.commit()

        async with factory() as session:
            replay_fetcher = _MetadataFetcher(_metadata(state="MERGED"))
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=replay_fetcher,
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        pr_url="https://github.com/dimileeh/aira-web/pull/277",
                        auto_merge=False,
                    )
                )
            await session.commit()

        assert excinfo.value.error_code == "PR_ALREADY_MERGED"
        assert replay_fetcher.calls == [("dimileeh/aira-web", 277)]
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

        async with factory() as session:
            assert await _count(session, Workspace) == 1

    @pytest.mark.unit
    async def test_terminal_policy_difference_does_not_block_fresh_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        canonical_key = _canonical_key()
        async with factory() as session:
            stale = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/stale")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                    profile_ref="auto",
                    auto_merge=False,
                    initial_review_grace_period_seconds=7,
                )
            )
            await _transition_adoption(session, stale.workspace_id, WorkspaceStatus.cancelled)
            await session.commit()

        async with factory() as session:
            fresh = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(
                    _metadata(head_ref="feature/current", head_sha="d" * 40)
                ),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="claude_code",
                    profile_ref="python",
                    auto_merge=True,
                    initial_review_grace_period_seconds=13,
                )
            )
            await session.commit()

        assert fresh.attached_existing is False
        assert fresh.workspace_id != stale.workspace_id
        assert fresh.head_ref == "feature/current"
        assert fresh.auto_merge is True
        assert fresh.monitor_policy == {
            "auto_merge": True,
            "initial_review_grace_period_seconds": 13.0,
        }

        async with factory() as session:
            old = await WorkspaceRepository(session).get(stale.workspace_id)
            new = await WorkspaceRepository(session).get(fresh.workspace_id)
            assert old is not None
            assert new is not None
            assert old.idempotency_key != canonical_key
            assert new.idempotency_key == canonical_key
            assert old.agent == "codex"
            assert old.profile_ref == "auto"
            assert old.auto_merge is False
            assert old.initial_review_grace_period_seconds == 7
            assert new.agent == "claude_code"
            assert new.profile_ref == "python"
            assert new.auto_merge is True
            assert new.initial_review_grace_period_seconds == 13
            assert new.task_policy["pr_adoption"]["head_ref"] == "feature/current"
            assert new.task_policy["pr_adoption"]["head_sha"] == "d" * 40

    @pytest.mark.unit
    async def test_terminal_non_adoption_key_conflicts_without_superseding(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        canonical_key = _canonical_key()
        fetcher = _MetadataFetcher(_metadata(head_ref="feature/current"))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="https://github.com/dimileeh/aira-web.git",
                branch_base="development",
                task_title="ordinary workspace",
                task_prompt="This is not a PR adoption.",
                agent="codex",
                test_commands=[],
                idempotency_key=canonical_key,
                task_policy={},
                profile_ref="auto",
            )
            workspace_id = workspace.id
            await _transition_adoption(session, workspace_id, WorkspaceStatus.cancelled)
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
        assert excinfo.value.detail == {
            "workspace_id": workspace_id,
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "existing_task_kind": "feature_branch_pr",
            "existing_pr_adoption_repo_slug": None,
            "existing_pr_adoption_pr_number": None,
        }
        assert fetcher.calls == []

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            assert workspace.idempotency_key == canonical_key
            assert workspace.task_policy == {}
            assert await _count(session, Workspace) == 1
            assert not any(
                event.event_type == "workspace.pr_monitor_adoption_superseded"
                for event in workspace.events
            )

    @pytest.mark.unit
    async def test_superseded_terminal_row_remains_auditable(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        canonical_key = _canonical_key()
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/stale")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await _transition_adoption(session, first.workspace_id, WorkspaceStatus.cancelled)
            await session.commit()

        async with factory() as session:
            fresh = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/current")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await session.commit()

        async with factory() as session:
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            new_workspace = await WorkspaceRepository(session).get(fresh.workspace_id)
            assert old_workspace is not None
            assert new_workspace is not None

            old_attempt = await TaskAttemptRepository(session).get_by_workspace_id(
                first.workspace_id
            )
            new_attempt = await TaskAttemptRepository(session).get_by_workspace_id(
                fresh.workspace_id
            )
            assert old_attempt is not None
            assert new_attempt is not None
            assert old_attempt.id != new_attempt.id

            operations = list(
                (
                    await session.execute(
                        select(Operation).order_by(Operation.created_at.asc(), Operation.id.asc())
                    )
                ).scalars()
            )
            assert len(operations) == 2

            old_events = list(
                (
                    await session.execute(
                        select(WorkspaceEvent)
                        .where(WorkspaceEvent.workspace_id == first.workspace_id)
                        .order_by(WorkspaceEvent.occurred_at.asc(), WorkspaceEvent.id.asc())
                    )
                ).scalars()
            )
            superseded_event = next(
                event
                for event in old_events
                if event.event_type == "workspace.pr_monitor_adoption_superseded"
            )
            assert superseded_event.reason_code == "PR_ADOPTION_SUPERSEDED_TERMINAL_WORKSPACE"
            assert superseded_event.payload == {
                "reason_code": "PR_ADOPTION_SUPERSEDED_TERMINAL_WORKSPACE",
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "previous_workspace_id": first.workspace_id,
                "previous_status": WorkspaceStatus.cancelled.value,
                "previous_idempotency_key": canonical_key,
                "superseded_idempotency_key": old_workspace.idempotency_key,
                "replacement_workspace_id": fresh.workspace_id,
            }

            new_operation = next(operation for operation in operations if operation.workspace_id == fresh.workspace_id)
            assert new_operation.payload is not None
            assert new_operation.payload["superseded_adoption"] == superseded_event.payload

            new_requested_event = next(
                event
                for event in new_workspace.events
                if event.event_type == "workspace.pr_monitor_adoption_requested"
            )
            assert new_requested_event.payload is not None
            assert new_requested_event.payload["superseded_adoption"] == superseded_event.payload

    @pytest.mark.unit
    async def test_replay_after_supersession_attaches_new_active_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/stale")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await _transition_adoption(session, first.workspace_id, WorkspaceStatus.cancelled)
            await session.commit()

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/current")),
            )
            fresh = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            replay = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await session.commit()

        assert fresh.attached_existing is False
        assert replay.attached_existing is True
        assert replay.workspace_id == fresh.workspace_id

        async with factory() as session:
            assert await _count(session, Workspace) == 2

    @pytest.mark.unit
    async def test_concurrent_terminal_supersession_converges_on_one_fresh_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/stale")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            await _transition_adoption(session, first.workspace_id, WorkspaceStatus.cancelled)
            await session.commit()

        async def _adopt_once() -> tuple[str, bool]:
            async with factory() as session:
                result = await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=_MetadataFetcher(_metadata(head_ref="feature/current")),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )
                await session.commit()
                return result.workspace_id, result.attached_existing

        results = await asyncio.gather(_adopt_once(), _adopt_once())

        workspace_ids = {workspace_id for workspace_id, _attached in results}
        attached_flags = sorted(attached for _workspace_id, attached in results)
        assert len(workspace_ids) == 1
        assert attached_flags == [False, True]

        async with factory() as session:
            canonical_key = _canonical_key()
            active_workspaces = list(
                (
                    await session.execute(
                        select(Workspace).where(Workspace.idempotency_key == canonical_key)
                    )
                ).scalars()
            )
            assert len(active_workspaces) == 1
            assert active_workspaces[0].id in workspace_ids
            assert await _count(session, Workspace) == 2

    @pytest.mark.unit
    async def test_replay_with_changed_explicit_repo_url_conflicts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_url="https://github.com/dimileeh/aira-web.git",
                    pr_number=277,
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_url="git@github.com:dimileeh/aira-web.git",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": excinfo.value.detail["workspace_id"],
            "repo_slug": "dimileeh/aira-web",
            "existing_repo_url": "https://github.com/dimileeh/aira-web.git",
            "requested_repo_url": "git@github.com:dimileeh/aira-web.git",
        }

    @pytest.mark.unit
    async def test_replay_with_changed_inline_profile_conflicts(
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
                    profile={
                        "name": "inline-a",
                        "monitor": {"initial_review_grace_period_seconds": 10},
                    },
                )
            )

            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        profile={
                            "name": "inline-b",
                            "monitor": {"initial_review_grace_period_seconds": 10},
                        },
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": excinfo.value.detail["workspace_id"],
            "existing_inline_profile_name": "inline-a",
            "requested_inline_profile_name": "inline-b",
        }

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
    @pytest.mark.parametrize(
        ("identity_field", "identity_value"),
        [
            ("repo_url", "https://github.com/dimileeh/stale-repo.git"),
            ("repo_slug", "dimileeh/stale-repo"),
        ],
    )
    async def test_pr_url_rejects_conflicting_repo_identity_before_metadata_fetch(
        self,
        factory: async_sessionmaker[AsyncSession],
        identity_field: str,
        identity_value: str,
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        pr_url="https://github.com/dimileeh/aira-web/pull/277",
                        **{identity_field: identity_value},
                    )
                )

            assert await _count(session, Workspace) == 0

        assert excinfo.value.error_code == "PR_ADOPTION_INPUT_REQUIRED"
        assert excinfo.value.status_code == 422
        assert fetcher.calls == []

    @pytest.mark.unit
    async def test_pr_url_rejects_unparseable_secondary_repo_identity_before_metadata_fetch(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, metadata_fetcher=fetcher)
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        pr_url="https://github.com/dimileeh/aira-web/pull/277",
                        repo_url="https://example.com/not/github",
                    )
                )

            assert await _count(session, Workspace) == 0

        assert excinfo.value.error_code == "INVALID_GITHUB_REPO"
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == {
            "repo": "https://example.com/not/github",
            "field": "repo_url",
        }
        assert fetcher.calls == []

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

    @pytest.mark.unit
    def test_inline_profile_name_handles_missing_profile(self) -> None:
        assert adoption_module._inline_profile_name(None) is None

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("workspace", "expected"),
        [
            (
                Workspace(
                    task_kind="sync_feature_pr",
                    task_external_id="pr-adopt-external",
                    idempotency_key=None,
                    task_policy={},
                ),
                True,
            ),
            (
                Workspace(
                    task_kind="sync_feature_pr",
                    task_external_id=None,
                    idempotency_key="logical-key",
                    task_policy={
                        "pr_adoption": {
                            "repo_slug": "DIMILEEH/AIRA-WEB",
                            "pr_number": "277",
                        }
                    },
                ),
                True,
            ),
            (
                Workspace(
                    task_kind="sync_feature_pr",
                    task_external_id=None,
                    idempotency_key="logical-key",
                    task_policy={},
                ),
                False,
            ),
            (
                Workspace(
                    task_kind="feature_branch_pr",
                    task_external_id=None,
                    idempotency_key="other-key",
                    task_policy={
                        "pr_adoption": {
                            "repo_slug": "dimileeh/aira-web",
                            "pr_number": 277,
                        }
                    },
                ),
                False,
            ),
            (
                Workspace(
                    task_kind="sync_feature_pr",
                    task_external_id=None,
                    idempotency_key="logical-key",
                    task_policy={
                        "pr_adoption": {
                            "repo_slug": "dimileeh/aira-web",
                            "pr_number": ["277"],
                        }
                    },
                ),
                False,
            ),
            (
                Workspace(
                    task_kind="sync_feature_pr",
                    task_external_id=None,
                    idempotency_key="logical-key",
                    task_policy={
                        "pr_adoption": {
                            "repo_slug": "dimileeh/aira-web",
                            "pr_number": "not-a-number",
                        }
                    },
                ),
                False,
            ),
            (
                Workspace(
                    task_kind="sync_feature_pr",
                    task_external_id=None,
                    idempotency_key="logical-key",
                    task_policy={"pr_adoption": "legacy-bad-payload"},
                ),
                False,
            ),
        ],
    )
    def test_pr_adoption_history_identity_matches_external_id_and_policy_fallback(
        self,
        workspace: Workspace,
        expected: bool,
    ) -> None:
        assert (
            repository_module._matches_pr_adoption_identity(
                workspace,
                task_external_id="pr-adopt-external",
                idempotency_key="logical-key",
                task_kind="sync_feature_pr",
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )
            is expected
        )

    @pytest.mark.unit
    async def test_terminal_lineage_skips_live_adoption_candidates(
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
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            fresh = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            fresh_workspace = await WorkspaceRepository(session).get(fresh.workspace_id)
            assert fresh_workspace is not None

            lineage = await adoption_module._terminal_adoption_lineage(
                session,
                [old_workspace, fresh_workspace],
            )

        assert lineage == [
            {
                "workspace_id": first.workspace_id,
                "status": WorkspaceStatus.destroyed.value,
                "task_id": first.task_id,
                "attempt_id": first.attempt_id,
            }
        ]

    @pytest.mark.unit
    async def test_terminal_lineage_uses_adoption_history_attempts_without_extra_reads(
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
                )
            )
            first_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert first_workspace is not None
            first_workspace.status = WorkspaceStatus.destroyed.value
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            second_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            assert second_workspace is not None
            second_workspace.status = WorkspaceStatus.destroyed.value
            await session.flush()

            repo_slug = "dimileeh/aira-web"
            pr_number = 277
            history = await WorkspaceRepository(session).list_pr_adoption_history(
                task_external_id=adoption_module._adoption_external_id(
                    repo_slug=repo_slug,
                    pr_number=pr_number,
                ),
                idempotency_key=adoption_module.pr_adoption_idempotency_key(
                    repo_slug=repo_slug,
                    pr_number=pr_number,
                ),
                task_kind=adoption_module.PR_ADOPTION_TASK_KIND,
                repo_slug=repo_slug,
                pr_number=pr_number,
            )

            task_attempt_selects: list[str] = []

            def _capture_task_attempt_selects(
                _conn: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                normalized = statement.upper()
                if not normalized.lstrip().startswith("SELECT"):
                    return
                if "FROM TASK_ATTEMPTS" in normalized:
                    task_attempt_selects.append(statement)

            engine = factory.kw["bind"]
            event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                _capture_task_attempt_selects,
            )
            try:
                lineage = await adoption_module._terminal_adoption_lineage(
                    session,
                    history,
                )
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    _capture_task_attempt_selects,
                )

        assert lineage == [
            {
                "workspace_id": first.workspace_id,
                "status": WorkspaceStatus.destroyed.value,
                "task_id": first.task_id,
                "attempt_id": first.attempt_id,
            },
            {
                "workspace_id": second.workspace_id,
                "status": WorkspaceStatus.destroyed.value,
                "task_id": second.task_id,
                "attempt_id": second.attempt_id,
            },
        ]
        assert task_attempt_selects == []

    @pytest.mark.unit
    async def test_adoption_workspace_idempotency_key_prefetches_generation_family(
        self,
    ) -> None:
        class _WorkspaceRepo:
            def __init__(self) -> None:
                self.list_calls: list[str] = []

            async def list_idempotency_key_family(self, logical_key: str) -> list[str]:
                self.list_calls.append(logical_key)
                return [
                    logical_key,
                    f"{logical_key}:g1",
                    f"{logical_key}:g3",
                    f"{logical_key}:g1000",
                    f"{logical_key}:gignored",
                ]

            async def get_by_idempotency_key(self, key: str) -> object | None:
                raise AssertionError(f"unexpected per-key lookup for {key}")

        repo = _WorkspaceRepo()

        key = await adoption_module._next_adoption_workspace_idempotency_key(
            repo,  # type: ignore[arg-type]
            logical_idempotency_key="pr-adopt:logical",
        )

        assert key == "pr-adopt:logical:g2"
        assert repo.list_calls == ["pr-adopt:logical"]

    @pytest.mark.unit
    async def test_adoption_workspace_idempotency_key_exhaustion_is_explicit(self) -> None:
        class _ExhaustedWorkspaceRepo:
            async def list_idempotency_key_family(self, logical_key: str) -> list[str]:
                return [logical_key, *(f"{logical_key}:g{generation}" for generation in range(1, 1000))]

        with pytest.raises(RuntimeError, match="fresh PR adoption workspace idempotency key"):
            await adoption_module._next_adoption_workspace_idempotency_key(
                _ExhaustedWorkspaceRepo(),  # type: ignore[arg-type]
                logical_idempotency_key="pr-adopt:exhausted",
            )
