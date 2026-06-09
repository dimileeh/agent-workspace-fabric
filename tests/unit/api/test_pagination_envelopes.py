"""Cross-endpoint pagination envelope contract tests."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from awf.common.config import get_settings

_ENVELOPE_KEYS = {"items", "next_cursor", "has_more", "limit", "cursor"}
_API_TOKEN = "secret"
_AUTH_HEADERS = {"Authorization": f"Bearer {_API_TOKEN}"}


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWF_API_TOKEN", _API_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _overview_cursor() -> str:
    """Generate a stable cursor for the workspace overview test."""
    payload = {
        "created_at": "2026-04-29T00:00:00+00:00",
        "workspace_id": "ws_envelope_cursor",
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


_OVERVIEW_CURSOR = _overview_cursor()


def _assert_standard_envelope(
    body: dict[str, object],
    *,
    limit: int,
    cursor: str | None = None,
) -> None:
    """Assert that a response payload conforms to the standard pagination envelope."""
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
        (
            "/v1/workspaces/overview",
            {"limit": 7, "cursor": _OVERVIEW_CURSOR},
            7,
            _OVERVIEW_CURSOR,
        ),
    ],
)
async def test_enveloped_list_endpoints_expose_standard_pagination_metadata(
    client: AsyncClient,
    path: str,
    params: dict[str, object],
    expected_limit: int,
    expected_cursor: str | None,
) -> None:
    """Test that listing endpoints return a standard pagination metadata envelope."""
    response = await client.get(path, params=params, headers=_AUTH_HEADERS)

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
    """Test that list operations preserve the required shape of items inside the envelope."""
    create = await client.post(
        "/v1/workspaces",
        json={
            "repo_url": "git@github.com:example/envelope.git",
            "branch_base": "main",
            "task_title": "Envelope item shape",
            "task_prompt": "Keep event item fields stable.",
            "agent": "codex",
            "test_commands": ["pytest -q"],
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "pagination envelope fixture",
            },
        },
        headers=_AUTH_HEADERS,
    )
    assert create.status_code == 202

    response = await client.get("/v1/events", params={"limit": 3}, headers=_AUTH_HEADERS)

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
    """Test that the legacy workspace list endpoint returns a bare array by default."""
    response = await client.get("/v1/workspaces", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == []
