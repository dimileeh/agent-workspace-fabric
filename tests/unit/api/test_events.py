"""Workspace event API contract tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.events as events_route
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

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


async def _add_event(
    engine: AsyncEngine,
    workspace_id: str,
    event_type: str,
    *,
    reason_code: str = "TEST",
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.add_event(
            workspace,
            event_type=event_type,
            reason_code=reason_code,
            payload={"source": "test"},
        )
        await session.commit()


class TestListEvents:
    @pytest.mark.unit
    async def test_direct_call_returns_empty_envelope(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _Repo:
            def __init__(self, session: object) -> None:
                self.session = session

            async def list(self, *, workspace_id: str | None, limit: int) -> list[object]:
                assert workspace_id == "ws_missing"
                assert limit == 7
                return []

        monkeypatch.setattr(events_route, "WorkspaceEventRepository", _Repo)

        response = await events_route.list_events(
            workspace_id="ws_missing",
            limit=7,
            session=object(),  # type: ignore[arg-type]
        )

        assert response.items == []
        assert response.limit == 7
        assert response.cursor is None

    @pytest.mark.unit
    async def test_lists_all_events_newest_first(self, client: AsyncClient) -> None:
        first_id = await _create_workspace(client, "first")
        second_id = await _create_workspace(client, "second")

        response = await client.get("/v1/events")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == 50
        assert body["cursor"] is None
        assert [item["workspace_id"] for item in body["items"]] == [second_id, first_id]
        assert set(body["items"][0]) == {
            "id",
            "workspace_id",
            "event_type",
            "old_state",
            "new_state",
            "reason_code",
            "payload",
            "occurred_at",
        }
        assert body["items"][0]["event_type"] == "workspace.created"
        assert body["items"][0]["new_state"] == "requested"
        assert body["items"][0]["reason_code"] == "CREATED"

    @pytest.mark.unit
    async def test_filters_by_workspace_id(self, client: AsyncClient) -> None:
        first_id = await _create_workspace(client, "first")
        second_id = await _create_workspace(client, "second")

        response = await client.get("/v1/events", params={"workspace_id": first_id})

        assert response.status_code == 200
        body = response.json()
        assert [item["workspace_id"] for item in body["items"]] == [first_id]
        assert second_id not in {item["workspace_id"] for item in body["items"]}

    @pytest.mark.unit
    async def test_applies_limit(self, client: AsyncClient) -> None:
        await _create_workspace(client, "first")
        second_id = await _create_workspace(client, "second")

        response = await client.get("/v1/events", params={"limit": 1})

        assert response.status_code == 200
        body = response.json()
        assert [item["workspace_id"] for item in body["items"]] == [second_id]
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == 1
        assert body["cursor"] is None

    @pytest.mark.unit
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_validates_limit_bounds(self, client: AsyncClient, limit: int) -> None:
        response = await client.get("/v1/events", params={"limit": limit})

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_returns_empty_items_for_no_matches(self, client: AsyncClient) -> None:
        response = await client.get("/v1/events", params={"workspace_id": "ws_missing"})

        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "limit": 50,
            "cursor": None,
        }


class TestListWorkspaceEvents:
    @pytest.mark.unit
    async def test_lists_workspace_events_newest_first(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        first_id = await _create_workspace(client, "first")
        second_id = await _create_workspace(client, "second")
        await _add_event(engine, second_id, "workspace.phase_started")
        await _add_event(engine, first_id, "workspace.phase_started")

        response = await client.get(f"/v1/workspaces/{first_id}/events")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == 50
        assert body["cursor"] is None
        assert [item["workspace_id"] for item in body["items"]] == [first_id, first_id]
        assert [item["event_type"] for item in body["items"]] == [
            "workspace.phase_started",
            "workspace.created",
        ]

    @pytest.mark.unit
    async def test_returns_empty_items_for_known_workspace_with_no_matching_events(
        self,
        client: AsyncClient,
    ) -> None:
        workspace_id = await _create_workspace(client, "created only")

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/events",
            params={"event_type": "workspace.phase_started"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "limit": 50,
            "cursor": None,
        }

    @pytest.mark.unit
    async def test_returns_404_for_unknown_workspace(self, client: AsyncClient) -> None:
        response = await client.get("/v1/workspaces/ws_missing/events")

        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_validates_limit_bounds_like_global_events(
        self,
        client: AsyncClient,
        limit: int,
    ) -> None:
        response = await client.get(
            "/v1/workspaces/ws_missing/events",
            params={"limit": limit},
        )

        assert response.status_code == 422
