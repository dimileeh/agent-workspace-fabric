"""Contract: REST and MCP must return identical structures for read surfaces.

For each capability we seed deterministic state, fetch via REST and via the
MCP tool, validate both through the canonical Pydantic response model, and
assert deep structural equality. ``WorkspaceService`` is the shared façade,
so divergence here flags a route/tool adapter, not a data-model bug.
"""

from __future__ import annotations

import pytest

from awf.api.routes.health import HealthResponse
from awf.api.schemas import (
    MergeQueueListResponse,
    OperationListResponse,
    OperationResponse,
    WorkspaceEventListResponse,
    WorkspaceLockListResponse,
    WorkspaceOverviewListResponse,
    WorkspaceResponse,
)

from tests.unit.contract.conftest import (
    ContractStack,
    call_mcp_structured,
    seed_monitoring_workspace,
    seed_requested_workspace,
    seed_workspace_operation,
)


@pytest.mark.unit
async def test_get_workspace_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await seed_monitoring_workspace(contract_stack.factory)

    rest_payload = (
        await contract_stack.rest_client.get(f"/v1/workspaces/{workspace_id}")
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_get_workspace",
        {"workspace_id": workspace_id},
    )

    assert isinstance(mcp_payload, dict)
    rest_validated = WorkspaceResponse.model_validate(rest_payload)
    mcp_validated = WorkspaceResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_list_workspaces_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    await seed_requested_workspace(contract_stack.factory, title="contract-list-1")
    await seed_requested_workspace(contract_stack.factory, title="contract-list-2")
    await seed_requested_workspace(contract_stack.factory, title="contract-list-3")

    rest_payload = (await contract_stack.rest_client.get("/v1/workspaces")).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_workspaces",
        {"limit": 50},
    )

    assert isinstance(rest_payload, list)
    assert isinstance(mcp_payload, list)
    assert len(rest_payload) == len(mcp_payload) == 3
    rest_validated = [WorkspaceResponse.model_validate(item) for item in rest_payload]
    mcp_validated = [WorkspaceResponse.model_validate(item) for item in mcp_payload]
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_list_workspace_overview_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    await seed_requested_workspace(contract_stack.factory, title="contract-overview-1")
    await seed_requested_workspace(contract_stack.factory, title="contract-overview-2")

    rest_payload = (
        await contract_stack.rest_client.get("/v1/workspaces/overview")
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_workspace_overview",
        {"limit": 50},
    )

    assert isinstance(rest_payload, dict)
    assert isinstance(mcp_payload, dict)
    rest_validated = WorkspaceOverviewListResponse.model_validate(rest_payload)
    mcp_validated = WorkspaceOverviewListResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_list_workspace_events_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await seed_monitoring_workspace(contract_stack.factory)

    rest_payload = (
        await contract_stack.rest_client.get(f"/v1/workspaces/{workspace_id}/events")
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_workspace_events",
        {"workspace_id": workspace_id},
    )

    assert isinstance(mcp_payload, list)
    rest_validated = WorkspaceEventListResponse.model_validate(rest_payload)
    # MCP exposes events as a bare list; REST wraps it in the canonical envelope.
    # Wrap MCP into the envelope before comparing to the REST shape.
    mcp_envelope = WorkspaceEventListResponse.model_validate(
        {"items": mcp_payload, "limit": 50, "cursor": None}
    )
    assert {item.id for item in rest_validated.items} == {
        item.id for item in mcp_envelope.items
    }
    assert rest_validated.items == mcp_envelope.items


@pytest.mark.unit
async def test_list_workspace_operations_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await seed_monitoring_workspace(contract_stack.factory)

    rest_payload = (
        await contract_stack.rest_client.get(f"/v1/workspaces/{workspace_id}/operations")
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_workspace_operations",
        {"workspace_id": workspace_id, "limit": 50},
    )

    assert isinstance(mcp_payload, list)
    rest_validated = OperationListResponse.model_validate(rest_payload)
    mcp_envelope = OperationListResponse.model_validate(
        {"items": mcp_payload, "limit": 50, "cursor": None}
    )
    assert rest_validated.items == mcp_envelope.items


@pytest.mark.unit
async def test_list_operations_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    await seed_monitoring_workspace(contract_stack.factory)
    await seed_monitoring_workspace(
        contract_stack.factory, title="Operations parity 2"
    )

    rest_payload = (
        await contract_stack.rest_client.get(
            "/v1/operations",
            headers=contract_stack.auth_headers,
        )
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_operations",
        {"limit": 50},
    )

    assert isinstance(rest_payload, dict)
    assert isinstance(mcp_payload, dict)
    rest_validated = OperationListResponse.model_validate(rest_payload)
    mcp_validated = OperationListResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_get_operation_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    workspace_id = await seed_monitoring_workspace(contract_stack.factory)
    operation_id = await seed_workspace_operation(
        contract_stack.factory, workspace_id=workspace_id
    )

    rest_payload = (
        await contract_stack.rest_client.get(
            f"/v1/operations/{operation_id}",
            headers=contract_stack.auth_headers,
        )
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_get_operation",
        {"operation_id": operation_id},
    )

    assert isinstance(rest_payload, dict)
    assert isinstance(mcp_payload, dict)
    rest_validated = OperationResponse.model_validate(rest_payload)
    mcp_validated = OperationResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_merge_queue_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    await seed_monitoring_workspace(contract_stack.factory, with_open_candidate=True)

    rest_payload = (
        await contract_stack.rest_client.get(
            "/v1/merge-queue",
            headers=contract_stack.auth_headers,
        )
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_merge_queue",
        {"limit": 50},
    )

    assert isinstance(rest_payload, dict)
    assert isinstance(mcp_payload, dict)
    rest_validated = MergeQueueListResponse.model_validate(rest_payload)
    mcp_validated = MergeQueueListResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_locks_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    await seed_monitoring_workspace(contract_stack.factory)

    rest_payload = (
        await contract_stack.rest_client.get(
            "/v1/locks",
            headers=contract_stack.auth_headers,
        )
    ).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp,
        "awf_list_locks",
        {"limit": 50},
    )

    assert isinstance(rest_payload, dict)
    assert isinstance(mcp_payload, dict)
    rest_validated = WorkspaceLockListResponse.model_validate(rest_payload)
    mcp_validated = WorkspaceLockListResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated


@pytest.mark.unit
async def test_service_health_payload_aligned_across_rest_and_mcp(
    contract_stack: ContractStack,
) -> None:
    rest_payload = (await contract_stack.rest_client.get("/healthz")).json()
    mcp_payload = await call_mcp_structured(
        contract_stack.mcp, "awf_get_service_health", {}
    )

    rest_validated = HealthResponse.model_validate(rest_payload)
    mcp_validated = HealthResponse.model_validate(mcp_payload)
    assert rest_validated == mcp_validated
