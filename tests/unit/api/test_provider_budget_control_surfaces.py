"""REST, CLI and MCP expose the managed provider-budget control 1:1."""

from __future__ import annotations

import httpx
import pytest
from mcp.types import CallToolResult

from awf.common.config import Settings
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from tests.shared.openrouter_management import FakeOpenRouterManagement
from tests.unit.service.test_controls_provider_budget import _workspace

pytestmark = pytest.mark.unit


@pytest.fixture
def fake(monkeypatch) -> FakeOpenRouterManagement:
    fake = FakeOpenRouterManagement()
    monkeypatch.setattr(
        "awf.api.routes.controls.management_client_from_settings", lambda _settings: fake.client()
    )
    return fake


async def test_rest_sets_the_budget_and_resumes(client, engine, fake, tmp_path):
    workspace_id = await _workspace(make_session_factory(engine), fake, tmp_path)

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/provider-budget",
        json={"usd": 4, "reason": "approved"},
        headers={"Idempotency-Key": "rest-budget"},
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["workspace_id"] == workspace_id
    assert body["status"] == "blocked"
    assert body["operation_status"] == "succeeded"
    assert ("PATCH", "/keys/hash1", {"limit": 4.0}) in fake.calls


@pytest.mark.parametrize(
    ("body", "headers", "status", "code"),
    [
        ({"usd": 4}, {}, 400, "Idempotency-Key header is required"),
        ({"usd": 0}, {"Idempotency-Key": "k"}, 422, None),
        ({"usd": "4"}, {"Idempotency-Key": "k"}, 422, None),
        ({"usd": 0.25}, {"Idempotency-Key": "k"}, 422, "PROVIDER_BUDGET_INVALID"),
    ],
)
async def test_rest_rejects_invalid_requests(
    client, engine, fake, tmp_path, body, headers, status, code
):
    workspace_id = await _workspace(make_session_factory(engine), fake, tmp_path)

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/provider-budget", json=body, headers=headers
    )

    assert response.status_code == status, response.json()
    if code is not None:
        assert code in str(response.json())


async def test_rest_maps_unsupported_missing_and_management_failures(
    client, engine, fake, tmp_path
):
    factory = make_session_factory(engine)
    static = await _workspace(factory, fake, tmp_path, managed=False)
    missing = await client.post(
        "/v1/workspaces/ws_missing/provider-budget",
        json={"usd": 4},
        headers={"Idempotency-Key": "missing"},
    )
    unsupported = await client.post(
        f"/v1/workspaces/{static}/provider-budget",
        json={"usd": 4},
        headers={"Idempotency-Key": "static"},
    )
    managed = await _workspace(factory, fake, tmp_path)
    fake.fail[("PATCH", "key")] = 500
    failed = await client.post(
        f"/v1/workspaces/{managed}/provider-budget",
        json={"usd": 4},
        headers={"Idempotency-Key": "patch-fails"},
    )

    assert missing.status_code == 404
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["error_code"] == "PROVIDER_BUDGET_UNSUPPORTED"
    assert failed.status_code == 502
    assert failed.json()["detail"]["error_code"] == "PROVIDER_MANAGEMENT_API_UNAVAILABLE"


def test_cli_posts_the_budget_with_an_idempotency_key(monkeypatch):
    from typer.testing import CliRunner

    from awf.cli import workspace_commands

    captured: dict = {}

    def call(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return httpx.Response(200, json={})

    monkeypatch.setattr(workspace_commands, "_call", call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_a, **_k: None)

    result = CliRunner().invoke(
        workspace_commands.workspace_app,
        ["provider-budget", "ws_x", "--usd", "2.5", "--reason", "approved", "--if-match", "7"],
    )

    assert result.exit_code == 0, result.output
    assert (captured["method"], captured["path"]) == (
        "POST",
        "/v1/workspaces/ws_x/provider-budget",
    )
    assert captured["json"] == {"usd": 2.5, "reason": "approved"}
    assert captured["headers"]["Idempotency-Key"].startswith("awf-cli-provider-budget-")
    assert captured["headers"]["If-Match"] == "7"
    captured.clear()
    result = CliRunner().invoke(
        workspace_commands.workspace_app, ["provider-budget", "ws_x", "--usd", "3"]
    )
    assert result.exit_code == 0, result.output
    assert captured["json"] == {"usd": 3.0}


async def test_mcp_tool_mirrors_the_rest_control(engine, tmp_path, monkeypatch):
    fake = FakeOpenRouterManagement()
    monkeypatch.setattr(
        "awf.service.workspaces_provider_budget.management_client_from_settings",
        lambda _settings: fake.client(),
    )
    factory = make_session_factory(engine)
    workspace_id = await _workspace(factory, fake, tmp_path)
    mcp = build_mcp_server(
        service=WorkspaceService(factory, settings=Settings(_env_file=None)),
        settings=Settings(_env_file=None),
    )

    result = await mcp.call_tool(
        "awf_set_workspace_provider_budget",
        {"workspace_id": workspace_id, "usd": 6, "idempotency_key": "mcp-budget"},
    )
    missing_key = await mcp.call_tool(
        "awf_set_workspace_provider_budget",
        {"workspace_id": workspace_id, "usd": 6, "idempotency_key": " "},
    )
    invalid = await mcp.call_tool(
        "awf_set_workspace_provider_budget",
        {"workspace_id": workspace_id, "usd": 0.1, "idempotency_key": "mcp-invalid"},
    )

    payload = result.structuredContent if isinstance(result, CallToolResult) else result[1]
    assert payload["workspace_id"] == workspace_id
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws.task_policy["provider_budget"]["limit_usd"] == 6.0
    for error in (missing_key, invalid):
        error_payload = error.structuredContent if isinstance(error, CallToolResult) else error[1]
        assert "error_code" in error_payload
