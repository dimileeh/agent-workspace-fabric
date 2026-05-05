"""Smoke test that proves the shared contract_stack fixture wires all surfaces."""

from __future__ import annotations

import pytest

from tests.unit.contract.conftest import (
    ContractStack,
    call_mcp_structured,
    invoke_cli,
)


@pytest.mark.unit
async def test_contract_stack_smoke(contract_stack: ContractStack) -> None:
    rest_health = (await contract_stack.rest_client.get("/healthz")).json()
    mcp_health = await call_mcp_structured(contract_stack.mcp, "awf_get_service_health", {})

    assert rest_health["status"] == mcp_health["status"]
    assert rest_health["service"] == mcp_health["service"]

    cli_result, request_mock = invoke_cli(
        contract_stack.cli_runner,
        ["workspace", "list"],
        response_status=200,
        response_payload=[],
    )
    assert cli_result.exit_code == 0
    assert request_mock.called
    method, url = request_mock.call_args.args[0], request_mock.call_args.args[1]
    assert method == "GET"
    assert url.endswith("/v1/workspaces")
