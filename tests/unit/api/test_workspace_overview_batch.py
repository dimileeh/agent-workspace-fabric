"""Batch workspace overview API contract tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.api.routes.workspaces as workspaces_route
from awf.api.schemas import WorkspaceOverviewBatchRequest
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_BATCH_PATH = "/v1/workspaces/overview/batch"
_CREATE_BODY = {
    "repo": {
        "url": "git@github.com:example/app.git",
        "base_branch": "development",
    },
    "task": {
        "title": "batch overview fixture",
        "prompt": "Create a workspace for batch overview tests.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "unit test bypasses provider auth",
    },
}


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _create_workspace(client: AsyncClient, *, title: str) -> str:
    body = {
        **_CREATE_BODY,
        "task": {**_CREATE_BODY["task"], "title": title},
    }
    response = await client.post(
        "/v1/workspaces",
        json=body,
        headers={"Idempotency-Key": f"batch-overview-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["workspace_id"])


@pytest.mark.unit
async def test_batch_overview_preserves_request_order(client: AsyncClient) -> None:
    first_id = await _create_workspace(client, title="batch first")
    second_id = await _create_workspace(client, title="batch second")
    # Request in reverse creation order so DB list order cannot accidentally match.
    response = await client.post(
        _BATCH_PATH,
        json={"workspace_ids": [second_id, first_id]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["workspace_id"] for item in payload["items"]] == [second_id, first_id]
    assert payload["missing_workspace_ids"] == []
    assert payload["items"][0]["title"] == "batch second"
    assert payload["items"][1]["title"] == "batch first"


@pytest.mark.unit
async def test_batch_overview_reports_missing_ids_in_request_order(client: AsyncClient) -> None:
    existing_id = await _create_workspace(client, title="batch exists")
    missing_a = "ws_missing_a"
    missing_b = "ws_missing_b"
    response = await client.post(
        _BATCH_PATH,
        json={"workspace_ids": [missing_a, existing_id, missing_b]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["workspace_id"] for item in payload["items"]] == [existing_id]
    assert payload["missing_workspace_ids"] == [missing_a, missing_b]
    assert all(item["workspace_id"] != missing_a for item in payload["items"])
    assert all(item["workspace_id"] != missing_b for item in payload["items"])


@pytest.mark.unit
async def test_batch_overview_route_direct_call_covers_handler(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_workspace(client, title="batch direct")
    async with session_factory() as session:
        response = await workspaces_route.batch_workspace_overview(
            payload=WorkspaceOverviewBatchRequest(workspace_ids=[workspace_id, "ws_gone"]),
            session=session,
        )
    assert [item.workspace_id for item in response.items] == [workspace_id]
    assert response.missing_workspace_ids == ["ws_gone"]
    assert response.items[0].title == "batch direct"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        {"workspace_ids": []},
        {"workspace_ids": [f"ws_{i}" for i in range(201)]},
        {"workspace_ids": ["ws_a", "ws_a"]},
        {},
        {"workspace_ids": "ws_a"},
        {"workspace_ids": [1]},
        {"workspace_ids": [""]},
    ],
)
async def test_batch_overview_rejects_invalid_payloads(
    client: AsyncClient,
    body: object,
) -> None:
    response = await client.post(_BATCH_PATH, json=body)
    assert response.status_code == 422, response.text


@pytest.mark.unit
@pytest.mark.parametrize("authorization", ["Bearer wrong-token", None])
async def test_batch_overview_requires_authorization(
    client: AsyncClient,
    authorization: str | None,
) -> None:
    default_authorization = client.headers.get("Authorization")
    workspace_id = await _create_workspace(client, title="batch auth")
    kwargs: dict[str, object] = {"json": {"workspace_ids": [workspace_id]}}
    sent_wrong_token = False
    try:
        if authorization is not None:
            kwargs["headers"] = {"Authorization": authorization}
            sent_wrong_token = True
        elif default_authorization is not None:
            del client.headers["Authorization"]
        response = await client.post(_BATCH_PATH, **kwargs)
    finally:
        if not sent_wrong_token and default_authorization is not None:
            client.headers["Authorization"] = default_authorization
    assert response.status_code == 401, response.text
