"""Workspace event API contract tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.events as events_route
from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

_API_TOKEN = "unit-test-events-api-token"
_AUTH_HEADERS = {"Authorization": f"Bearer {_API_TOKEN}"}

_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "event API fixture",
    },
}


@pytest.fixture(autouse=True)
def _event_api_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWF_API_TOKEN", _API_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient, title: str) -> str:
    response = await client.post(
        "/v1/workspaces",
        json={**_MINIMAL_BODY, "task_title": title},
        headers=_AUTH_HEADERS,
    )
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

            async def list(
                self, *, workspace_id: str | None, event_type: str | None = None, limit: int
            ) -> list[object]:
                assert workspace_id == "ws_missing"
                assert limit == 8
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

        response = await client.get(
            "/v1/events",
            params={"event_type": "workspace.created"},
            headers=_AUTH_HEADERS,
        )

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

        response = await client.get(
            "/v1/events",
            params={"workspace_id": first_id, "event_type": "workspace.created"},
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["workspace_id"] for item in body["items"]] == [first_id]
        assert second_id not in {item["workspace_id"] for item in body["items"]}

    @pytest.mark.unit
    async def test_applies_limit(self, client: AsyncClient) -> None:
        await _create_workspace(client, "first")
        second_id = await _create_workspace(client, "second")

        response = await client.get(
            "/v1/events",
            params={"limit": 1, "event_type": "workspace.created"},
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["workspace_id"] for item in body["items"]] == [second_id]
        assert body["next_cursor"] is None
        assert body["limit"] == 1
        assert body["cursor"] is None

    @pytest.mark.unit
    async def test_has_more_true_when_more_events_exist(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        ws_id = await _create_workspace(client, "has-more-global")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="E1")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="E2")

        response = await client.get("/v1/events", params={"limit": 1}, headers=_AUTH_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["has_more"] is True
        assert len(body["items"]) == 1

    @pytest.mark.unit
    async def test_filters_by_event_type(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        ws_id = await _create_workspace(client, "event-type-filter")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="STARTED")
        await _add_event(engine, ws_id, "workspace.log", reason_code="LOG")

        response = await client.get(
            "/v1/events",
            params={"workspace_id": ws_id, "event_type": "workspace.phase_started"},
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) >= 1
        for item in body["items"]:
            assert item["event_type"] == "workspace.phase_started"

    @pytest.mark.unit
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_validates_limit_bounds(self, client: AsyncClient, limit: int) -> None:
        response = await client.get(
            "/v1/events",
            params={"limit": limit},
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_returns_empty_items_for_no_matches(self, client: AsyncClient) -> None:
        response = await client.get(
            "/v1/events",
            params={"workspace_id": "ws_missing"},
            headers=_AUTH_HEADERS,
        )

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

        response = await client.get(f"/v1/workspaces/{first_id}/events", headers=_AUTH_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == 50
        assert body["cursor"] is None
        assert body["items"][0]["workspace_id"] == first_id
        assert body["items"][0]["event_type"] == "workspace.phase_started"
        assert any(
            item["workspace_id"] == first_id and item["event_type"] == "workspace.created"
            for item in body["items"]
        )

    @pytest.mark.unit
    async def test_returns_empty_items_for_known_workspace_with_no_matching_events(
        self,
        client: AsyncClient,
    ) -> None:
        workspace_id = await _create_workspace(client, "created only")

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/events",
            params={"event_type": "workspace.phase_started"},
            headers=_AUTH_HEADERS,
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
        response = await client.get(
            "/v1/workspaces/ws_missing/events",
            headers=_AUTH_HEADERS,
        )

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
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_has_more_true_when_more_workspace_events_exist(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        ws_id = await _create_workspace(client, "has-more-ws")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="E1")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="E2")

        response = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={"limit": 1},
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["has_more"] is True
        assert len(body["items"]) == 1
