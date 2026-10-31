"""Adoption rejects missing authority and rechecks source after forge I/O."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from awf.api.schemas_pr_adoption import PullRequestMonitorAdoptionRequest
from awf.common.config import Settings
from awf.db.models import Operation, Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_authority import SESSION_KEY
from awf.service import pr_monitor_adoption
from tests.integration.test_hosted_lifecycle_parity import source_workspace
from tests.integration.test_hosted_lifecycle_successors import adoption
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


def metadata():
    return replace(
        _metadata(number=42),
        base_ref="main",
        head_repo_slug="example/repo",
        url="https://github.com/example/repo/pull/42",
    )


async def no_destination(factory, source_count):
    async with factory() as session:
        assert await session.get(Workspace, "ws_successor") is None
        assert await session.scalar(select(func.count()).select_from(Workspace)) == source_count
        assert await session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.parametrize("fault", ["missing-association", "missing-runtime"])
async def test_adoption_service_requires_runtime_and_association(authority, fault):
    runtime, _, factory = authority
    fetcher = AsyncMock(side_effect=AssertionError("forge must not be read"))
    request = PullRequestMonitorAdoptionRequest(pr_url="https://github.com/example/repo/pull/42")
    async with factory() as session:
        if fault == "missing-association":
            session.info[SESSION_KEY] = runtime
        settings = Settings(_env_file=None, hosted_node=runtime.receiver.config)
        service = pr_monitor_adoption.PullRequestMonitorAdoptionService(
            session, metadata_fetcher=fetcher, settings=settings
        )
        reason = (
            "HOSTED_WORKSPACE_REQUIRED"
            if fault == "missing-association"
            else "HOSTED_RUNTIME_INTEGRATION_REQUIRED"
        )
        with pytest.raises(MaterialDeniedError, match=reason):
            await service.adopt(request)
    fetcher.assert_not_awaited()
    await no_destination(factory, 0)


@pytest.mark.parametrize("drift", ["repository", "pr-number", "producer-claim"])
async def test_adoption_rechecks_source_after_forge_read(
    authority, create_client, create_application, tmp_path, drift
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    body = await adoption(create_application, authority, source="ws_cloud_stable")
    calls = 0

    async def fetch(*, repo, pr_number):
        nonlocal calls
        calls += 1
        assert repo.slug() == "example/repo" and pr_number == 42
        async with authority[2].begin() as session:
            source = await session.get(Workspace, "ws_cloud_stable")
            assert source is not None
            if drift == "repository":
                source.repo_url = "https://github.com/foreign/repo.git"
            elif drift == "pr-number":
                source.pr_number = 99
            else:
                source.execution_claimed_by = "existing-producer"
        return metadata()

    create_application[0].state.pr_adoption_metadata_fetcher = fetch
    response = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert calls == 1
    reason = (
        "HOSTED_SOURCE_NOT_QUIESCENT"
        if drift == "producer-claim"
        else "HOSTED_SOURCE_REPOSITORY_MISMATCH"
    )
    assert response.status_code == (409 if drift == "producer-claim" else 403), response.text
    assert reason in response.text
    await no_destination(authority[2], 1)


async def test_default_adoption_fetcher_receives_only_current_material_environment(
    authority, create_client, create_application, monkeypatch
):
    body = await adoption(create_application, authority)
    create_application[0].state.pr_adoption_metadata_fetcher = None
    observed = []

    async def fetch(*, repo, pr_number, provider_environ=None):
        assert repo.slug() == "example/repo" and pr_number == 42
        assert provider_environ is not None
        assert provider_environ["GH_TOKEN"] == "fresh-github"
        assert provider_environ["CODEX_API_KEY"] == "fresh-agent"
        assert provider_environ["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert provider_environ["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "AWS_SECRET_ACCESS_KEY" not in provider_environ
        observed.append(True)
        return metadata()

    monkeypatch.setenv("GH_TOKEN", "untrusted-inherited-host-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "untrusted-inherited-host-secret")
    monkeypatch.setattr(pr_monitor_adoption, "_default_metadata_fetcher", fetch)
    first = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert first.status_code == 202, first.text
    replay = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert replay.status_code == 202, replay.text
    assert observed == [True]
    assert replay.json()["attached_existing"] is True
    assert first.json()["workspace_id"] == replay.json()["workspace_id"] == "ws_successor"


async def test_unhosted_service_cannot_attach_hosted_adoption_history(
    authority, create_client, create_application
):
    body = await adoption(create_application, authority)
    first = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert first.status_code == 202, first.text
    fetcher = AsyncMock(side_effect=AssertionError("forge must not be read"))
    async with authority[2]() as session:
        session.info.pop(SESSION_KEY, None)
        service = pr_monitor_adoption.PullRequestMonitorAdoptionService(
            session, settings=Settings(_env_file=None), metadata_fetcher=fetcher
        )
        with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_INTEGRATION_REQUIRED"):
            await service.adopt(PullRequestMonitorAdoptionRequest(pr_url=body["pr_url"]))
    fetcher.assert_not_awaited()
    async with authority[2]() as session:
        assert await session.scalar(select(func.count()).select_from(Workspace)) == 1
        row = await session.get(Workspace, "ws_successor")
        assert row is not None and row.task_policy["hosted_node"] == body["hosted"]
