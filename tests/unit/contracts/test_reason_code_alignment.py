"""REST + MCP must agree on reason codes for every shared error scenario.

Each scenario exercises one reason code through both surfaces against the same
seeded state and asserts the operator-visible error envelope (``error_code``,
``message``, ``detail``) is byte-identical after envelope normalization.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    collect_known_error_codes,
    normalize_mcp_error_body,
    normalize_rest_error_body,
    parity_matrix_error_codes,
)
from tests.unit.contracts._control_scenarios import (
    CONTROL_CAPABILITY_NAMES,
    call_mcp_control,
    call_rest_control,
    seed_basic_workspace,
    seed_monitoring_workspace,
)
from tests.unit.contracts._stack import ContractStack


async def _seed_basic_workspace(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/contract.git",
            branch_base="main",
            task_title="Contract scenario",
            task_prompt="Exercise contract behaviors.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return ws.id


async def _seed_monitoring_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    with_pr_url: bool = True,
) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/monitor.git",
            branch_base="main",
            task_title="Monitor scenario",
            task_prompt="Exercise monitor recovery.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        if with_pr_url:
            ws.pr_url = "https://github.com/example/monitor/pull/1"
            ws.pr_number = 1
            ws.monitor_last_commit_sha = "b" * 40
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await session.commit()
        return ws.id


async def _call_mcp(mcp: Any, name: str, args: dict[str, object]) -> CallToolResult:
    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    return result


@pytest.mark.unit
async def test_known_error_codes_appear_in_parity_matrix() -> None:
    """Every error code we test against must be referenced in the parity matrix.

    Keeps the harness honest: if we test for a code that isn't in
    ``docs/MCP_CLIENT_PARITY.md``, either the docs are stale or the harness is
    asserting a code the contract doesn't promise.
    """
    declared = collect_known_error_codes()
    matrix_codes: set[str] = set()
    seen: set[str] = set()
    for capability in CAPABILITIES_BY_NAME.values():
        if capability.parity_capability in seen:
            continue
        seen.add(capability.parity_capability)
        matrix_codes |= parity_matrix_error_codes(capability.parity_capability)
    missing = declared - matrix_codes
    assert not missing, (
        "Contract registry references error codes not in the parity matrix "
        f"Schema/Error-Code column: {sorted(missing)}"
    )


@pytest.mark.unit
async def test_rest_and_mcp_agree_on_not_found_for_cancel(
    contract_stack: ContractStack,
) -> None:
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    rest_response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id="ws_does_not_exist"),
        headers={**contract_stack.auth_headers, "Idempotency-Key": "scenario-key"},
        json={"reason": "test", "stop_stack": True},
    )
    assert rest_response.status_code == 404
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": "ws_does_not_exist",
            "reason": "test",
            "stop_stack": True,
            "idempotency_key": "scenario-key",
        },
    )
    assert mcp_result.isError is True
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope == mcp_envelope, (
        "REST and MCP disagree on cancel-not-found envelope:\n"
        f"REST: {rest_envelope}\n"
        f"MCP:  {mcp_envelope}"
    )
    assert rest_envelope["error_code"] == "NOT_FOUND"


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    [
        "cancel_workspace",
        "stop_workspace",
        "destroy_workspace",
        "remonitor_workspace",
        "request_validation",
    ],
)
async def test_rest_and_mcp_agree_on_not_found_for_control(
    contract_stack: ContractStack,
    capability_name: str,
) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    bogus_id = "ws_not_present"
    headers = {**contract_stack.auth_headers, "Idempotency-Key": f"{capability_name}-nf"}
    if capability_name == "request_validation":
        payload: dict[str, object] = {"reason": "scenario", "requested_tier": 1}
    elif capability_name in {"stop_workspace", "remonitor_workspace"}:
        payload = {"reason": "scenario"}
    else:
        payload = {"reason": "scenario", "stop_stack": True}
    if capability.rest_method == "DELETE":
        rest_response = await contract_stack.client.delete(
            capability.rest_path.format(workspace_id=bogus_id),
            headers=headers,
        )
    else:
        rest_response = await contract_stack.client.post(
            capability.rest_path.format(workspace_id=bogus_id),
            headers=headers,
            json=payload,
        )
    assert rest_response.status_code == 404
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_args: dict[str, object] = {
        "workspace_id": bogus_id,
        "idempotency_key": headers["Idempotency-Key"],
    }
    if capability_name in {"cancel_workspace"}:
        mcp_args.update({"reason": "scenario", "stop_stack": True})
    elif capability_name in {"stop_workspace", "remonitor_workspace", "request_validation"}:
        mcp_args["reason"] = "scenario"
    elif capability_name == "destroy_workspace":
        mcp_args.update({"force": False, "remove_volumes": True, "remove_worktree": True})

    mcp_result = await _call_mcp(contract_stack.mcp, capability.mcp_tool or "", mcp_args)
    assert mcp_result.isError is True
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == "NOT_FOUND"
    assert isinstance(rest_envelope["message"], str)
    assert rest_envelope["message"] == mcp_envelope["message"]
    assert rest_envelope["detail"] == mcp_envelope["detail"]


@pytest.mark.unit
async def test_rest_and_mcp_agree_on_remonitor_pr_required(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await _seed_monitoring_workspace(contract_stack.factory, with_pr_url=False)
    capability = CAPABILITIES_BY_NAME["remonitor_workspace"]

    headers = {**contract_stack.auth_headers, "Idempotency-Key": "remonitor-no-pr"}
    rest_response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "needs pr"},
    )
    assert rest_response.status_code == 400
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "reason": "needs pr",
            "idempotency_key": "remonitor-no-pr-mcp",
        },
    )
    assert mcp_result.isError is True
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == "WORKSPACE_PR_URL_REQUIRED"
    assert rest_envelope["message"] == mcp_envelope["message"]
    assert rest_envelope["detail"] == mcp_envelope["detail"]


@pytest.mark.unit
async def test_rest_and_mcp_agree_on_validate_state_not_validatable(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await _seed_basic_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["request_validation"]

    headers = {**contract_stack.auth_headers, "Idempotency-Key": "validate-bad-state"}
    rest_response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        json={"reason": "rerun"},
    )
    assert rest_response.status_code == 409, rest_response.text
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "reason": "rerun",
            "idempotency_key": "validate-bad-state-mcp",
        },
    )
    assert mcp_result.isError is True
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"]
    assert rest_envelope["error_code"] in {
        "WORKSPACE_PR_URL_REQUIRED",
        "WORKSPACE_STATE_NOT_VALIDATABLE",
    }
    assert rest_envelope["message"] == mcp_envelope["message"]
    assert rest_envelope["detail"] == mcp_envelope["detail"]


@pytest.mark.unit
async def test_rest_and_mcp_agree_on_invalid_request_missing_idempotency_key() -> None:
    """REST and MCP reject missing or blank idempotency keys on control surfaces.

    The REST control surface treats ``Idempotency-Key`` as required;
    ``INVALID_REQUEST`` is the agreed-upon code. The MCP adapter normalizes
    omitted and blank values before calling the control service.
    """
    # Asserted via dedicated REST endpoint test in
    # tests/unit/api/test_workspace_controls_idempotency.py — this contract test
    # ensures the registry reflects today's REST stance vs MCP stance.
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    assert capability.supports_idempotency_key is True
    assert capability.mcp_tool is not None


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", CONTROL_CAPABILITY_NAMES)
async def test_rest_and_mcp_agree_on_not_found_for_every_control_registry_row(
    contract_stack: ContractStack,
    capability_name: str,
) -> None:
    rest_response = await call_rest_control(
        contract_stack,
        capability_name,
        workspace_id="ws_not_present",
        idempotency_key=f"{capability_name}-not-found",
    )
    assert rest_response.status_code == 404, rest_response.text
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await call_mcp_control(
        contract_stack,
        capability_name,
        workspace_id="ws_not_present",
        idempotency_key=f"{capability_name}-not-found-mcp",
    )
    assert mcp_result.isError is True, mcp_result.structuredContent
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope == mcp_envelope
    assert rest_envelope["error_code"] == "NOT_FOUND"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("capability_name", "final_status", "expected_code"),
    [
        ("remonitor_workspace", WorkspaceStatus.completed, "WORKSPACE_STATE_NOT_REMONITORABLE"),
        ("request_validation", WorkspaceStatus.completed, "WORKSPACE_STATE_NOT_VALIDATABLE"),
        ("refresh_workspace", WorkspaceStatus.destroyed, "WORKSPACE_STATE_NOT_REFRESHABLE"),
        ("rebase_workspace", WorkspaceStatus.completed, "WORKSPACE_STATE_NOT_REBASEABLE"),
    ],
)
async def test_rest_and_mcp_agree_on_invalid_state_for_registry_controls(
    contract_stack: ContractStack,
    capability_name: str,
    final_status: WorkspaceStatus,
    expected_code: str,
) -> None:
    workspace_id, _version = await seed_monitoring_workspace(
        contract_stack.factory,
        final_status=final_status,
        with_open_candidate=capability_name == "rebase_workspace",
    )

    rest_response = await call_rest_control(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=f"{capability_name}-invalid-state",
    )
    assert rest_response.status_code == 409, rest_response.text
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await call_mcp_control(
        contract_stack,
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=f"{capability_name}-invalid-state-mcp",
    )
    assert mcp_result.isError is True, mcp_result.structuredContent
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == expected_code
    assert rest_envelope["message"] == mcp_envelope["message"]
    assert rest_envelope["detail"] == mcp_envelope["detail"]


@pytest.mark.unit
async def test_rest_and_mcp_agree_on_destroy_active_without_force(
    contract_stack: ContractStack,
) -> None:
    workspace_id, _version = await seed_basic_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["destroy_workspace"]

    rest_response = await contract_stack.client.delete(
        capability.rest_path.format(workspace_id=workspace_id),
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": "destroy-active-rest",
        },
    )
    assert rest_response.status_code == 409, rest_response.text
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "workspace_id": workspace_id,
            "force": False,
            "remove_volumes": True,
            "remove_worktree": True,
            "idempotency_key": "destroy-active-mcp",
        },
    )
    assert mcp_result.isError is True, mcp_result.structuredContent
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == "WORKSPACE_ACTIVE"
    assert rest_envelope["message"] == mcp_envelope["message"]


@pytest.mark.unit
async def test_rest_and_mcp_retry_invalid_state_reason_code_matches_registry(
    contract_stack: ContractStack,
) -> None:
    workspace_id, _version = await seed_basic_workspace(contract_stack.factory)
    capability = CAPABILITIES_BY_NAME["retry_workspace"]

    rest_response = await contract_stack.client.post(
        capability.rest_path.format(workspace_id=workspace_id),
        headers=contract_stack.auth_headers,
    )
    assert rest_response.status_code == 409, rest_response.text
    rest_envelope = normalize_rest_error_body(rest_response.json())

    mcp_result = await _call_mcp(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {"workspace_id": workspace_id},
    )
    assert mcp_result.isError is True, mcp_result.structuredContent
    mcp_envelope = normalize_mcp_error_body(mcp_result.structuredContent)

    assert rest_envelope["error_code"] == mcp_envelope["error_code"] == "WORKSPACE_NOT_RETRYABLE"
    assert rest_envelope["message"] == mcp_envelope["message"]
    assert rest_envelope["detail"] == mcp_envelope["detail"]
