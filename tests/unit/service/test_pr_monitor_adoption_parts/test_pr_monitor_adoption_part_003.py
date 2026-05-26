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
    RepoRef,
)
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    Task,
    TaskAttempt,
    Workspace,
)
from awf.db.repositories import (
    TaskExternalIdConflictError,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
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


class TestPullRequestMonitorAdoptionServicePart003:
    @pytest.mark.unit
    async def test_supersede_previous_adoption_without_attempt_updates_workspace_only(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        logical_key = _canonical_key()
        adoption_external_id = adoption_module._adoption_external_id(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:dimileeh/aira-web.git",
                branch_base="development",
                task_title="detached adoption",
                task_prompt="recover detached adoption",
                task_external_id=adoption_external_id,
                agent="codex",
                test_commands=[],
                idempotency_key=logical_key,
                task_kind=adoption_module.PR_ADOPTION_TASK_KIND,
            )
            workspace.status = WorkspaceStatus.destroyed.value

            payload = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )._supersede_previous_adoption(
                workspace=workspace,
                idempotency_key=logical_key,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )
            await session.commit()

        assert payload["previous_workspace_id"] == workspace.id
        assert payload["previous_idempotency_key"] == logical_key

        async with factory() as session:
            superseded = await WorkspaceRepository(session).get(workspace.id)
            assert superseded is not None
            assert superseded.idempotency_key is not None
            assert superseded.idempotency_key.startswith(f"{logical_key}:superseded:")
            assert superseded.task_external_id == adoption_module._superseded_adoption_external_id(
                external_id=adoption_external_id,
                workspace_id=workspace.id,
            )
            assert await _count(session, TaskAttempt) == 0

    @pytest.mark.unit
    async def test_supersede_previous_adoption_preserves_nonmatching_task_fields(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        logical_key = _canonical_key()
        adoption_external_id = adoption_module._adoption_external_id(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        already_superseded_external_id = adoption_module._superseded_adoption_external_id(
            external_id=adoption_external_id,
            workspace_id="prior-generation",
        )
        task_generation_key = f"{logical_key}:g1"
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            result = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            assert result.task_id is not None
            task = await TaskRepository(session).get(result.task_id)
            assert task is not None
            workspace.status = WorkspaceStatus.destroyed.value
            workspace.task_external_id = already_superseded_external_id
            task.idempotency_key = task_generation_key
            task.external_id = already_superseded_external_id
            await session.flush()

            await service._supersede_previous_adoption(
                workspace=workspace,
                idempotency_key=logical_key,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )
            await session.commit()

        async with factory() as session:
            superseded = await WorkspaceRepository(session).get(result.workspace_id)
            assert superseded is not None
            assert superseded.task_external_id == already_superseded_external_id
            assert result.task_id is not None
            task = await TaskRepository(session).get(result.task_id)
            assert task is not None
            assert task.idempotency_key == task_generation_key
            assert task.external_id == already_superseded_external_id

    @pytest.mark.unit
    async def test_supersede_previous_adoption_tolerates_missing_task_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        logical_key = _canonical_key()

        async def _missing_task(_repo: TaskRepository, _task_id: str) -> None:
            return None

        monkeypatch.setattr(TaskRepository, "get", _missing_task)

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            result = await service.adopt(
                PullRequestMonitorAdoptionRequest(repo_slug="dimileeh/aira-web", pr_number=277)
            )
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            workspace.status = WorkspaceStatus.destroyed.value

            payload = await service._supersede_previous_adoption(
                workspace=workspace,
                idempotency_key=logical_key,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )
            await session.commit()

        assert payload["previous_workspace_id"] == result.workspace_id
        assert payload["previous_idempotency_key"] == logical_key

        async with factory() as session:
            superseded = await WorkspaceRepository(session).get(result.workspace_id)
            assert superseded is not None
            assert superseded.idempotency_key is not None
            assert superseded.idempotency_key.startswith(f"{logical_key}:superseded:")

    @pytest.mark.unit
    async def test_create_adoption_workspace_reraises_unexpected_task_conflict(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _raise_task_conflict(
            _repo: TaskRepository,
            **kwargs: object,
        ) -> Task:
            raise TaskExternalIdConflictError(str(kwargs["external_id"]))

        monkeypatch.setattr(TaskRepository, "create_or_get", _raise_task_conflict)

        metadata = _metadata()
        logical_key = _canonical_key()
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(metadata),
            )
            with pytest.raises(TaskExternalIdConflictError):
                await service._create_adoption_workspace(
                    request=PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    ),
                    repo=RepoRef(owner="dimileeh", name="aira-web"),
                    metadata=metadata,
                    idempotency_key=logical_key,
                    logical_idempotency_key=logical_key,
                    previous_terminal_adoptions=[],
                )

    @pytest.mark.unit
    async def test_task_external_id_family_filters_generated_adoption_ids(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        logical_key = "pr-adopt:github:dimileeh/aira-web:277"
        task_external_id = "pr-adopt:github:dimileeh/aira-web:277"
        async with factory() as session:
            session.add_all(
                [
                    Task(
                        id="task-family-base",
                        external_id=task_external_id,
                        idempotency_key=logical_key,
                        repo_url="git@github.com:dimileeh/aira-web.git",
                        base_branch="development",
                        title="base",
                        prompt="base prompt",
                        owned_paths=[],
                    ),
                    Task(
                        id="task-family-g2",
                        external_id=f"{task_external_id}:g2",
                        idempotency_key=None,
                        repo_url="git@github.com:dimileeh/aira-web.git",
                        base_branch="development",
                        title="generated",
                        prompt="generated prompt",
                        owned_paths=[],
                    ),
                    Task(
                        id="task-family-invalid",
                        external_id=f"{task_external_id}:garbage",
                        idempotency_key=None,
                        repo_url="git@github.com:dimileeh/aira-web.git",
                        base_branch="development",
                        title="invalid generation",
                        prompt="invalid prompt",
                        owned_paths=[],
                    ),
                    Task(
                        id="task-family-other",
                        external_id="pr-adopt:github:dimileeh/other:277:g9",
                        idempotency_key=None,
                        repo_url="git@github.com:dimileeh/other.git",
                        base_branch="development",
                        title="other",
                        prompt="other prompt",
                        owned_paths=[],
                    ),
                    Task(
                        id="task-family-null",
                        external_id=None,
                        idempotency_key="unrelated-null",
                        repo_url="git@github.com:dimileeh/aira-web.git",
                        base_branch="development",
                        title="null",
                        prompt="null prompt",
                        owned_paths=[],
                    ),
                ]
            )
            await session.flush()

            reserved = await adoption_module._task_external_id_family_idempotency_keys(
                session,
                logical_idempotency_key=logical_key,
                task_external_id=task_external_id,
            )

        assert reserved == [logical_key, f"{logical_key}:g2"]

    @pytest.mark.unit
    async def test_adoption_task_idempotency_key_exhaustion_is_explicit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _reserved_task_keys(
            _session: AsyncSession,
            *,
            logical_idempotency_key: str,
        ) -> list[str]:
            return [
                logical_idempotency_key,
                *(f"{logical_idempotency_key}:g{generation}" for generation in range(1, 1000)),
            ]

        async def _reserved_external_keys(
            _session: AsyncSession,
            *,
            logical_idempotency_key: str,
            task_external_id: str,
        ) -> list[str]:
            del logical_idempotency_key, task_external_id
            return []

        monkeypatch.setattr(adoption_module, "_task_idempotency_key_family", _reserved_task_keys)
        monkeypatch.setattr(
            adoption_module,
            "_task_external_id_family_idempotency_keys",
            _reserved_external_keys,
        )

        with pytest.raises(RuntimeError, match="fresh PR adoption task idempotency key"):
            await adoption_module._next_adoption_task_idempotency_key(
                None,  # type: ignore[arg-type]
                logical_idempotency_key="pr-adopt:exhausted",
                task_external_id="pr-adopt:external",
            )

    @pytest.mark.unit
    async def test_adoption_workspace_idempotency_key_merges_known_keys_with_family(
        self,
    ) -> None:
        class _WorkspaceRepo:
            calls: list[str]

            def __init__(self) -> None:
                self.calls = []

            async def list_idempotency_key_family(self, logical_key: str) -> list[str]:
                self.calls.append(logical_key)
                return [
                    "pr-adopt:logical:g2",
                    "pr-adopt:logical:g1000",
                ]

            async def get_by_idempotency_key(self, key: str) -> object | None:
                raise AssertionError(f"unexpected per-key lookup for {key}")

        repo = _WorkspaceRepo()

        key = await adoption_module._next_adoption_workspace_idempotency_key(
            repo,  # type: ignore[arg-type]
            logical_idempotency_key="pr-adopt:logical",
            known_workspace_keys=[
                "pr-adopt:logical",
                "pr-adopt:logical:g1",
                "pr-adopt:logical:g3",
                "pr-adopt:logical:g1000",
                "pr-adopt:logical:gignored",
            ],
            reserved_idempotency_keys=["pr-adopt:logical:g4"],
        )

        assert key == "pr-adopt:logical:g5"
        assert repo.calls == ["pr-adopt:logical"]

    @pytest.mark.unit
    async def test_adoption_workspace_idempotency_key_ignores_task_reservations_without_generation(
        self,
    ) -> None:
        class _WorkspaceRepo:
            async def list_idempotency_key_family(self, logical_key: str) -> list[str]:
                assert logical_key == "pr-adopt:logical"
                return []

        key = await adoption_module._next_adoption_workspace_idempotency_key(
            _WorkspaceRepo(),  # type: ignore[arg-type]
            logical_idempotency_key="pr-adopt:logical",
            reserved_idempotency_keys=[
                "pr-adopt:logical",
                "pr-adopt:logical:g1",
            ],
            require_generation=False,
        )

        assert key == "pr-adopt:logical"

    @pytest.mark.unit
    async def test_adoption_workspace_idempotency_key_exhaustion_is_explicit(self) -> None:
        class _ExhaustedWorkspaceRepo:
            async def list_idempotency_key_family(self, logical_key: str) -> list[str]:
                assert logical_key == "pr-adopt:exhausted"
                return []

        with pytest.raises(RuntimeError, match="fresh PR adoption workspace idempotency key"):
            await adoption_module._next_adoption_workspace_idempotency_key(
                _ExhaustedWorkspaceRepo(),  # type: ignore[arg-type]
                logical_idempotency_key="pr-adopt:exhausted",
                known_workspace_keys=[
                    "pr-adopt:exhausted",
                    *(f"pr-adopt:exhausted:g{generation}" for generation in range(1, 1000)),
                ],
            )
