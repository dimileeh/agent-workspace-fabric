"""Cross-endpoint pagination envelope contract tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

_ENVELOPE_KEYS = {"items", "next_cursor", "has_more", "limit", "cursor"}


def _assert_standard_envelope(
    body: dict[str, object],
    *,
    limit: int,
    cursor: str | None = None,
) -> None:
    assert set(body) >= _ENVELOPE_KEYS
    assert isinstance(body["items"], list)
    assert body["limit"] == limit
    assert body["cursor"] == cursor
    assert "next_cursor" in body
    assert isinstance(body["has_more"], bool)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "params", "expected_limit", "expected_cursor"),
    [
        ("/v1/events", {"limit": 7}, 7, None),
        ("/v1/tasks", {"limit": 7}, 7, None),
        ("/v1/operations", {"limit": 7}, 7, None),
        ("/v1/merge-queue", {"limit": 7}, 7, None),
        ("/v1/locks", {"limit": 7}, 7, None),
        ("/v1/workspaces/overview", {"limit": 7, "cursor": "opaque"}, 7, "opaque"),
    ],
)
async def test_enveloped_list_endpoints_expose_standard_pagination_metadata(
    client: AsyncClient,
    path: str,
    params: dict[str, object],
    expected_limit: int,
    expected_cursor: str | None,
) -> None:
    response = await client.get(path, params=params)

    assert response.status_code == 200
    _assert_standard_envelope(
        response.json(),
        limit=expected_limit,
        cursor=expected_cursor,
    )


@pytest.mark.unit
async def test_enveloped_list_preserves_representative_item_shape(
    client: AsyncClient,
) -> None:
    create = await client.post(
        "/v1/workspaces",
        json={
            "repo_url": "git@github.com:example/envelope.git",
            "branch_base": "main",
            "task_title": "Envelope item shape",
            "task_prompt": "Keep event item fields stable.",
            "agent": "codex",
            "test_commands": ["pytest -q"],
        },
    )
    assert create.status_code == 202

    response = await client.get("/v1/events", params={"limit": 3})

    assert response.status_code == 200
    body = response.json()
    _assert_standard_envelope(body, limit=3)
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


@pytest.mark.unit
async def test_workspace_list_keeps_legacy_bare_array_by_default(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/workspaces")

    assert response.status_code == 200
    assert response.json() == []
