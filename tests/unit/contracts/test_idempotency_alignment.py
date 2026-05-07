"""Idempotency contract: REST + MCP replay/conflict semantics, CLI header forwarding.

REST cancel/stop/destroy/remonitor/validate/refresh/rebase require the
``Idempotency-Key`` header. MCP variants require the same key as a tool arg
and replay through the same backend coalescing path. CLI commands that
declare ``--idempotency-key`` must forward it as ``Idempotency-Key``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

import awf.api.routes.controls as controls_route
from awf.cli.main import app as cli_app
from awf.db.repositories import WorkspaceRepository
from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    control_capabilities,
    normalize_mcp_error_body,
    normalize_rest_error_body,
)
from tests.unit.contracts._control_scenarios import (
    ALL_IDEMPOTENT_SURFACES,
    call_mcp_idempotent_surface,
    call_rest_idempotent_surface,
    idempotent_conflict_error_code,
    idempotent_response_identity_field,
    idempotent_success_status,
    install_control_side_effect_stubs,
    seed_workspace_for_idempotent_surface,
)
from tests.unit.contracts._stack import ContractStack

_runner = CliRunner()


async def _seed_workspace(factory: async_sessionmaker[Any]) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/idempotency.git",
            branch_base="main",
            task_title="Idempotency contract",
            task_prompt="Exercise idempotency replay/conflict.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return ws.id


async def _call_mcp(mcp: Any, name: str, args: dict[str, object]) -> CallToolResult:
    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    return result


def _stub_stack_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stop(_compose_project_name: str | None) -> None:
        return None

    monkeypatch.setattr(controls_route, "_stop_project", fake_stop)


@pytest.mark.unit
async def test_rest_cancel_replay_returns_same_operation(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_stack_mutations(monkeypatch)
    workspace_id = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    headers = {**contract_stack.auth_headers, "Idempotency-Key": "rest-cancel-replay"}

    first = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "first", "stop_stack": True},
    )
    replay = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "first", "stop_stack": True},
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["operation_id"] == replay.json()["operation_id"]


@pytest.mark.unit
async def test_mcp_cancel_replay_returns_same_operation(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]

    async def _stub_project_stopper(_name: str | None) -> None:
        return None

    contract_stack.service._project_stopper = _stub_project_stopper  # type: ignore[attr-defined]

    first = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "reason": "first",
            "stop_stack": True,
            "idempotency_key": "mcp-cancel-replay",
        },
    )
    replay = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "reason": "first",
            "stop_stack": True,
            "idempotency_key": "mcp-cancel-replay",
        },
    )
    assert first.isError is False
    assert replay.isError is False
    assert first.structuredContent["operation_id"] == replay.structuredContent["operation_id"]


@pytest.mark.unit
async def test_rest_and_mcp_agree_on_idempotency_conflict_for_cancel(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same key, different payload → ``IDEMPOTENCY_CONFLICT`` on both layers.

    The error envelope (error_code/message/detail) must match between layers so
    operators reading either response see the same operator-visible reason.
    """
    _stub_stack_mutations(monkeypatch)

    async def _stub_project_stopper(_name: str | None) -> None:
        return None

    contract_stack.service._project_stopper = _stub_project_stopper  # type: ignore[attr-defined]

    workspace_id = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]

    rest_headers = {**contract_stack.auth_headers, "Idempotency-Key": "rest-cancel-conflict"}
    rest_first = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=rest_headers,
        json={"reason": "v1", "stop_stack": True},
    )
    assert rest_first.status_code == 200
    rest_conflict = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=rest_headers,
        json={"reason": "v2-different", "stop_stack": False},
    )
    assert rest_conflict.status_code == 409
    rest_envelope = normalize_rest_error_body(rest_conflict.json())

    mcp_first = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "reason": "v1",
            "stop_stack": True,
            "idempotency_key": "mcp-cancel-conflict",
        },
    )
    assert mcp_first.isError is False
    mcp_conflict = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "reason": "v2-different",
            "stop_stack": False,
            "idempotency_key": "mcp-cancel-conflict",
        },
    )
    assert mcp_conflict.isError is True
    mcp_envelope = normalize_mcp_error_body(mcp_conflict.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert rest_envelope["message"] == mcp_envelope["message"]
    assert rest_envelope["detail"] == mcp_envelope["detail"]


@pytest.mark.unit
def test_cli_remonitor_forwards_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``awf workspace remonitor --idempotency-key`` must forward as ``Idempotency-Key``."""
    captured: dict[str, Any] = {}

    def _capture(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json")
        return httpx.Response(
            status_code=200,
            content=json.dumps({"operation_id": "op_remon", "status": "running"}).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("awf.cli.main.httpx.request", _capture)
    monkeypatch.setenv("AWF_API_TOKEN", "secret")

    result = _runner.invoke(
        cli_app,
        [
            "workspace",
            "remonitor",
            "ws_under_test",
            "--idempotency-key",
            "cli-remonitor-key",
            "--reason",
            "operator wants recovery",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/workspaces/ws_under_test/remonitor")
    assert captured["headers"]["Idempotency-Key"] == "cli-remonitor-key"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"] == {"reason": "operator wants recovery"}


@pytest.mark.unit
async def test_rest_cancel_requires_idempotency_key_with_invalid_request(
    contract_stack: ContractStack,
) -> None:
    """REST cancel rejects missing key with the shared invalid-request envelope.

    Pinning REST behavior via the registry's ``supports_idempotency_key`` flag.
    """
    workspace_id = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]

    response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=contract_stack.auth_headers,
        json={"reason": "no key", "stop_stack": True},
    )
    assert response.status_code == 400
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "INVALID_REQUEST"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("capability_name", "args"),
    [
        (
            "cancel_workspace",
            {"reason": "mcp no-key", "stop_stack": True},
        ),
        ("stop_workspace", {"reason": "mcp no-key"}),
        (
            "destroy_workspace",
            {"force": True, "remove_volumes": True, "remove_worktree": True},
        ),
        ("remonitor_workspace", {"reason": "mcp no-key"}),
        ("request_validation", {"reason": "mcp no-key", "requested_tier": 1}),
        ("refresh_workspace", {"reason": "mcp no-key"}),
        ("rebase_workspace", {"reason": "mcp no-key"}),
    ],
)
async def test_mcp_control_tools_reject_blank_idempotency_key(
    contract_stack: ContractStack,
    capability_name: str,
    args: dict[str, object],
) -> None:
    async def _stub_project_stopper(_name: str | None) -> None:
        return None

    contract_stack.service._project_stopper = _stub_project_stopper  # type: ignore[attr-defined]

    workspace_id = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME[capability_name]

    result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {"workspace_id": workspace_id, "idempotency_key": " ", **args},
    )
    assert result.isError is True
    envelope = normalize_mcp_error_body(result.structuredContent)
    assert envelope["error_code"] == "INVALID_REQUEST"
    assert envelope["message"] == "Idempotency-Key header is required for this endpoint."


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability",
    control_capabilities(),
    ids=lambda capability: capability.name,
)
async def test_mcp_control_tools_require_idempotency_key_in_schema(
    contract_stack: ContractStack,
    capability: Any,
) -> None:
    """MCP controls mirror REST's required ``Idempotency-Key`` at the tool boundary."""
    assert capability.requires_idempotency_key is True
    tools = {tool.name: tool for tool in await contract_stack.mcp.list_tools()}
    assert "idempotency_key" in tools[capability.mcp_tool or ""].inputSchema.get("required", [])


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", ALL_IDEMPOTENT_SURFACES)
async def test_rest_idempotent_surface_replay_returns_same_identity_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every registered idempotent surface replays exactly at the REST boundary."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, _version = await seed_workspace_for_idempotent_surface(
        contract_stack.factory,
        capability_name,
    )
    key = f"{capability_name}-rest-replay"

    first = await call_rest_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
    )
    replay = await call_rest_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
    )

    assert first.status_code == idempotent_success_status(capability_name), first.text
    assert replay.status_code == idempotent_success_status(capability_name), replay.text
    identity_field = idempotent_response_identity_field(capability_name, client="rest")
    assert replay.json()[identity_field] == first.json()[identity_field]


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", ALL_IDEMPOTENT_SURFACES)
async def test_mcp_idempotent_surface_replay_returns_same_identity_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every registered idempotent surface replays exactly at the MCP boundary."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, _version = await seed_workspace_for_idempotent_surface(
        contract_stack.factory,
        capability_name,
    )
    key = f"{capability_name}-mcp-replay"

    first = await call_mcp_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
    )
    replay = await call_mcp_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
    )

    assert isinstance(first, CallToolResult)
    assert isinstance(replay, CallToolResult)
    assert first.isError is False, first.structuredContent
    assert replay.isError is False, replay.structuredContent
    identity_field = idempotent_response_identity_field(capability_name, client="mcp")
    assert replay.structuredContent[identity_field] == first.structuredContent[identity_field]


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", ALL_IDEMPOTENT_SURFACES)
async def test_rest_idempotent_surface_conflict_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Same idempotency identity plus changed user payload conflicts on every REST surface."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, _version = await seed_workspace_for_idempotent_surface(
        contract_stack.factory,
        capability_name,
    )
    key = f"{capability_name}-rest-conflict"

    first = await call_rest_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
    )
    conflict = await call_rest_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
        variant="changed",
    )

    assert first.status_code == idempotent_success_status(capability_name), first.text
    assert conflict.status_code == 409, conflict.text
    envelope = normalize_rest_error_body(conflict.json())
    assert envelope["error_code"] == idempotent_conflict_error_code(capability_name)


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", ALL_IDEMPOTENT_SURFACES)
async def test_mcp_idempotent_surface_conflict_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Same idempotency identity plus changed user payload conflicts on every MCP surface."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, _version = await seed_workspace_for_idempotent_surface(
        contract_stack.factory,
        capability_name,
    )
    key = f"{capability_name}-mcp-conflict"

    first = await call_mcp_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
    )
    conflict = await call_mcp_idempotent_surface(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=key,
        variant="changed",
    )

    assert isinstance(first, CallToolResult)
    assert isinstance(conflict, CallToolResult)
    assert first.isError is False, first.structuredContent
    assert conflict.isError is True, conflict.structuredContent
    envelope = normalize_mcp_error_body(conflict.structuredContent)
    assert envelope["error_code"] == idempotent_conflict_error_code(capability_name)
