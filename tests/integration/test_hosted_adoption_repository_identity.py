"""Scoped adoption canonicalizes GitHub casing without relaxing source fences."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.db.models import Workspace
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_hosted_lifecycle_parity import fresh_association
from tests.integration.test_hosted_lifecycle_reauthorization import WS, quiet_failed
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("when", ["before", "during_metadata"])
@pytest.mark.parametrize("change", ["case", "forge", "repository", "pr"])
async def test_scoped_adoption_repository_identity_at_both_authority_checks(
    authority, create_client, create_application, tmp_path, when, change
):
    await quiet_failed(authority, create_client, tmp_path)
    runtime, value, factory = authority
    target = await fresh_association(runtime, value)

    async def mutate():
        async with factory.begin() as session:
            source = await session.get(Workspace, WS)
            if change == "case":
                source.repo_url = "https://github.com/Example/Repo.git"
            elif change == "forge":
                source.repo_url = "https://bitbucket.org/example/repo.git"
            elif change == "repository":
                source.repo_url = "https://github.com/example/other.git"
            else:
                source.pr_number = 43

    async def metadata(**kwargs):
        if when == "during_metadata":
            await mutate()
        return replace(
            _metadata(number=42),
            base_ref="main",
            head_repo_slug="example/repo",
            url="https://github.com/example/repo/pull/42",
        )

    fetcher = AsyncMock(side_effect=metadata)
    create_application[0].state.pr_adoption_metadata_fetcher = fetcher
    if when == "before":
        await mutate()
    body = {
        "pr_url": "https://github.com/example/repo/pull/42",
        "source_workspace_id": WS,
        "hosted": target,
    }
    client, headers = create_client
    result = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
    if change == "case":
        assert result.status_code == 202, result.text
        replay = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["workspace_id"] == result.json()["workspace_id"] == "ws_successor"
        assert replay.json()["attached_existing"] is True
        fetcher.assert_awaited_once()
        async with factory() as session:
            source = await session.get(Workspace, WS)
            destination = await session.get(Workspace, "ws_successor")
            assert source.repo_url == "https://github.com/Example/Repo.git"
            assert destination.task_policy["hosted_node"] == target
        assert (await runtime.workspace("ws_successor")).environment["GH_TOKEN"] == "fresh-github"
    else:
        assert result.status_code == 403, result.text
        assert result.json()["error_code"] == "HOSTED_SOURCE_REPOSITORY_MISMATCH"
        assert fetcher.await_count == int(when == "during_metadata")
        async with factory() as session:
            assert await session.get(Workspace, "ws_successor") is None
            assert len(list(await session.scalars(select(Workspace)))) == 1
