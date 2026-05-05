"""``If-Match`` / workspace-version concurrency contract.

REST control endpoints accept ``If-Match`` and produce ``VERSION_CONFLICT`` with
structured ``expected_version``/``actual_version`` detail when stale; never
mutate stale state. CLI ``awf workspace remonitor --if-match`` must forward as
``If-Match``.

MCP control tools currently do **not** accept an ``expected_version`` argument
— that is the named ``TODO§P1-if-match-parity`` gap. The harness asserts the
gap as a load-bearing contract claim so the next slice flips both halves of
the assertion together when MCP ships ``expected_version`` arguments.
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
    normalize_rest_error_body,
)
from tests.unit.contracts._stack import ContractStack, contract_stack  # noqa: F401


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
async def test_mcp_control_tools_do_not_accept_expected_version_today(
    contract_stack: ContractStack,
) -> None:
    """Gap-tracked: MCP control tools do NOT expose ``expected_version`` today.

    The parity matrix Status row for "Optimistic concurrency on controls" is
    ``MCP partial`` with backlog ``TODO§P1-if-match-parity``. When that slice
    lands the MCP control tool input schemas will gain ``expected_version`` and
    the assertions below should flip together.
    """
    tools = {tool.name: tool for tool in await contract_stack.mcp.list_tools()}
    gap_slice = "TODO§P1-if-match-parity"
    affected_tools = sorted(
        {capability.mcp_tool for capability in control_capabilities() if capability.mcp_tool}
    )
    assert affected_tools, "Registry must declare at least one MCP-partial control tool"

    for tool_name in affected_tools:
        assert tool_name in tools, tool_name
        properties = tools[tool_name].inputSchema.get("properties", {})
        assert "expected_version" not in properties, (
            f"{tool_name}: input schema unexpectedly already exposes "
            f"'expected_version'. The {gap_slice} slice may have landed — "
            "update tests/unit/contracts/_capabilities.py to flip these "
            "rows from 'MCP partial' to 'MCP implemented'."
        )

    for capability in control_capabilities():
        if capability.is_mcp_partial:
            assert capability.parity_backlog_slice == gap_slice, (
                f"{capability.name}: MCP partial control rows must reference "
                f"the {gap_slice} backlog slice; got "
                f"{capability.parity_backlog_slice!r}"
            )


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
