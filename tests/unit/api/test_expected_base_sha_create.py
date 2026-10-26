"""Real HTTP plus PostgreSQL create replay for expected_base_sha."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import awf.api.routes.workspaces as workspaces_route
from awf.common.config import get_settings
from awf.db.models import Workspace

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_SHA = "c" * 40
_OTHER = "d" * 40
_BODY = {
    "repo": {
        "url": "git@github.com:example/expected-base.git",
        "base_branch": "development",
    },
    "task": {
        "title": "Pin development",
        "prompt": "Implement against the inspected development commit.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "expected base sha replay test",
    },
}


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _body(*, pin: str | None, include_pin: bool) -> dict[str, object]:
    body = {
        **_BODY,
        "repo": dict(_BODY["repo"]),
        "task": dict(_BODY["task"]),
    }
    if include_pin:
        body["repo"]["expected_base_sha"] = pin
    return body


def _drop_replay_cache(client: AsyncClient) -> None:
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    cache_key = workspaces_route._WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY  # noqa: SLF001
    if hasattr(transport.app.state, cache_key):
        delattr(transport.app.state, cache_key)


async def _workspace_count(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as session:
        count = await session.scalar(select(func.count()).select_from(Workspace))
    assert isinstance(count, int)
    return count


@pytest.mark.unit
async def test_pinned_create_survives_cache_restart_and_rejects_a_different_pin(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    headers = {"Idempotency-Key": "expected-base-pin"}
    created = await client.post(
        "/v1/workspaces", json=_body(pin=_SHA, include_pin=True), headers=headers
    )
    assert created.status_code == 202, created.text
    workspace_id = created.json()["workspace_id"]
    _drop_replay_cache(client)

    replay = await client.post(
        "/v1/workspaces", json=_body(pin=_SHA, include_pin=True), headers=headers
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["workspace_id"] == workspace_id
    assert await _workspace_count(engine) == 1
    async with AsyncSession(engine) as session:
        row = await session.get(Workspace, workspace_id)
        assert row is not None
        assert row.base_commit is None
        assert row.task_policy["expected_base_sha"] == _SHA

    changed = await client.post(
        "/v1/workspaces", json=_body(pin=_OTHER, include_pin=True), headers=headers
    )
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    omitted = await client.post(
        "/v1/workspaces", json=_body(pin=None, include_pin=False), headers=headers
    )
    assert omitted.status_code == 409
    assert omitted.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert await _workspace_count(engine) == 1


@pytest.mark.unit
async def test_legacy_unpinned_replay_survives_cache_restart(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    headers = {"Idempotency-Key": "expected-base-unpinned"}
    created = await client.post(
        "/v1/workspaces", json=_body(pin=None, include_pin=False), headers=headers
    )
    assert created.status_code == 202, created.text
    workspace_id = created.json()["workspace_id"]
    _drop_replay_cache(client)
    replay = await client.post(
        "/v1/workspaces", json=_body(pin=None, include_pin=False), headers=headers
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["workspace_id"] == workspace_id
    assert await _workspace_count(engine) == 1
    async with AsyncSession(engine) as session:
        row = await session.get(Workspace, workspace_id)
        assert row is not None
        assert "expected_base_sha" not in (row.task_policy or {})
        assert row.base_commit is None
