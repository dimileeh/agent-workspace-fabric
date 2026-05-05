"""Contract: REST and MCP must surface canonical reason codes uniformly.

For each reason code reachable from a controllable seed, both REST (canonical)
and the MCP tool (where one exists for that capability) must:

1. carry the same ``error_code`` string;
2. validate as ``ErrorResponse`` after ``unwrap_error_envelope``;
3. preserve the ``detail`` payload when one is populated by the underlying
   ``WorkspaceControlError`` -- this is the contract the MCP control tool path
   currently breaks (the ``_tool_error`` helper in ``src/awf/mcp/server.py``
   drops ``detail``), so the smallest-safe surface fix lives in this slice.
"""

from __future__ import annotations

from typing import Any

import pytest

from awf.api.schemas import ErrorResponse
from awf.db.enums import WorkspaceStatus
from awf.mcp import server as mcp_server
from awf.service.controls import (
    WorkspaceRemonitorStateError,
)

from tests.unit.contract.conftest import (
    ContractStack,
    call_mcp_result,
    seed_monitoring_workspace,
    seed_requested_workspace,
    unwrap_error_envelope,
)


@pytest.mark.unit
async def test_remonitor_workspace_not_found_error_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    rest = await contract_stack.rest_client.post(
        "/v1/workspaces/ws_missing/remonitor",
        headers={**contract_stack.auth_headers, "Idempotency-Key": "remon-missing"},
        json={"reason": "missing"},
    )
    mcp = await call_mcp_result(
        contract_stack.mcp,
        "awf_remonitor_workspace",
        {
            "workspace_id": "ws_missing",
            "reason": "missing",
            "idempotency_key": "remon-missing",
        },
    )

    assert rest.status_code == 404
    rest_envelope = unwrap_error_envelope(rest.json())
    assert mcp.isError is True
    assert isinstance(mcp.structuredContent, dict)
    mcp_envelope = unwrap_error_envelope(mcp.structuredContent)
    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_remonitor_workspace_state_error_keeps_detail_on_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    """Pin the smallest-safe-surface fix: MCP must carry ``detail`` like REST.

    REST returns ``{"detail": {"error_code": "WORKSPACE_STATE_NOT_REMONITORABLE",
    "message": ..., "detail": {"status": ..., "eligible_statuses": [...]}}}``.
    MCP must surface the same ``detail`` payload through ``_tool_error``;
    historically it dropped the field, which is the precise bug this contract
    test pins.
    """

    completed_id = await seed_monitoring_workspace(
        contract_stack.factory, final_status=WorkspaceStatus.completed
    )

    rest = await contract_stack.rest_client.post(
        f"/v1/workspaces/{completed_id}/remonitor",
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": "remon-state-rest",
        },
        json={"reason": "state error"},
    )
    mcp = await call_mcp_result(
        contract_stack.mcp,
        "awf_remonitor_workspace",
        {
            "workspace_id": completed_id,
            "reason": "state error",
            "idempotency_key": "remon-state-mcp",
        },
    )

    assert rest.status_code == 409
    rest_envelope = unwrap_error_envelope(rest.json())
    assert rest_envelope["error_code"] == "WORKSPACE_STATE_NOT_REMONITORABLE"
    rest_detail = rest_envelope["detail"]
    assert rest_detail is not None
    assert rest_detail["status"] == WorkspaceStatus.completed.value
    assert WorkspaceStatus.monitoring_pr.value in rest_detail["eligible_statuses"]

    assert mcp.isError is True
    assert isinstance(mcp.structuredContent, dict)
    mcp_envelope = unwrap_error_envelope(mcp.structuredContent)
    assert mcp_envelope["error_code"] == "WORKSPACE_STATE_NOT_REMONITORABLE"
    assert mcp_envelope["detail"] is not None, (
        "MCP must surface the same `detail` payload as REST for control errors. "
        "If this assertion fails, align `_tool_error` in src/awf/mcp/server.py "
        "with `_workspace_error_result` (pass `detail=exc.detail`)."
    )
    assert mcp_envelope["detail"] == rest_detail


@pytest.mark.unit
async def test_remonitor_missing_pr_url_error_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        contract_stack.factory, with_pr_url=False
    )

    rest = await contract_stack.rest_client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": "remon-pr-rest",
        },
        json={"reason": "no pr"},
    )
    mcp = await call_mcp_result(
        contract_stack.mcp,
        "awf_remonitor_workspace",
        {
            "workspace_id": workspace_id,
            "reason": "no pr",
            "idempotency_key": "remon-pr-mcp",
        },
    )

    assert rest.status_code == 400
    rest_envelope = unwrap_error_envelope(rest.json())
    assert rest_envelope["error_code"] == "WORKSPACE_PR_URL_REQUIRED"
    assert rest_envelope["detail"] is not None
    assert rest_envelope["detail"]["status"] == WorkspaceStatus.monitoring_pr.value

    assert mcp.isError is True
    assert isinstance(mcp.structuredContent, dict)
    mcp_envelope = unwrap_error_envelope(mcp.structuredContent)
    assert mcp_envelope["error_code"] == "WORKSPACE_PR_URL_REQUIRED"
    assert mcp_envelope["detail"] == rest_envelope["detail"]


@pytest.mark.unit
async def test_request_validation_state_error_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await seed_requested_workspace(
        contract_stack.factory, title="Validate state error"
    )

    rest = await contract_stack.rest_client.post(
        f"/v1/workspaces/{workspace_id}/validate",
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": "validate-state-rest",
        },
        json={"reason": "state"},
    )
    mcp = await call_mcp_result(
        contract_stack.mcp,
        "awf_request_workspace_validation",
        {
            "workspace_id": workspace_id,
            "reason": "state",
            "idempotency_key": "validate-state-mcp",
        },
    )

    assert rest.status_code == 409
    rest_envelope = unwrap_error_envelope(rest.json())
    assert rest_envelope["error_code"] == "WORKSPACE_STATE_NOT_VALIDATABLE"
    assert rest_envelope["detail"] is not None
    assert rest_envelope["detail"]["status"] == WorkspaceStatus.requested.value

    assert mcp.isError is True
    assert isinstance(mcp.structuredContent, dict)
    mcp_envelope = unwrap_error_envelope(mcp.structuredContent)
    assert mcp_envelope["error_code"] == "WORKSPACE_STATE_NOT_VALIDATABLE"
    assert mcp_envelope["detail"] == rest_envelope["detail"]


@pytest.mark.unit
async def test_adopt_pr_input_required_error_uses_uniform_envelope(
    contract_stack: ContractStack,
) -> None:
    rest = await contract_stack.rest_client.post(
        "/v1/workspaces/adopt-pr",
        headers=contract_stack.auth_headers,
        json={},
    )
    mcp = await call_mcp_result(
        contract_stack.mcp,
        "awf_adopt_pull_request_monitor",
        {},
    )

    assert rest.status_code == 422
    rest_envelope = unwrap_error_envelope(rest.json())
    assert rest_envelope["error_code"] == "PR_ADOPTION_INPUT_REQUIRED"

    assert mcp.isError is True
    assert isinstance(mcp.structuredContent, dict)
    mcp_envelope = unwrap_error_envelope(mcp.structuredContent)
    assert mcp_envelope["error_code"] == "PR_ADOPTION_INPUT_REQUIRED"
    assert ErrorResponse.model_validate(mcp_envelope) == ErrorResponse.model_validate(
        rest_envelope
    )


@pytest.mark.unit
async def test_unauthorized_rest_envelope_validates_as_error_response(
    contract_stack: ContractStack,
) -> None:
    """REST 401 must use the canonical ``UNAUTHORIZED`` envelope.

    The MCP transport-trust contract is in-process (per parity-matrix security
    note), so MCP tools have no equivalent ``UNAUTHORIZED`` code; this is
    documented and pinned by ``test_mcp_tools_do_not_expose_unauthorized_code``.

    Hits a route that is known to enforce ``require_api_token`` at the router
    level (workspace controls). Some other parity-matrix-listed routes do not
    yet enforce the token at the route level; those gaps are tracked
    separately and not part of the structured-error contract.
    """

    response = await contract_stack.rest_client.post(
        "/v1/workspaces/ws_irrelevant/remonitor",
        json={"reason": "missing auth"},
        headers={"Idempotency-Key": "remon-no-auth"},
    )
    assert response.status_code == 401
    envelope = unwrap_error_envelope(response.json())
    assert envelope["error_code"] == "UNAUTHORIZED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.unit
def test_mcp_tools_do_not_expose_unauthorized_code() -> None:
    """MCP tool wrappers never construct an ``UNAUTHORIZED`` error envelope.

    The MCP transport sits inside the trusted process. Any ``UNAUTHORIZED``
    string in MCP server source would imply a token-check that contradicts
    the documented security boundary.
    """

    import inspect

    source = inspect.getsource(mcp_server)
    assert "UNAUTHORIZED" not in source


@pytest.mark.unit
def test_tool_error_helper_carries_detail_payload() -> None:
    """The ``_tool_error`` helper must propagate ``WorkspaceControlError.detail``.

    This is the unit-level guard for the contract pinned by
    ``test_remonitor_workspace_state_error_keeps_detail_on_rest_and_mcp``: any
    future regression that drops ``detail`` from the helper fails this test
    even before the integration assertion runs.
    """

    fake_workspace = type("FakeWorkspace", (), {"status": WorkspaceStatus.completed.value})()
    exc = WorkspaceRemonitorStateError(fake_workspace)  # type: ignore[arg-type]

    result = mcp_server._tool_error(exc)
    assert result.isError is True
    assert isinstance(result.structuredContent, dict)
    payload: dict[str, Any] = result.structuredContent
    assert payload["error_code"] == "WORKSPACE_STATE_NOT_REMONITORABLE"
    assert payload["detail"] == exc.detail, (
        "_tool_error must keep `detail=exc.detail` so MCP and REST surface "
        "the same structured envelope for WorkspaceControlError instances."
    )
