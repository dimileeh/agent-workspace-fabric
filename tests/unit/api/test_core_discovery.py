"""Core discovery endpoint contract tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from awf import __version__
from awf.api.app import create_app
from awf.common.config import get_settings

DISCOVERY_PATH = "/.well-known/awf-core.json"


def _client_without_database() -> AsyncClient:
    app = create_app(use_lifespan=False)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_core_discovery_is_public_with_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "secret-token-that-must-not-leak"
    monkeypatch.setenv("AWF_API_TOKEN", token)
    monkeypatch.setenv("AWF_GIT_COMMIT", "abc1234")
    get_settings.cache_clear()

    try:
        async with _client_without_database() as client:
            response = await client.get(DISCOVERY_PATH)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "package_name": "agent-workspace-fabric",
        "package_version": __version__,
        "git_commit": "abc1234",
        "capabilities": ["workspace.execution.v1"],
    }
    assert token not in response.text


async def test_core_discovery_and_health_do_not_require_database() -> None:
    async with _client_without_database() as client:
        discovery = await client.get(DISCOVERY_PATH)
        health = await client.get("/healthz")

    assert discovery.status_code == 200
    assert health.status_code == 200


async def test_core_discovery_uses_safe_unknown_git_commit_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWF_GIT_COMMIT", raising=False)
    monkeypatch.setattr(
        "awf.service.core_discovery._git_rev_parse_head",
        lambda: None,
    )

    async with _client_without_database() as client:
        response = await client.get(DISCOVERY_PATH)

    assert response.status_code == 200
    assert response.json()["git_commit"] == "unknown"


async def test_core_discovery_does_not_weaken_protected_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_API_TOKEN", "protected-token")
    get_settings.cache_clear()

    try:
        async with _client_without_database() as client:
            discovery = await client.get(DISCOVERY_PATH)
            missing_auth = await client.get("/v1/operations")
    finally:
        get_settings.cache_clear()

    assert discovery.status_code == 200
    assert missing_auth.status_code == 401
    assert missing_auth.headers["WWW-Authenticate"] == "Bearer"
    assert missing_auth.json()["detail"]["error_code"] == "UNAUTHORIZED"
