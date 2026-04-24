"""Event observability API contract tests.

GET /v1/events returns workspace events newest-first with optional
``workspace_id`` filtering and a bounded ``limit``. The response
envelope mirrors the pagination shape called out in the PRD
(``items`` + ``next_cursor`` + ``has_more``), but this slice doesn't
wire up cursor-based pagination yet — ``next_cursor`` is always
``null`` and ``has_more`` always ``false``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


async def _create_workspace(client: AsyncClient, **overrides: object) -> str:
    body = {**_MINIMAL_BODY, **overrides}
    response = await client.post("/v1/workspaces", json=body)
    assert response.status_code == 202
    return response.json()["workspace_id"]


class TestListEvents:
    @pytest.mark.unit
    async def test_returns_empty_envelope_when_no_events(self, client: AsyncClient) -> None:
        response = await client.get("/v1/events")
        assert response.status_code == 200
        body = response.json()
        assert body == {"items": [], "next_cursor": None, "has_more": False}

    @pytest.mark.unit
    async def test_returns_creation_event_shape(self, client: AsyncClient) -> None:
        ws_id = await _create_workspace(client, task_title="event-shape")

        response = await client.get("/v1/events")
        assert response.status_code == 200

        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert len(body["items"]) == 1

        event = body["items"][0]
        assert event["id"].startswith("evt_")
        assert event["workspace_id"] == ws_id
        assert event["event_type"] == "workspace.created"
        assert event["old_state"] is None
        assert event["new_state"] == "requested"
        assert event["reason_code"] == "CREATED"
        assert event["payload"] is None
        assert "occurred_at" in event

    @pytest.mark.unit
    async def test_newest_first_across_workspaces(self, client: AsyncClient) -> None:
        ids: list[str] = []
        for title in ["first", "second", "third"]:
            ids.append(await _create_workspace(client, task_title=title))

        response = await client.get("/v1/events")
        assert response.status_code == 200

        body = response.json()
        returned_ws_ids = [e["workspace_id"] for e in body["items"]]
        # Each create emits exactly one event; newest creation first.
        assert returned_ws_ids == list(reversed(ids))

    @pytest.mark.unit
    async def test_workspace_id_filter(self, client: AsyncClient) -> None:
        keep_id = await _create_workspace(client, task_title="keep")
        await _create_workspace(client, task_title="drop")

        response = await client.get("/v1/events", params={"workspace_id": keep_id})
        assert response.status_code == 200

        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["workspace_id"] == keep_id

    @pytest.mark.unit
    async def test_workspace_id_filter_unknown_id_returns_empty(self, client: AsyncClient) -> None:
        await _create_workspace(client)

        response = await client.get("/v1/events", params={"workspace_id": "ws_nope"})
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None, "has_more": False}

    @pytest.mark.unit
    async def test_default_limit_is_50(self, client: AsyncClient) -> None:
        # Each creation emits a single event, so 60 workspaces == 60 events.
        for i in range(60):
            await _create_workspace(client, task_title=f"n-{i}")

        response = await client.get("/v1/events")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 50

    @pytest.mark.unit
    async def test_custom_limit_is_respected(self, client: AsyncClient) -> None:
        for i in range(5):
            await _create_workspace(client, task_title=f"n-{i}")

        response = await client.get("/v1/events", params={"limit": 3})
        assert response.status_code == 200
        assert len(response.json()["items"]) == 3

    @pytest.mark.unit
    async def test_limit_zero_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/v1/events", params={"limit": 0})
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_limit_over_max_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/v1/events", params={"limit": 501})
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_limit_at_max_is_accepted(self, client: AsyncClient) -> None:
        response = await client.get("/v1/events", params={"limit": 500})
        assert response.status_code == 200
