"""REST and MCP must use one structured error envelope across registered surfaces.

The error envelope is ``{"error_code": str, "message": str, "detail": dict|None}``
on both layers (after stripping the FastAPI ``HTTPException`` ``detail`` wrapper).
This test asserts that every error path in the registry conforms to that shape so
operators can write one error handler instead of one per route.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import async_sessionmaker

from awf.db.repositories import WorkspaceRepository
from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    assert_envelope_shape,
    normalize_mcp_error_body,
    normalize_rest_error_body,
)
from tests.unit.contracts._stack import ContractStack


async def _seed_workspace(factory: async_sessionmaker[Any]) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/envelope.git",
            branch_base="main",
            task_title="Envelope contract",
            task_prompt="Exercise envelope shape.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return ws.id


async def _call_mcp(mcp: Any, name: str, args: dict[str, object]) -> CallToolResult:
    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    return result


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    ["cancel_workspace", "stop_workspace", "remonitor_workspace", "request_validation"],
)
async def test_rest_error_envelope_shape_for_not_found(
    contract_stack: ContractStack,
    capability_name: str,
) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    headers = {**contract_stack.auth_headers, "Idempotency-Key": f"{capability_name}-shape"}
    body: dict[str, object]
    if capability_name == "request_validation":
        body = {"reason": "shape", "requested_tier": 1}
    elif capability_name == "cancel_workspace":
        body = {"reason": "shape", "stop_stack": True}
    else:
        body = {"reason": "shape"}

    response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id="ws_not_present"),
        headers=headers,
        json=body,
    )
    envelope = normalize_rest_error_body(response.json())
    assert_envelope_shape(envelope)


@pytest.mark.unit
async def test_rest_destroy_error_envelope_shape_for_not_found(
    contract_stack: ContractStack,
) -> None:
    capability = CAPABILITIES_BY_NAME["destroy_workspace"]
    headers = {**contract_stack.auth_headers, "Idempotency-Key": "destroy-shape"}
    response = await contract_stack.client.delete(
        capability.rest_path.format(workspace_id="ws_not_present"),
        headers=headers,
    )
    envelope = normalize_rest_error_body(response.json())
    assert_envelope_shape(envelope)


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    ["cancel_workspace", "stop_workspace", "remonitor_workspace", "request_validation"],
)
async def test_mcp_error_envelope_shape_for_not_found(
    contract_stack: ContractStack,
    capability_name: str,
) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    args: dict[str, object] = {
        "workspace_id": "ws_not_present",
        "idempotency_key": f"{capability_name}-mcp-shape",
    }
    if capability_name == "cancel_workspace":
        args.update({"reason": "shape", "stop_stack": True})
    else:
        args["reason"] = "shape"
    result = await _call_mcp(contract_stack.mcp, capability.mcp_tool or "", args)
    assert result.isError is True
    envelope = normalize_mcp_error_body(result.structuredContent)
    assert_envelope_shape(envelope)


@pytest.mark.unit
async def test_mcp_destroy_error_envelope_shape_for_not_found(
    contract_stack: ContractStack,
) -> None:
    capability = CAPABILITIES_BY_NAME["destroy_workspace"]
    result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": "ws_not_present",
            "force": False,
            "remove_volumes": True,
            "remove_worktree": True,
            "idempotency_key": "destroy-mcp-shape",
        },
    )
    assert result.isError is True
    envelope = normalize_mcp_error_body(result.structuredContent)
    assert_envelope_shape(envelope)


@pytest.mark.unit
async def test_rest_invalid_request_envelope_for_missing_idempotency_key(
    contract_stack: ContractStack,
) -> None:
    """REST cancel/stop/remonitor/destroy/validate require the Idempotency-Key header.

    Missing key → ``INVALID_REQUEST`` envelope on REST. The test pins the shape
    so unrelated route changes can't silently weaken the contract.
    """
    workspace_id = await _seed_workspace(contract_stack.factory)
    response = await contract_stack.client.post(
        f"/v1/workspaces/{workspace_id}/cancel",
        headers=contract_stack.auth_headers,
        json={"reason": "no key", "stop_stack": True},
    )
    assert response.status_code == 400
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "INVALID_REQUEST"
    assert "Idempotency-Key" in envelope["message"]


@pytest.mark.unit
async def test_rest_idempotency_conflict_envelope_for_create_v1(
    contract_stack: ContractStack,
) -> None:
    """``POST /v1/workspaces`` returns the JSONResponse ErrorResponse top-level shape.

    Asserts the harness's normalizer accepts both REST envelope shapes.
    """
    body = {
        "repo_url": "git@github.com:example/idempotency.git",
        "branch_base": "main",
        "task_title": "Replay",
        "task_prompt": "Exercise replay error envelope.",
        "agent": "codex",
        "test_commands": ["pytest -q"],
        "preflight": {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "structured error fixture",
        },
    }
    headers = {**contract_stack.auth_headers, "Idempotency-Key": "create-v1-key"}
    accepted = await contract_stack.client.post("/v1/workspaces", json=body, headers=headers)
    assert accepted.status_code == 202

    different_body = {**body, "task_title": "Different title"}
    conflict = await contract_stack.client.post(
        "/v1/workspaces",
        json=different_body,
        headers=headers,
    )
    assert conflict.status_code == 409
    envelope = normalize_rest_error_body(conflict.json())
    assert envelope["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert isinstance(envelope["message"], str)
    assert envelope["detail"] is None or isinstance(envelope["detail"], dict)
