"""Event observability API contract tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


async def _create_workspace(client: AsyncClient, title: str) -> str:
    response = await client.post("/v1/workspaces", json={**_MINIMAL_BODY, "task_title": title})
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


class TestListEvents:
    @pytest.mark.unit
    async def test_returns_empty_page_when_no_events(self, client: AsyncClient) -> None:
        response = await client.get("/v1/events")

        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None, "has_more": False}

    @pytest.mark.unit
    async def test_lists_all_events_newest_first(self, client: AsyncClient) -> None:
        ids = [
            await _create_workspace(client, "first"),
            await _create_workspace(client, "second"),
            await _create_workspace(client, "third"),
        ]

        response = await client.get("/v1/events")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert [item["workspace_id"] for item in body["items"]] == list(reversed(ids))

        first = body["items"][0]
        assert set(first) == {
            "id",
            "workspace_id",
            "event_type",
            "old_state",
            "new_state",
            "reason_code",
            "payload",
            "occurred_at",
        }
        assert first["id"].startswith("evt_")
        assert first["event_type"] == "workspace.created"
        assert first["old_state"] is None
        assert first["new_state"] == "requested"
        assert first["reason_code"] == "CREATED"
        assert first["payload"] is None

    @pytest.mark.unit
    async def test_filters_events_by_workspace_id(self, client: AsyncClient) -> None:
        first_id = await _create_workspace(client, "first")
        second_id = await _create_workspace(client, "second")

        response = await client.get("/v1/events", params={"workspace_id": first_id})

        assert response.status_code == 200
        body = response.json()
        assert [item["workspace_id"] for item in body["items"]] == [first_id]
        assert second_id not in {item["workspace_id"] for item in body["items"]}
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    @pytest.mark.unit
    async def test_limit_caps_returned_events(self, client: AsyncClient) -> None:
        ids = [
            await _create_workspace(client, "first"),
            await _create_workspace(client, "second"),
            await _create_workspace(client, "third"),
        ]

        response = await client.get("/v1/events", params={"limit": 2})

        assert response.status_code == 200
        body = response.json()
        assert [item["workspace_id"] for item in body["items"]] == list(reversed(ids))[:2]
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    @pytest.mark.unit
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_rejects_limit_outside_supported_bounds(
        self, client: AsyncClient, limit: int
    ) -> None:
        response = await client.get("/v1/events", params={"limit": limit})

        assert response.status_code == 422
