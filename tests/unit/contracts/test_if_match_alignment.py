"""``If-Match`` / workspace-version concurrency contract.

REST control endpoints accept ``If-Match`` and produce ``VERSION_CONFLICT`` with
structured ``expected_version``/``actual_version`` detail when stale; never
mutate stale state. CLI ``awf workspace remonitor --if-match`` must forward as
``If-Match``.

MCP control tools expose an optional ``expected_version`` argument that maps to
the same backend optimistic-concurrency guard.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

from awf.cli.main import app as cli_app
from awf.db.repositories import WorkspaceRepository
from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    control_capabilities,
    normalize_mcp_error_body,
    normalize_rest_error_body,
)
from tests.unit.contracts._control_scenarios import (
    CONTROL_CAPABILITY_NAMES,
    call_mcp_control,
    call_rest_control,
    install_control_side_effect_stubs,
    seed_workspace_for_control,
)
from tests.unit.contracts._stack import ContractStack

_runner = CliRunner()


async def _seed_workspace(factory: async_sessionmaker[Any]) -> tuple[str, int]:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/version.git",
            branch_base="main",
            task_title="Version contract",
            task_prompt="Exercise version concurrency.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return ws.id, ws.version


@pytest.mark.unit
async def test_rest_stale_if_match_returns_version_conflict_envelope(
    contract_stack: ContractStack,
) -> None:
    workspace_id, version = await _seed_workspace(contract_stack.factory)
    stale = version + 99
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    headers = {
        **contract_stack.auth_headers,
        "Idempotency-Key": "cancel-stale-if-match",
        "If-Match": str(stale),
    }
    response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "stale", "stop_stack": True},
    )
    assert response.status_code == 409
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "VERSION_CONFLICT"
    assert envelope["detail"] == {
        "expected_version": stale,
        "actual_version": version,
    }


@pytest.mark.unit
async def test_rest_malformed_if_match_returns_invalid_request(
    contract_stack: ContractStack,
) -> None:
    workspace_id, _ = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    headers = {
        **contract_stack.auth_headers,
        "Idempotency-Key": "cancel-malformed-if-match",
        "If-Match": "not-a-version",
    }
    response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "stale", "stop_stack": True},
    )
    assert response.status_code == 400
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "INVALID_REQUEST"
    assert "If-Match" in envelope["message"]


@pytest.mark.unit
@pytest.mark.parametrize("variant", ['"7"', 'W/"7"', "7"])
async def test_rest_if_match_accepts_quoted_and_weak_etag_forms(
    contract_stack: ContractStack,
    variant: str,
) -> None:
    workspace_id, _ = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    headers = {
        **contract_stack.auth_headers,
        "Idempotency-Key": f"cancel-if-match-form-{variant}",
        "If-Match": variant,
    }
    response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "form", "stop_stack": True},
    )
    assert response.status_code == 409
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "VERSION_CONFLICT"
    assert envelope["detail"]["expected_version"] == 7


@pytest.mark.unit
def test_cli_remonitor_forwards_if_match_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _capture(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return httpx.Response(
            status_code=200,
            content=json.dumps({"operation_id": "op_x", "status": "running"}).encode(),
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
            "ws_target",
            "--idempotency-key",
            "cli-if-match-key",
            "--if-match",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["headers"]["If-Match"] == "7"


@pytest.mark.unit
async def test_mcp_control_tools_expose_optional_expected_version(
    contract_stack: ContractStack,
) -> None:
    """MCP control tools mirror REST ``If-Match`` through ``expected_version``."""
    tools = {tool.name: tool for tool in await contract_stack.mcp.list_tools()}
    affected_tools = sorted(
        {capability.mcp_tool for capability in control_capabilities() if capability.mcp_tool}
    )
    assert affected_tools, "Registry must declare at least one MCP control tool"

    for tool_name in affected_tools:
        assert tool_name in tools, tool_name
        properties = tools[tool_name].inputSchema.get("properties", {})
        assert "expected_version" in properties, f"{tool_name}: missing expected_version"
        assert "expected_version" not in tools[tool_name].inputSchema.get("required", [])

    for capability in control_capabilities():
        assert capability.is_mcp_implemented
        assert capability.parity_backlog_slice == "—"


@pytest.mark.unit
async def test_rest_stale_if_match_does_not_mutate(
    contract_stack: ContractStack,
) -> None:
    workspace_id, version = await _seed_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    stale = version + 1
    headers = {
        **contract_stack.auth_headers,
        "Idempotency-Key": "cancel-no-mutate",
        "If-Match": str(stale),
    }
    response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "stale", "stop_stack": True},
    )
    assert response.status_code == 409

    async with contract_stack.factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.version == version, "stale If-Match must not mutate workspace"
        assert ws.status != "cancelled"


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", CONTROL_CAPABILITY_NAMES)
async def test_rest_stale_if_match_returns_version_conflict_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every registered versioned control rejects stale REST ``If-Match``."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, version = await seed_workspace_for_control(
        contract_stack.factory,
        capability_name,
    )
    stale = version + 1

    response = await call_rest_control(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=f"{capability_name}-stale-rest",
        expected_version=stale,
    )

    assert response.status_code == 409, response.text
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "VERSION_CONFLICT"
    assert envelope["detail"] == {"expected_version": stale, "actual_version": version}


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", CONTROL_CAPABILITY_NAMES)
async def test_mcp_stale_expected_version_returns_version_conflict_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every registered versioned control rejects stale MCP ``expected_version``."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, version = await seed_workspace_for_control(
        contract_stack.factory,
        capability_name,
    )
    stale = version + 1

    result = await call_mcp_control(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=f"{capability_name}-stale-mcp",
        expected_version=stale,
    )

    assert result.isError is True, result.structuredContent
    envelope = normalize_mcp_error_body(result.structuredContent)
    assert envelope["error_code"] == "VERSION_CONFLICT"
    assert envelope["detail"] == {"expected_version": stale, "actual_version": version}


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", CONTROL_CAPABILITY_NAMES)
async def test_rest_malformed_if_match_returns_invalid_request_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Malformed REST ``If-Match`` is a shared invalid-request envelope for controls."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    workspace_id, _version = await seed_workspace_for_control(
        contract_stack.factory,
        capability_name,
    )
    capability = CAPABILITIES_BY_NAME[capability_name]

    response = await contract_stack.client.request(
        capability.rest_method,
        capability.rest_path.format(workspace_id=workspace_id),
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": f"{capability_name}-malformed-rest",
            "If-Match": "not-a-version",
        },
        json=(
            {"reason": "malformed", "stop_stack": True}
            if capability_name == "cancel_workspace"
            else {"reason": "malformed"}
        )
        if capability.rest_method != "DELETE"
        else None,
        params=(
            {"force": True, "remove_volumes": True, "remove_worktree": False}
            if capability_name == "destroy_workspace"
            else None
        ),
    )

    assert response.status_code == 400, response.text
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "INVALID_REQUEST"
    assert "If-Match" in envelope["message"]
