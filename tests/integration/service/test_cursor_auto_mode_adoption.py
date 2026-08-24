"""PR-adoption integration coverage for Cursor Auto routing modes."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.config import Settings
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.service import pr_monitor_adoption as adoption_module
from awf.service import workspaces_create
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from awf.service.pr_monitor_adoption_cursor_preflight import (
    run_deferred_cursor_auto_mode_provider_preflight,
)
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request() -> PullRequestMonitorAdoptionRequest:
    return PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
        agent="cursor",
        cursor_auto_mode="intelligence",
    )


async def _metadata_fetcher(*, repo: RepoRef, pr_number: int) -> PullRequestAdoptionMetadata:
    assert repo.slug() == "dimileeh/aira-web"
    assert pr_number == 277
    return PullRequestAdoptionMetadata(
        number=277,
        head_ref="feature/cursor-router",
        head_repo_slug="dimileeh/aira-web",
        base_ref="development",
        head_sha="h" * 40,
        base_sha="b" * 40,
        state="OPEN",
        is_draft=False,
        closed=False,
        merged=False,
        author="octocat",
        url="https://github.com/dimileeh/aira-web/pull/277",
        title="feature: Cursor Router",
    )


async def test_adoption_persists_successful_cursor_router_preflight(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = {
        "readiness_status": "ready",
        "reason_code": "PROVIDER_READY",
        "probe_status": "ok",
        "blocks_launch": False,
    }

    async def _ready(*_args: object, **_kwargs: object) -> dict[str, object]:
        return preflight

    monkeypatch.setattr(adoption_module, "_cursor_auto_mode_provider_preflight", _ready)
    async with factory() as session:
        result = await PullRequestMonitorAdoptionService(
            session,
            metadata_fetcher=_metadata_fetcher,
            settings=Settings(_env_file=None),
        ).adopt(_request())
        await session.commit()

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(result.workspace_id)

    assert workspace is not None
    assert workspace.task_policy["cursor_auto_mode"] == "intelligence"
    assert workspace.task_policy["provider_readiness_preflight"] == preflight


async def test_auto_profile_adoption_defers_then_probes_with_resolved_credentials(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto adoption waits for checkout profile credentials before probing Router."""
    probe_environments: list[dict[str, str]] = []

    async def _ready(
        *_args: object,
        provider_environ: dict[str, str],
        **_kwargs: object,
    ) -> dict[str, object]:
        probe_environments.append(provider_environ)
        return {"readiness_status": "ready", "blocks_launch": False}

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _ready,
    )
    monkeypatch.setenv("CURSOR_API_KEY", "worker-cursor-key")
    async with factory() as session:
        adopted = await PullRequestMonitorAdoptionService(
            session,
            metadata_fetcher=_metadata_fetcher,
            settings=Settings(_env_file=None),
        ).adopt(_request())
        await session.commit()

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(adopted.workspace_id)

    assert workspace is not None
    assert "provider_readiness_preflight" not in (workspace.task_policy or {})
    preflight = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=workspace.agent,
        task_policy=workspace.task_policy,
        resolved_profile=WorkspaceProfile.model_validate(
            {
                "name": "checkout-profile",
                "runtime": {"environment": {"CURSOR_API_KEY": "profile-cursor-key"}},
            }
        ).model_dump(mode="json", by_alias=True),
        settings=Settings(_env_file=None),
    )

    assert preflight == {"readiness_status": "ready", "blocks_launch": False}
    assert probe_environments[0]["CURSOR_API_KEY"] == "profile-cursor-key"


async def test_live_adoption_reattaches_without_reprobing_cursor_router(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_calls = 0

    async def _ready(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal probe_calls
        probe_calls += 1
        return {"readiness_status": "ready", "blocks_launch": False}

    monkeypatch.setattr(adoption_module, "_cursor_auto_mode_provider_preflight", _ready)
    async with factory() as session:
        first = await PullRequestMonitorAdoptionService(
            session,
            metadata_fetcher=_metadata_fetcher,
            settings=Settings(_env_file=None),
        ).adopt(_request())
        await session.commit()

    async def _unexpected_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("live adoption replay must not rerun the Router catalog probe")

    async def _unexpected_fetcher(*, repo: RepoRef, pr_number: int) -> PullRequestAdoptionMetadata:
        del repo, pr_number
        raise AssertionError("live adoption replay must not refetch PR metadata")

    monkeypatch.setattr(
        adoption_module,
        "_cursor_auto_mode_provider_preflight",
        _unexpected_probe,
    )
    async with factory() as session:
        second = await PullRequestMonitorAdoptionService(
            session,
            metadata_fetcher=_unexpected_fetcher,
            settings=Settings(_env_file=None),
        ).adopt(_request())

    assert first.workspace_id == second.workspace_id
    assert second.attached_existing is True
    assert probe_calls == 1
