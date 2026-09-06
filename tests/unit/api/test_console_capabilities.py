"""Console capabilities API tests (schema_version=1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from awf.common.config import get_settings

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "console" / "fixtures" / "v1"


@pytest.mark.unit
async def test_console_capabilities_requires_auth(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/console/capabilities",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["detail"]["error_code"] == "UNAUTHORIZED"
    assert "widgets" not in body
    assert "wrong-token" not in response.text


@pytest.mark.unit
async def test_console_capabilities_local_schema_v1(client: AsyncClient) -> None:
    response = await client.get("/v1/console/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["backend_kind"] == "local"
    assert body["identity"]["scope"] == "local"

    widgets = {item["id"]: item for item in body["widgets"]}
    assert widgets["fleet_summary"]["availability"] == "available"
    assert widgets["fleet_summary"]["route"] == "/v1/console/dashboard-summary"
    assert widgets["resource_capacity"]["availability"] == "available"
    assert widgets["resource_capacity"]["route"].startswith("/v1/")
    assert widgets["cloud_runtime"]["availability"] == "unsupported"
    assert widgets["cloud_runtime"]["reason_code"] == "backend_kind_local"
    assert widgets["cost"]["availability"] == "unsupported"
    assert widgets["cloud_runtime"].get("route") is None

    for group in ("widgets", "diagnostics", "controls"):
        for item in body[group]:
            route = item.get("route")
            if route is not None:
                assert route.startswith("/v1/")
                assert "://" not in route


@pytest.mark.unit
async def test_console_capabilities_matches_local_fixture_shape(client: AsyncClient) -> None:
    fixture = json.loads((FIXTURES / "capabilities.local.json").read_text(encoding="utf-8"))
    response = await client.get("/v1/console/capabilities")
    body = response.json()
    assert {w["id"] for w in body["widgets"]} == {w["id"] for w in fixture["widgets"]}
    assert {d["id"] for d in body["diagnostics"]} == {d["id"] for d in fixture["diagnostics"]}
    assert {c["id"] for c in body["controls"]} == {c["id"] for c in fixture["controls"]}
    live = {w["id"]: w["availability"] for w in body["widgets"]}
    expected = {w["id"]: w["availability"] for w in fixture["widgets"]}
    assert live == expected


@pytest.mark.unit
async def test_console_capabilities_unconfigured_token_is_distinguishable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        response = await client.get("/v1/console/capabilities")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "API_TOKEN_NOT_CONFIGURED"
