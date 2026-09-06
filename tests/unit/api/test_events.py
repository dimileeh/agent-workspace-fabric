"""Workspace event API contract tests."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.events as events_route
import awf.api.routes.workspaces as workspaces_route
from awf.api.schemas import WorkspaceEventResponse
from awf.common.config import get_settings
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.events import (
    MAX_WORKSPACE_EVENT_LIST_CURSOR_LEN,
    decode_workspace_event_list_cursor,
    encode_workspace_event_cursor,
)

_API_TOKEN = "unit-test-events-api-token"
_AUTH_HEADERS = {"Authorization": f"Bearer {_API_TOKEN}"}
# Matches awf.common.ids.new_event_id() / EVENT_ID_PATTERN (evt_ + 24 hex).
_VALID_EVENT_ID = "evt_0123456789abcdef01234567"

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
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        event = await repo.add_event(
            workspace,
            event_type=event_type,
            reason_code=reason_code,
            payload={"source": "test"},
        )
        await session.commit()
        return str(event.id)


async def _set_occurred_at(
    engine: AsyncEngine,
    event_ids: list[str],
    occurred_at: datetime,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            update(WorkspaceEvent)
            .where(WorkspaceEvent.id.in_(event_ids))
            .values(occurred_at=occurred_at)
        )
        await session.commit()


async def _list_all_workspace_event_ids(engine: AsyncEngine, workspace_id: str) -> list[str]:
    factory = make_session_factory(engine)
    async with factory() as session:
        rows = await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=500)
        return [row.id for row in rows]


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
        assert body["next_cursor"] is not None
        assert len(body["items"]) == 1
        for item in body["items"]:
            assert "workspace_id" in item
            assert "event_type" in item
            assert "reason_code" in item
        assert "has_more" in body
        assert "next_cursor" in body

    @pytest.mark.unit
    async def test_complete_multi_page_traversal(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        ws_id = await _create_workspace(client, "multi-page")
        for index in range(5):
            await _add_event(
                engine,
                ws_id,
                "workspace.phase_started",
                reason_code=f"E{index}",
            )

        expected_ids = set(await _list_all_workspace_event_ids(engine, ws_id))
        assert len(expected_ids) >= 6

        collected: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, object] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(
                f"/v1/workspaces/{ws_id}/events",
                params=params,
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 200
            body = response.json()
            pages += 1
            page_ids = [item["id"] for item in body["items"]]
            assert len(page_ids) == len(set(page_ids))
            collected.extend(page_ids)
            if body["has_more"]:
                assert body["next_cursor"] is not None
                assert body["cursor"] == cursor
                cursor = body["next_cursor"]
                continue
            assert body["next_cursor"] is None
            break

        assert pages > 1
        assert set(collected) == expected_ids
        assert len(collected) == len(expected_ids)

    @pytest.mark.unit
    async def test_equal_timestamps_advance_by_id(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        ws_id = await _create_workspace(client, "equal-ts")
        first = await _add_event(engine, ws_id, "workspace.phase_started", reason_code="A")
        second = await _add_event(engine, ws_id, "workspace.phase_started", reason_code="B")
        third = await _add_event(engine, ws_id, "workspace.phase_started", reason_code="C")
        shared = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        await _set_occurred_at(engine, [first, second, third], shared)

        collected: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {
                "limit": 1,
                "event_type": "workspace.phase_started",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(
                f"/v1/workspaces/{ws_id}/events",
                params=params,
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 200
            body = response.json()
            collected.extend(item["id"] for item in body["items"])
            if not body["has_more"]:
                assert body["next_cursor"] is None
                break
            assert body["next_cursor"] is not None
            cursor = body["next_cursor"]

        assert collected == sorted([first, second, third], reverse=True)

    @pytest.mark.unit
    async def test_inserts_between_pages_do_not_dup_or_skip_preexisting(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        ws_id = await _create_workspace(client, "insert-between")
        preexisting: list[str] = []
        for index in range(4):
            preexisting.append(
                await _add_event(
                    engine,
                    ws_id,
                    "workspace.phase_started",
                    reason_code=f"P{index}",
                )
            )
        preexisting_set = set(preexisting)

        first_page = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={"limit": 2, "event_type": "workspace.phase_started"},
            headers=_AUTH_HEADERS,
        )
        assert first_page.status_code == 200
        first_body = first_page.json()
        assert first_body["has_more"] is True
        assert first_body["next_cursor"] is not None
        first_ids = {item["id"] for item in first_body["items"]}

        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="NEWER")

        second_page = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={
                "limit": 10,
                "event_type": "workspace.phase_started",
                "cursor": first_body["next_cursor"],
            },
            headers=_AUTH_HEADERS,
        )
        assert second_page.status_code == 200
        second_body = second_page.json()
        second_ids = {item["id"] for item in second_body["items"]}

        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == preexisting_set
        assert "NEWER" not in {item["reason_code"] for item in second_body["items"]}

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "cursor",
        [
            "not-valid-base64!!!",
            "",
            # One past the decoder's accept bound.
            "a" * (MAX_WORKSPACE_EVENT_LIST_CURSOR_LEN + 1),
        ],
    )
    async def test_invalid_cursors_return_fixed_invalid_cursor_error(
        self,
        client: AsyncClient,
        cursor: str,
    ) -> None:
        ws_id = await _create_workspace(client, "bad-cursor")
        response = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={"cursor": cursor},
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "INVALID_CURSOR"
        assert detail["message"] == "Invalid workspace event list cursor."
        # Fixed message must not echo the supplied token (skip empty, which is a
        # trivial substring of any JSON body).
        if cursor:
            assert cursor not in response.text
            assert cursor not in detail["message"]

    @pytest.mark.unit
    async def test_offset_naive_cursor_timestamp_returns_invalid_cursor(
        self,
        client: AsyncClient,
    ) -> None:
        ws_id = await _create_workspace(client, "naive-cursor")
        payload = {
            "o": "2024-05-06T07:08:09",
            "i": _VALID_EVENT_ID,
            "w": ws_id,
            "e": None,
        }
        cursor = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        response = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={"cursor": cursor},
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "INVALID_CURSOR"
        assert detail["message"] == "Invalid workspace event list cursor."
        assert cursor not in response.text
        assert cursor not in detail["message"]

    @pytest.mark.unit
    async def test_nul_event_id_cursor_returns_invalid_cursor_not_db_error(
        self,
        client: AsyncClient,
    ) -> None:
        """NUL in cursor i must not reach PostgreSQL as before_event_id."""
        ws_id = await _create_workspace(client, "nul-event-id")
        payload = {
            "o": "2024-05-06T07:08:09+00:00",
            "i": "\x00",
            "w": ws_id,
            "e": None,
        }
        cursor = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        response = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={"cursor": cursor},
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "INVALID_CURSOR"
        assert detail["message"] == "Invalid workspace event list cursor."
        assert cursor not in response.text
        assert cursor not in detail["message"]

    @pytest.mark.unit
    async def test_cursor_scope_bound_to_workspace_and_event_type(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        first_id = await _create_workspace(client, "scope-a")
        second_id = await _create_workspace(client, "scope-b")
        await _add_event(engine, first_id, "workspace.phase_started", reason_code="A1")
        await _add_event(engine, first_id, "workspace.phase_started", reason_code="A2")
        await _add_event(engine, first_id, "workspace.log", reason_code="L1")

        page = await client.get(
            f"/v1/workspaces/{first_id}/events",
            params={"limit": 1, "event_type": "workspace.phase_started"},
            headers=_AUTH_HEADERS,
        )
        assert page.status_code == 200
        cursor = page.json()["next_cursor"]
        assert cursor is not None

        cross_workspace = await client.get(
            f"/v1/workspaces/{second_id}/events",
            params={"limit": 1, "event_type": "workspace.phase_started", "cursor": cursor},
            headers=_AUTH_HEADERS,
        )
        assert cross_workspace.status_code == 400
        assert cross_workspace.json()["detail"]["error_code"] == "INVALID_CURSOR"
        assert cursor not in cross_workspace.text

        cross_filter = await client.get(
            f"/v1/workspaces/{first_id}/events",
            params={"limit": 1, "event_type": "workspace.log", "cursor": cursor},
            headers=_AUTH_HEADERS,
        )
        assert cross_filter.status_code == 400
        assert cross_filter.json()["detail"]["error_code"] == "INVALID_CURSOR"

        no_filter = await client.get(
            f"/v1/workspaces/{first_id}/events",
            params={"limit": 1, "cursor": cursor},
            headers=_AUTH_HEADERS,
        )
        assert no_filter.status_code == 400
        assert no_filter.json()["detail"]["error_code"] == "INVALID_CURSOR"

    @pytest.mark.unit
    @pytest.mark.parametrize("authorization", ["Bearer wrong-token", None])
    async def test_auth_denial_without_valid_bearer(
        self,
        client: AsyncClient,
        authorization: str | None,
    ) -> None:
        ws_id = await _create_workspace(client, "auth-events")
        default_authorization = client.headers.get("Authorization")
        kwargs: dict[str, object] = {}
        sent_wrong_token = False
        if authorization is not None:
            kwargs["headers"] = {"Authorization": authorization}
            sent_wrong_token = True
        try:
            if not sent_wrong_token and default_authorization is not None:
                del client.headers["Authorization"]
            response = await client.get(f"/v1/workspaces/{ws_id}/events", **kwargs)  # type: ignore[arg-type]
        finally:
            if not sent_wrong_token and default_authorization is not None:
                client.headers["Authorization"] = default_authorization
        assert response.status_code == 401

    @pytest.mark.unit
    async def test_repository_receives_keyset_bounds_for_cursor(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _create_workspace(client, "bounded-query")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="B1")
        await _add_event(engine, ws_id, "workspace.phase_started", reason_code="B2")

        first = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={"limit": 1, "event_type": "workspace.phase_started"},
            headers=_AUTH_HEADERS,
        )
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        decoded = decode_workspace_event_list_cursor(
            cursor,
            workspace_id=ws_id,
            event_type="workspace.phase_started",
        )
        assert decoded is not None

        captured: dict[str, object] = {}
        real_list = WorkspaceEventRepository.list

        async def _capturing_list(self: WorkspaceEventRepository, **kwargs: object) -> object:
            captured.update(kwargs)
            return await real_list(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(WorkspaceEventRepository, "list", _capturing_list)
        monkeypatch.setattr(workspaces_route, "WorkspaceEventRepository", WorkspaceEventRepository)

        second = await client.get(
            f"/v1/workspaces/{ws_id}/events",
            params={
                "limit": 1,
                "event_type": "workspace.phase_started",
                "cursor": cursor,
            },
            headers=_AUTH_HEADERS,
        )
        assert second.status_code == 200
        assert captured["limit"] == 2
        assert captured["before_occurred_at"] == decoded.occurred_at
        assert captured["before_event_id"] == decoded.event_id
        assert captured["workspace_id"] == ws_id
        assert captured["event_type"] == "workspace.phase_started"


class TestWorkspaceEventCursorHelpers:
    @pytest.mark.unit
    def test_encode_decode_round_trip_and_scope_mismatch(self) -> None:
        event = WorkspaceEventResponse(
            id=_VALID_EVENT_ID,
            workspace_id="ws_1",
            event_type="workspace.phase_started",
            old_state=None,
            new_state=None,
            reason_code="TEST",
            payload=None,
            occurred_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
        )
        cursor = encode_workspace_event_cursor(
            event,
            workspace_id="ws_1",
            event_type="workspace.phase_started",
        )
        decoded = decode_workspace_event_list_cursor(
            cursor,
            workspace_id="ws_1",
            event_type="workspace.phase_started",
        )
        assert decoded is not None
        assert decoded.event_id == _VALID_EVENT_ID
        assert decoded.occurred_at == event.occurred_at

        with pytest.raises(InvalidBoundedListCursorError):
            decode_workspace_event_list_cursor(
                cursor,
                workspace_id="ws_other",
                event_type="workspace.phase_started",
            )
        with pytest.raises(InvalidBoundedListCursorError):
            decode_workspace_event_list_cursor(
                cursor,
                workspace_id="ws_1",
                event_type=None,
            )
        with pytest.raises(InvalidBoundedListCursorError):
            decode_workspace_event_list_cursor(
                "",
                workspace_id="ws_1",
                event_type=None,
            )

    @pytest.mark.unit
    def test_decode_rejects_non_alphabet_characters_in_cursor(self) -> None:
        """urlsafe_b64decode discards non-alphabet chars unless validate=True."""
        event = WorkspaceEventResponse(
            id=_VALID_EVENT_ID,
            workspace_id="ws_1",
            event_type="workspace.phase_started",
            old_state=None,
            new_state=None,
            reason_code="TEST",
            payload=None,
            occurred_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
        )
        cursor = encode_workspace_event_cursor(
            event,
            workspace_id="ws_1",
            event_type="workspace.phase_started",
        )
        dirty = cursor[:10] + "!!!!" + cursor[10:]
        with pytest.raises(
            InvalidBoundedListCursorError, match="Invalid workspace event list cursor"
        ):
            decode_workspace_event_list_cursor(
                dirty,
                workspace_id="ws_1",
                event_type="workspace.phase_started",
            )

    @pytest.mark.unit
    def test_non_ascii_event_type_cursor_stays_within_bound(self) -> None:
        """Max-length non-ASCII event_type must not inflate past the cursor cap."""
        event_type = "é" * 64
        event = WorkspaceEventResponse(
            id=_VALID_EVENT_ID,
            workspace_id="ws_1",
            event_type=event_type,
            old_state=None,
            new_state=None,
            reason_code="TEST",
            payload=None,
            occurred_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
        )
        cursor = encode_workspace_event_cursor(
            event,
            workspace_id="ws_1",
            event_type=event_type,
        )
        assert len(cursor) <= MAX_WORKSPACE_EVENT_LIST_CURSOR_LEN
        decoded = decode_workspace_event_list_cursor(
            cursor,
            workspace_id="ws_1",
            event_type=event_type,
        )
        assert decoded is not None
        assert decoded.event_id == _VALID_EVENT_ID
        assert decoded.occurred_at == event.occurred_at

    @pytest.mark.unit
    def test_c0_event_type_cursor_stays_within_bound_and_round_trips(self) -> None:
        """JSON always escapes C0 as \\uXXXX; 64 controls must still paginate."""
        # Generated-format IDs (ws_/evt_ + 24 hex) — the encode path's real shape.
        workspace_id = "ws_0123456789abcdef01234567"
        event_id = _VALID_EVENT_ID
        event_type = "\x01" * 64
        event = WorkspaceEventResponse(
            id=event_id,
            workspace_id=workspace_id,
            event_type=event_type,
            old_state=None,
            new_state=None,
            reason_code="TEST",
            payload=None,
            occurred_at=datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC),
        )
        cursor = encode_workspace_event_cursor(
            event,
            workspace_id=workspace_id,
            event_type=event_type,
        )
        assert len(cursor) > 512  # proves the old 512 gate was too tight
        assert len(cursor) <= MAX_WORKSPACE_EVENT_LIST_CURSOR_LEN
        decoded = decode_workspace_event_list_cursor(
            cursor,
            workspace_id=workspace_id,
            event_type=event_type,
        )
        assert decoded is not None
        assert decoded.event_id == event_id
        assert decoded.occurred_at == event.occurred_at

    @pytest.mark.unit
    def test_decode_rejects_offset_naive_timestamp(self) -> None:
        payload = {
            "o": "2024-05-06T07:08:09",
            "i": _VALID_EVENT_ID,
            "w": "ws_1",
            "e": "workspace.phase_started",
        }
        cursor = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        with pytest.raises(
            InvalidBoundedListCursorError, match="Invalid workspace event list cursor"
        ):
            decode_workspace_event_list_cursor(
                cursor,
                workspace_id="ws_1",
                event_type="workspace.phase_started",
            )

    @staticmethod
    def _encode_payload(payload: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )

    @pytest.mark.unit
    def test_decode_rejects_non_object_json_payload(self) -> None:
        cursor = self._encode_payload(["not", "an", "object"])
        with pytest.raises(
            InvalidBoundedListCursorError, match="Invalid workspace event list cursor"
        ):
            decode_workspace_event_list_cursor(
                cursor,
                workspace_id="ws_1",
                event_type="workspace.phase_started",
            )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("payload", "event_type"),
        [
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": "",
                    "w": "ws_1",
                    "e": "workspace.phase_started",
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": 123,
                    "w": "ws_1",
                    "e": "workspace.phase_started",
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    # Nonempty but not the generated evt_ + 24-hex form (and DB-unsafe NUL).
                    "i": "\x00",
                    "w": "ws_1",
                    "e": "workspace.phase_started",
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": "evt_1",
                    "w": "ws_1",
                    "e": "workspace.phase_started",
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": _VALID_EVENT_ID,
                    "w": "",
                    "e": "workspace.phase_started",
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": _VALID_EVENT_ID,
                    "w": 99,
                    "e": "workspace.phase_started",
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": _VALID_EVENT_ID,
                    "w": "ws_1",
                    "e": 7,
                },
                "workspace.phase_started",
            ),
            (
                {
                    "o": "2024-05-06T07:08:09+00:00",
                    "i": _VALID_EVENT_ID,
                    "w": "ws_1",
                    "e": "",
                },
                "",
            ),
        ],
    )
    def test_decode_rejects_malformed_cursor_fields(
        self,
        payload: dict[str, object],
        event_type: str,
    ) -> None:
        cursor = self._encode_payload(payload)
        with pytest.raises(
            InvalidBoundedListCursorError, match="Invalid workspace event list cursor"
        ):
            decode_workspace_event_list_cursor(
                cursor,
                workspace_id="ws_1",
                event_type=event_type,
            )
