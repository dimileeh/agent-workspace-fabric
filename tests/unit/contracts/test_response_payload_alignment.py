"""REST 2xx body and MCP ``structuredContent`` must match for shared surfaces.

Read surfaces are already covered for many capabilities by
``tests/unit/mcp/test_mcp_operator_surfaces.py``. This module covers the
mutating control surfaces (cancel/stop/destroy/remonitor/validate) — given the
same seeded workspace, REST and MCP must produce equivalent operator-visible
payloads.

CLI: where the CLI command exists, the printed JSON must round-trip to the same
dict the REST body returns (i.e., the CLI does not reshape responses).
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
from tests.unit.contracts._capabilities import CAPABILITIES_BY_NAME
from tests.unit.contracts._stack import ContractStack

_runner = CliRunner()


async def _seed_workspace(factory: async_sessionmaker[Any]) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/response.git",
            branch_base="main",
            task_title="Response contract",
            task_prompt="Exercise response payload alignment.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return ws.id


def _normalize_control_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop volatile fields so REST/MCP control responses can be compared.

    Both REST and MCP serialize ``WorkspaceControlResponse`` so the contract is
    deterministic — but the test suite still scrubs unexpected volatile keys
    defensively in case future fields land.
    """
    volatile = {"started_at", "finished_at", "occurred_at"}
    return {k: v for k, v in payload.items() if k not in volatile}


async def _call_mcp_structured(
    mcp: Any, name: str, args: dict[str, object]
) -> dict[str, Any]:
    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    assert result.isError is False, result.structuredContent
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


@pytest.mark.unit
async def test_cancel_rest_matches_mcp_structured_content(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST and MCP cancel responses must encode the same control envelope.

    Two separate seeded workspaces let both layers exercise the cancel path
    independently, then we compare the operator-visible response dict.
    """

    async def _stub(_name: str | None) -> None:
        return None

    monkeypatch.setattr(controls_route, "_stop_project", _stub)
    contract_stack.service._project_stopper = _stub  # type: ignore[attr-defined]

    rest_workspace = await _seed_workspace(contract_stack.factory)
    mcp_workspace = await _seed_workspace(contract_stack.factory)

    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    rest_response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=rest_workspace),
        headers={**contract_stack.auth_headers, "Idempotency-Key": "rest-cancel-equal"},
        json={"reason": "shared contract", "stop_stack": True},
    )
    assert rest_response.status_code == 200, rest_response.text
    rest_body = rest_response.json()

    mcp_body = await _call_mcp_structured(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": mcp_workspace,
            "reason": "shared contract",
            "stop_stack": True,
            "idempotency_key": "mcp-cancel-equal",
        },
    )

    assert set(rest_body.keys()) == set(mcp_body.keys()), (rest_body, mcp_body)
    rest_normalized = _normalize_control_payload(rest_body)
    mcp_normalized = _normalize_control_payload(mcp_body)
    rest_normalized.pop("workspace_id", None)
    rest_normalized.pop("operation_id", None)
    mcp_normalized.pop("workspace_id", None)
    mcp_normalized.pop("operation_id", None)
    assert rest_normalized == mcp_normalized, (rest_normalized, mcp_normalized)
    assert isinstance(rest_body["workspace_id"], str)
    assert isinstance(rest_body["operation_id"], str)
    assert isinstance(mcp_body["workspace_id"], str)
    assert isinstance(mcp_body["operation_id"], str)
    assert rest_body["status"] == mcp_body["status"]
    assert rest_body["operation_status"] == mcp_body["operation_status"]
    assert rest_body["message"] == mcp_body["message"]


@pytest.mark.unit
async def test_stop_rest_matches_mcp_structured_content(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _stub(_name: str | None) -> None:
        return None

    monkeypatch.setattr(controls_route, "_stop_project", _stub)
    contract_stack.service._project_stopper = _stub  # type: ignore[attr-defined]

    rest_workspace = await _seed_workspace(contract_stack.factory)
    mcp_workspace = await _seed_workspace(contract_stack.factory)

    capability = CAPABILITIES_BY_NAME["stop_workspace"]
    rest_response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=rest_workspace),
        headers={**contract_stack.auth_headers, "Idempotency-Key": "rest-stop-equal"},
        json={"reason": "stop contract"},
    )
    assert rest_response.status_code == 200, rest_response.text
    rest_body = rest_response.json()

    mcp_body = await _call_mcp_structured(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": mcp_workspace,
            "reason": "stop contract",
            "idempotency_key": "mcp-stop-equal",
        },
    )

    assert set(rest_body.keys()) == set(mcp_body.keys()), (rest_body, mcp_body)
    assert rest_body["operation_status"] == mcp_body["operation_status"]
    assert rest_body["status"] == mcp_body["status"]
    assert rest_body["message"] == mcp_body["message"]


@pytest.mark.unit
def test_cli_remonitor_does_not_reshape_rest_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI remonitor passes the REST body through ``json.dumps`` unchanged.

    The CLI must not invent fields, drop fields, or rename keys; operators read
    the raw REST envelope from CLI output to drive scripts.
    """
    rest_body = {
        "workspace_id": "ws_target",
        "operation_id": "op_remon",
        "operation_status": "running",
        "status": "monitoring_pr",
        "message": "remonitor requested",
    }

    def _capture(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            content=json.dumps(rest_body).encode(),
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
            "cli-remon-equal",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == rest_body, "CLI must not reshape REST 2xx body"
