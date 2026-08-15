"""PR monitor adoption service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.config import Settings
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    RepoRef,
)
from awf.db.enums import AgentRuntime, OperationType, WorkspaceStatus
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
from awf.service import config as service_config
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
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
                "execution": {"mode": "local"},
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
    async def test_omitted_auto_merge_reports_unresolved_until_provisioned(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # An adoption that omits an explicit intent must NOT report the
        # provisional ``DEFAULT_AUTO_MERGE`` seed as a resolved manual policy:
        # provisioning may resolve the setting to True from a trusted profile.
        # Until then the response surfaces ``auto_merge`` as unresolved (None).
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                )
            )
            await session.commit()

        assert result.status == WorkspaceStatus.requested
        assert result.auto_merge is None
        assert result.monitor_policy["auto_merge"] is None
        assert result.monitor_policy["auto_merge_intent"] is None
        assert result.monitor_policy["auto_merge_resolved"] is False

        # Once provisioning has resolved the flag (here, on to True), the
        # response reports the authoritative resolved value, not None.
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(result.workspace_id)
            assert workspace is not None
            await repo.transition(
                workspace, to=WorkspaceStatus.provisioning, reason_code="TEST_PROVISION"
            )
            workspace.auto_merge = True
            await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="TEST_READY")
            await session.commit()

        async with factory() as session:
            resumed = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                )
            )
            await session.commit()

        assert resumed.attached_existing is True
        assert resumed.auto_merge is True
        assert resumed.monitor_policy["auto_merge"] is True
        assert resumed.monitor_policy["auto_merge_intent"] is None
        assert resumed.monitor_policy["auto_merge_resolved"] is True

    @pytest.mark.unit
    @pytest.mark.parametrize("explicit_intent", [True, False])
    async def test_explicit_intent_reports_resolved_before_provisioning(
        self,
        factory: async_sessionmaker[AsyncSession],
        explicit_intent: bool,
    ) -> None:
        # An explicit ``auto_merge`` intent fixes the policy regardless of
        # provisioning status: ``resolve_auto_merge`` already has the final
        # answer, so an unresolved new-world row (still ``requested``) must
        # report ``auto_merge_resolved=True`` rather than contradicting the
        # surfaced ``auto_merge`` value with a stale ``False``.
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                    auto_merge=explicit_intent,
                )
            )
            await session.commit()

        assert result.status == WorkspaceStatus.requested
        assert result.auto_merge is explicit_intent
        assert result.monitor_policy["auto_merge"] is explicit_intent
        assert result.monitor_policy["auto_merge_intent"] is explicit_intent
        assert result.monitor_policy["auto_merge_resolved"] is True

    @pytest.mark.unit
    async def test_legacy_row_exposes_grandfathered_auto_merge_before_provisioning(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A pre-upgrade adoption row carries no ``auto_merge_intent`` key in its
        # ``task_policy``. The provisioner grandfathers such rows by preserving the
        # persisted ``auto_merge`` column instead of re-resolving it, so that column
        # is already authoritative even while the row is still ``requested``. The
        # response must expose the grandfathered value (and report it resolved),
        # not hide a merging ``True`` behind ``auto_merge=None``.
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                )
            )
            await session.commit()

        assert result.status == WorkspaceStatus.requested

        # Simulate a legacy in-flight row: strip the intent key and grandfather a
        # persisted ``auto_merge=True`` while the workspace is still ``requested``.
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(result.workspace_id)
            assert workspace is not None
            legacy_policy = {
                key: value
                for key, value in (workspace.task_policy or {}).items()
                if key != "auto_merge_intent"
            }
            workspace.task_policy = legacy_policy
            workspace.auto_merge = True
            await session.commit()

        async with factory() as session:
            resumed = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                )
            )
            await session.commit()

        assert resumed.attached_existing is True
        assert resumed.status == WorkspaceStatus.requested
        assert resumed.auto_merge is True
        assert resumed.monitor_policy["auto_merge"] is True
        assert resumed.monitor_policy["auto_merge_intent"] is None
        assert resumed.monitor_policy["auto_merge_resolved"] is True

    @pytest.mark.unit
    async def test_legacy_row_grandfathers_false_auto_merge_before_provisioning(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Sibling of the grandfathered-``True`` case above for the ``False`` branch of
        # ``_adoption_auto_merge_intent``: a pre-upgrade adoption row carries no
        # ``auto_merge_intent`` key, so a persisted ``auto_merge=False`` column is the
        # grandfathered, authoritative policy even while the row is still ``requested``.
        # The response must resolve it as ``False`` (not hide it behind ``None``), and
        # an omitted replay — whose historical default was ``True`` — must still
        # conflict with the persisted legacy ``False`` rather than spuriously attaching.
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                )
            )
            await session.commit()

        assert result.status == WorkspaceStatus.requested

        # Simulate a legacy in-flight row: strip the intent key and grandfather a
        # persisted ``auto_merge=False`` while the workspace is still ``requested``.
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(result.workspace_id)
            assert workspace is not None
            legacy_policy = {
                key: value
                for key, value in (workspace.task_policy or {}).items()
                if key != "auto_merge_intent"
            }
            workspace.task_policy = legacy_policy
            workspace.auto_merge = False
            await session.commit()

        # A replay carrying the matching explicit ``False`` intent attaches, and the
        # response surfaces the grandfathered ``False`` as resolved.
        async with factory() as session:
            resumed = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    agent="codex",
                    auto_merge=False,
                )
            )
            await session.commit()

        assert resumed.attached_existing is True
        assert resumed.status == WorkspaceStatus.requested
        assert resumed.auto_merge is False
        assert resumed.monitor_policy["auto_merge"] is False
        assert resumed.monitor_policy["auto_merge_intent"] is None
        assert resumed.monitor_policy["auto_merge_resolved"] is True

        # An omitted replay reconstructs the historical default ``True`` and must
        # conflict with the persisted legacy ``False`` intent instead of attaching.
        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=_MetadataFetcher(_metadata()),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        agent="codex",
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": result.workspace_id,
            "existing_auto_merge": False,
            "requested_auto_merge": None,
        }

    @pytest.mark.unit
    async def test_hosted_execution_policy_is_persisted_and_replay_attaches(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        settings = Settings(
            _env_file=None,
            hosted_delegation_base_url="https://hosted.example.test",
            hosted_delegation_bearer_token="hosted-token",
            workspace_steady_cpu=3.0,
            workspace_steady_memory_gb=5.0,
            workspace_peak_cpu=7.0,
            workspace_peak_memory_gb=11.0,
        )
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
                settings=settings,
            )
            first = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    execution={"mode": "hosted"},
                )
            )
            second = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    execution={"mode": "hosted"},
                )
            )
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert workspace is not None
            assert workspace.task_policy["pr_adoption"]["execution"] == {"mode": "hosted"}
            operation = (await session.execute(select(Operation))).scalar_one()
            assert operation.payload["execution"] == {"mode": "hosted"}
            event = next(
                event
                for event in workspace.events
                if event.event_type == "workspace.pr_monitor_adoption_requested"
            )
            assert event.payload["execution"] == {"mode": "hosted"}
            reservation = (await session.execute(select(ResourceReservation))).scalar_one()
            assert reservation.steady_cpu == 0.0
            assert reservation.steady_memory_gb == 0.0
            assert reservation.peak_cpu == 0.0
            assert reservation.peak_memory_gb == 0.0
            assert reservation.dind_slots == 0
            decision = (await session.execute(select(QueueDecision))).scalar_one()
            assert decision.resource_summary == {
                "node_id": "local",
                "steady_cpu": 0.0,
                "steady_memory_gb": 0.0,
                "peak_cpu": 0.0,
                "peak_memory_gb": 0.0,
                "disk_mb": None,
                "dind_slots": 0,
                "phase": "workspace_lifecycle",
                "dind_mode": "none",
            }

    @pytest.mark.unit
    async def test_hosted_execution_policy_requires_delegation_before_replay_attach(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        configured_settings = Settings(
            _env_file=None,
            hosted_delegation_base_url="https://hosted.example.test",
            hosted_delegation_bearer_token="hosted-token",
        )
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
                settings=configured_settings,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    execution={"mode": "hosted"},
                )
            )
            await session.commit()

        replay_fetcher = _MetadataFetcher(_metadata(head_ref="feature/should-not-fetch"))
        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=replay_fetcher,
                    settings=Settings(_env_file=None),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        execution={"mode": "hosted"},
                    )
                )

        assert first.workspace_id.startswith("ws_")
        assert excinfo.value.error_code == "HOSTED_DELEGATION_NOT_CONFIGURED"
        assert excinfo.value.detail == {
            "missing": [
                "AWF_HOSTED_DELEGATION_BASE_URL",
                "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV",
            ],
        }
        assert replay_fetcher.calls == []
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

    @pytest.mark.unit
    async def test_hosted_execution_policy_accepts_service_visible_token_env(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token_env = "AWF_PR_MONITOR_ADOPTION_HOSTED_TOKEN"
        monkeypatch.delenv(token_env, raising=False)
        fetcher = _MetadataFetcher(_metadata())
        settings = Settings(
            _env_file=None,
            hosted_delegation_base_url="https://hosted.example.test",
            hosted_delegation_bearer_token_env=token_env,
        )

        def _resolve_service_settings(base: Settings) -> service_config.ServiceSettings:
            assert base is settings
            return service_config.resolve_service_settings(
                base,
                environ={
                    "AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV": token_env,
                    token_env: "service-visible-token",
                },
            )

        monkeypatch.setattr(adoption_module, "resolve_service_settings", _resolve_service_settings)

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
                settings=settings,
            )
            response = await service.adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    execution={"mode": "hosted"},
                )
            )
            await session.commit()

        assert response.workspace_id.startswith("ws_")
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

    @pytest.mark.unit
    async def test_hosted_execution_policy_conflicts_with_existing_local_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        settings = Settings(
            _env_file=None,
            hosted_delegation_base_url="https://hosted.example.test",
            hosted_delegation_bearer_token="hosted-token",
        )
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
                settings=settings,
            )
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
                        execution={"mode": "hosted"},
                    )
                )

        assert first.workspace_id.startswith("ws_")
        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert excinfo.value.detail == {
            "workspace_id": first.workspace_id,
            "existing_execution": {"mode": "local"},
            "requested_execution": {"mode": "hosted"},
        }

    @pytest.mark.unit
    async def test_hosted_execution_policy_requires_configured_delegation(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
                settings=Settings(_env_file=None),
            )
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        execution={"mode": "hosted"},
                    )
                )

        assert excinfo.value.error_code == "HOSTED_DELEGATION_NOT_CONFIGURED"
        assert excinfo.value.detail == {
            "missing": [
                "AWF_HOSTED_DELEGATION_BASE_URL",
                "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV",
            ],
        }

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
    async def test_persists_task_tag_on_adopted_workspace(
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
                    task_tag="PROJ-123",
                )
            )
            await session.commit()

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            assert workspace is not None
            assert workspace.task_tag == "PROJ-123"

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
            # A genuine pre-change legacy row (no intent key in task_policy) was
            # persisted by an omitted auto_merge request back when the historical
            # default was True. Model the column accordingly so the reconstructed
            # intent matches the omitted replay instead of spuriously conflicting.
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

    async def test_adopt_pr_rejects_gemini_unsupported_runtime(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        request = PullRequestMonitorAdoptionRequest(
            repo_slug="acme/app",
            pr_number=123,
            agent=AgentRuntime.gemini,
        )
        assert request.agent == AgentRuntime.gemini

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(session, settings=Settings())
            with pytest.raises(PRMonitorAdoptionError) as exc_info:
                await service.adopt(request)
            assert exc_info.value.error_code == "UNSUPPORTED_AGENT_RUNTIME"
            assert "gemini" in str(exc_info.value)
