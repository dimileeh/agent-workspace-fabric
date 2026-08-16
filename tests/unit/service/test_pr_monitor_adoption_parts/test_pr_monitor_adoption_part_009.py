"""PR monitor adoption coverage edges for identity/forge helpers.

Closes combined line+branch gaps exposed by CI on the adoption identity work:
generated-ID lineage edge cases, empty shared-ownership probes, forge gates,
allocation exhaustion, post-lock ownership races, and divergent supersession
releases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import Task, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import pr_monitor_adoption as adoption_module
from awf.service import pr_monitor_adoption_helpers as adoption_helpers
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


class TestGeneratedAdoptionExternalIdLineage:
    @pytest.mark.unit
    def test_none_and_non_lineage_values_are_not_generated_lineage(self) -> None:
        repo_slug = "dimileeh/aira-web"
        pr_number = 277
        generated = adoption_helpers._adoption_external_id(
            repo_slug=repo_slug,
            pr_number=pr_number,
        )

        assert (
            adoption_helpers._is_generated_adoption_external_id_lineage(
                existing_external_id=None,
                repo_slug=repo_slug,
                pr_number=pr_number,
            )
            is False
        )
        assert (
            adoption_helpers._is_generated_adoption_external_id_lineage(
                existing_external_id="CLOUD-TASK-42",
                repo_slug=repo_slug,
                pr_number=pr_number,
            )
            is False
        )
        assert (
            adoption_helpers._is_generated_adoption_external_id_lineage(
                existing_external_id=f"{generated}:gnotadigit",
                repo_slug=repo_slug,
                pr_number=pr_number,
            )
            is False
        )
        assert (
            adoption_helpers._is_generated_adoption_external_id_lineage(
                existing_external_id=f"{generated}:g3",
                repo_slug=repo_slug,
                pr_number=pr_number,
            )
            is True
        )

    @pytest.mark.unit
    def test_omitted_external_id_conflicts_when_existing_is_not_lineage(self) -> None:
        workspace = Workspace(task_external_id=None)
        request = PullRequestMonitorAdoptionRequest(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        assert (
            adoption_helpers._adoption_external_id_policy_conflicts(
                workspace,
                request,
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )
            is True
        )

        workspace.task_external_id = "explicit-not-generated"
        assert (
            adoption_helpers._adoption_external_id_policy_conflicts(
                workspace,
                request,
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )
            is True
        )


class TestAdoptionForgeGates:
    @pytest.mark.unit
    def test_raise_if_forge_unsupported_rejects_unknown_forge(self) -> None:
        repo = RepoRef(owner="acme", name="widget", forge=cast(Any, "gitlab"))
        with pytest.raises(PRMonitorAdoptionError) as excinfo:
            adoption_helpers._raise_if_forge_unsupported(repo)
        assert excinfo.value.error_code == "FORGE_NOT_SUPPORTED"
        assert excinfo.value.detail["forge"] == "gitlab"
        assert excinfo.value.detail["repo_slug"] == "acme/widget"

    @pytest.mark.unit
    async def test_default_metadata_fetcher_rejects_non_github_forge(self) -> None:
        repo = RepoRef(owner="acme", name="widget", forge="bitbucket")
        with pytest.raises(PRMonitorAdoptionError) as excinfo:
            await adoption_helpers._default_metadata_fetcher(repo=repo, pr_number=7)
        assert excinfo.value.error_code == "PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY"
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["forge"] == "bitbucket"
        assert excinfo.value.detail["repo_slug"] == "acme/widget"

    @pytest.mark.unit
    def test_pr_url_forge_gate_accepts_supported_bitbucket_host(self) -> None:
        # parse_github_pull_request_url rejects non-github hosts; the helper must
        # still re-parse bitbucket.org and pass a supported forge through.
        adoption_helpers._raise_if_pr_url_forge_unsupported(
            "https://bitbucket.org/acme/widget/pull-requests/7"
        )

    @pytest.mark.unit
    def test_adoption_workspace_forge_returns_none_for_unparseable_url(self) -> None:
        workspace = Workspace(repo_url="not-a-valid-repo-url")
        assert adoption_helpers._adoption_workspace_forge(workspace) is None

    @pytest.mark.unit
    def test_raise_if_adoption_forge_mismatch_rejects_cross_forge_attach(self) -> None:
        workspace = Workspace(
            id="ws_forge_mismatch",
            repo_url="git@github.com:acme/widget.git",
        )
        repo = RepoRef(owner="acme", name="widget", forge="bitbucket")
        with pytest.raises(PRMonitorAdoptionError) as excinfo:
            adoption_helpers._raise_if_adoption_forge_mismatch(workspace, repo=repo)
        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail["requested_forge"] == "bitbucket"
        assert excinfo.value.detail["existing_forge"] == "github"


class TestSharedOwnershipAndAllocation:
    @pytest.mark.unit
    async def test_shared_ownership_probe_short_circuits_on_empty_terminal_ids(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            assert (
                await adoption_helpers._task_has_shared_ownership_attempt(
                    session,
                    "task_unused",
                    terminal_workspace_ids=(),
                )
                is False
            )

    @pytest.mark.unit
    async def test_allocate_superseded_external_id_raises_when_all_slots_occupied(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def always_occupied(_session: AsyncSession, _external_id: str) -> bool:
            return True

        monkeypatch.setattr(
            adoption_helpers,
            "_task_external_id_occupied",
            always_occupied,
        )
        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await adoption_helpers._allocate_superseded_adoption_external_id(
                    session,
                    external_id="CLOUD-TASK-42",
                    workspace_id="ws_alloc_exhaust",
                )
        assert excinfo.value.error_code == "TASK_EXTERNAL_ID_CONFLICT"
        assert excinfo.value.detail["workspace_id"] == "ws_alloc_exhaust"

    @pytest.mark.unit
    async def test_adoption_owns_task_identity_false_when_lock_sees_key_change(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-LOCK-RACE",
                )
            )
            task = await session.get(Task, result.task_id)
            assert task is not None
            assert task.idempotency_key is not None
            original_get = session.get

            async def get_with_rewritten_key(model: type[Any], ident: Any, **kwargs: Any) -> Any:
                loaded = await original_get(model, ident, **kwargs)
                if model is Task and ident == task.id and loaded is not None:
                    loaded.idempotency_key = f"{task.idempotency_key}:rewritten"
                return loaded

            session.get = get_with_rewritten_key  # type: ignore[method-assign]
            assert (
                await adoption_helpers._adoption_owns_task_identity(
                    session,
                    task,
                    adoption_idempotency_key=task.idempotency_key,
                    workspace_id=result.workspace_id,
                )
                is False
            )


class TestSupersedeDivergentAndExhaustion:
    @pytest.mark.unit
    async def test_supersede_releases_divergent_workspace_and_task_external_ids(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-MATCH",
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            task = await session.get(Task, first.task_id)
            assert workspace is not None
            assert task is not None
            # Simulate stranded divergence: workspace slot moved, owned task kept prior ID.
            workspace.task_external_id = "CLOUD-TASK-WORKSPACE-ONLY"
            task.external_id = "CLOUD-TASK-MATCH"
            workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()
            logical_key = adoption_module.pr_adoption_idempotency_key(
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            )
            await service._supersede_previous_adoption(
                workspace=workspace,
                idempotency_key=logical_key,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )
            await session.commit()

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            task = await session.get(Task, first.task_id)
            assert workspace is not None
            assert task is not None
            assert workspace.task_external_id == (
                adoption_helpers._superseded_adoption_external_id(
                    external_id="CLOUD-TASK-WORKSPACE-ONLY",
                    workspace_id=first.workspace_id,
                )
            )
            assert task.external_id == (
                adoption_helpers._superseded_adoption_external_id(
                    external_id="CLOUD-TASK-MATCH",
                    workspace_id=first.workspace_id,
                )
            )

    @pytest.mark.unit
    async def test_supersede_raises_when_integrity_retries_exhausted(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-EXHAUST",
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()
            logical_key = adoption_module.pr_adoption_idempotency_key(
                repo_slug="dimileeh/aira-web",
                pr_number=277,
            )

        async def always_integrity(
            _session: AsyncSession,
            external_id: str | None,
            *,
            workspace_id: str,
        ) -> str | None:
            raise IntegrityError("stmt", {}, Exception("uq_tasks_external_id"))

        monkeypatch.setattr(
            adoption_module,
            "_release_superseded_adoption_external_id",
            always_integrity,
        )
        monkeypatch.setattr(
            adoption_helpers,
            "_release_superseded_adoption_external_id",
            always_integrity,
        )

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service._supersede_previous_adoption(
                    workspace=workspace,
                    idempotency_key=logical_key,
                    repo=RepoRef(owner="dimileeh", name="aira-web"),
                    pr_number=277,
                )
        assert excinfo.value.error_code == "TASK_EXTERNAL_ID_CONFLICT"
        assert excinfo.value.detail["workspace_id"] == first.workspace_id

    @pytest.mark.unit
    async def test_owned_generation_slot_with_explicit_id_raises_conflict(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit re-adoption must not mint a generation when the logical key is owned.

        Same PR title keeps task scope identical so ``create_or_get`` reuses the
        prior task (else-branch), then the owned-generation guard raises.
        """
        shared_title = "feature: owned-slot"
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title=shared_title)),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            generated_external_id = workspace.task_external_id
            assert generated_external_id is not None
            workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async def no_shared(
            _session: AsyncSession,
            _task_id: str,
            *,
            terminal_workspace_ids: object,
        ) -> bool:
            return False

        async def not_owned(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(
            adoption_module,
            "_task_has_shared_ownership_attempt",
            no_shared,
        )
        monkeypatch.setattr(
            adoption_helpers,
            "_task_has_shared_ownership_attempt",
            no_shared,
        )
        # Supersede rewrites the workspace key but leaves the task's logical key
        # and external_id intact when ownership is denied.
        monkeypatch.setattr(
            adoption_module,
            "_adoption_owns_task_identity",
            not_owned,
        )
        monkeypatch.setattr(
            adoption_helpers,
            "_adoption_owns_task_identity",
            not_owned,
        )

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=_MetadataFetcher(_metadata(title=shared_title)),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        external_id=generated_external_id,
                    )
                )
        assert excinfo.value.error_code == "TASK_EXTERNAL_ID_CONFLICT"
        assert excinfo.value.detail["external_id"] == generated_external_id

    @pytest.mark.unit
    async def test_owned_generation_slot_without_explicit_id_mints_generation(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Omitted external_id must mint a generation rather than reuse an owned slot.

        Same PR title keeps task scope identical so ``create_or_get`` reuses the
        prior task, then the owned-generation guard mints a ``:gN`` external id.
        """
        shared_title = "feature: owned-slot-mint"
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title=shared_title)),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async def no_shared(
            _session: AsyncSession,
            _task_id: str,
            *,
            terminal_workspace_ids: object,
        ) -> bool:
            return False

        async def not_owned(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(
            adoption_module,
            "_task_has_shared_ownership_attempt",
            no_shared,
        )
        monkeypatch.setattr(
            adoption_helpers,
            "_task_has_shared_ownership_attempt",
            no_shared,
        )
        monkeypatch.setattr(
            adoption_module,
            "_adoption_owns_task_identity",
            not_owned,
        )
        monkeypatch.setattr(
            adoption_helpers,
            "_adoption_owns_task_identity",
            not_owned,
        )

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title=shared_title)),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        assert second.task_id != first.task_id
        async with factory() as session:
            fresh = await WorkspaceRepository(session).get(second.workspace_id)
            assert fresh is not None
            assert fresh.task_external_id is not None
            assert ":g" in fresh.task_external_id
