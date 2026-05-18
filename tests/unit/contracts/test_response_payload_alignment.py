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
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

import awf.api.routes.controls as controls_route
from awf.cli.main import app as cli_app
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import (
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    ContractCapability,
    implemented_surface_capabilities,
)
from tests.unit.contracts._control_scenarios import (
    CONTROL_CAPABILITY_NAMES,
    call_mcp_control,
    call_rest_control,
    control_success_status,
    install_control_side_effect_stubs,
    seed_workspace_for_control,
)
from tests.unit.contracts._stack import ContractStack

_runner = CliRunner()

READ_RESPONSE_CAPABILITY_NAMES = tuple(
    capability.name
    for capability in implemented_surface_capabilities()
    if capability.is_safe_read
    and capability.is_mcp_implemented
    and capability.mcp_tool is not None
    and capability.response_fields
    # workspace_artifact_download returns a raw base64 `content` field that
    # cannot be exercised by the generic MCP response-alignment loop; it is
    # covered by TestReadWorkspaceArtifact in test_mcp_server.py instead.
    and capability.name != "workspace_artifact_download"
)


@pytest.mark.unit
async def test_create_registry_response_fields_match_mcp_payload(
    contract_stack: ContractStack,
) -> None:
    """The create registry must document the MCP payload clients receive."""
    capability = CAPABILITIES_BY_NAME["create_workspace"]

    mcp_payload = await _call_mcp_structured(
        contract_stack.mcp,
        capability.mcp_tool or "",
        {
            "repo_url": "git@github.com:example/create-response.git",
            "task_title": "Create response contract",
            "task_prompt": "Exercise create response metadata.",
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "response contract fixture",
        },
    )

    assert capability.response_fields <= set(mcp_payload)


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


async def _call_mcp_structured(mcp: Any, name: str, args: dict[str, object]) -> dict[str, Any]:
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
@pytest.mark.parametrize("capability_name", CONTROL_CAPABILITY_NAMES)
async def test_control_rest_matches_mcp_structured_content_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every registered control returns the same REST/MCP top-level envelope."""
    install_control_side_effect_stubs(contract_stack, monkeypatch)
    rest_workspace, _rest_version = await seed_workspace_for_control(
        contract_stack.factory,
        capability_name,
    )
    mcp_workspace, _mcp_version = await seed_workspace_for_control(
        contract_stack.factory,
        capability_name,
    )

    rest_response = await call_rest_control(
        contract_stack,
        capability_name,
        workspace_id=rest_workspace,
        idempotency_key=f"{capability_name}-rest-response",
    )
    mcp_result = await call_mcp_control(
        contract_stack,
        capability_name,
        workspace_id=mcp_workspace,
        idempotency_key=f"{capability_name}-mcp-response",
    )

    assert rest_response.status_code == control_success_status(capability_name), rest_response.text
    assert isinstance(mcp_result, CallToolResult)
    assert mcp_result.isError is False, mcp_result.structuredContent
    assert isinstance(mcp_result.structuredContent, dict)

    rest_body = rest_response.json()
    mcp_body = mcp_result.structuredContent
    capability = CAPABILITIES_BY_NAME[capability_name]
    assert capability.response_fields <= set(rest_body)
    assert capability.response_fields <= set(mcp_body)
    assert set(rest_body) == set(mcp_body), (capability_name, rest_body, mcp_body)
    for key in capability.response_fields - {"workspace_id", "operation_id", "id"}:
        assert rest_body[key] == mcp_body[key], (capability_name, key, rest_body, mcp_body)


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", sorted(READ_RESPONSE_CAPABILITY_NAMES))
async def test_safe_read_response_envelope_fields_for_registry(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every implemented safe read with declared fields returns a REST/MCP envelope."""
    if capability_name == "core_release_readiness":
        _install_release_readiness_stub(monkeypatch)
    ids = await _seed_read_response_state(contract_stack)
    capability = CAPABILITIES_BY_NAME[capability_name]

    rest_response = await contract_stack.client.get(
        _read_rest_path(capability, ids),
        params=_read_rest_params(capability_name),
        headers=contract_stack.auth_headers if capability.auth_required else None,
    )
    assert rest_response.status_code == 200, (capability_name, rest_response.text)
    rest_payload = rest_response.json()

    mcp_result = await contract_stack.mcp.call_tool(
        capability.mcp_tool or "",
        _read_mcp_args(capability_name, ids),
    )
    mcp_payload = _mcp_payload(mcp_result)

    assert isinstance(rest_payload, dict), capability_name
    assert isinstance(mcp_payload, dict), (
        f"{capability_name}: MCP response must use the REST envelope shape; "
        f"got {type(mcp_payload).__name__}"
    )
    assert capability.response_fields <= set(rest_payload), (
        capability_name,
        capability.response_fields,
        rest_payload,
    )
    assert capability.response_fields <= set(mcp_payload), (
        capability_name,
        capability.response_fields,
        mcp_payload,
    )


async def _seed_read_response_state(contract_stack: ContractStack) -> dict[str, str]:
    async with contract_stack.factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/read-contract.git",
            branch_base="main",
            task_title="Read response contract",
            task_prompt="Exercise read response envelopes.",
            task_external_id="READ-CONTRACT-1",
            task_class="test_task",
            owned_paths=["src/awf/**"],
            agent="codex",
            test_commands=["pytest -q"],
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        operation = await OperationRepository(session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.validate,
            status=OperationStatus.succeeded,
            payload={"owner": "operator_api", "source": "operator_api"},
        )
        log_path = Path(contract_stack.settings.work_dir) / "logs" / workspace.id / "agent.stdout"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("alpha\nbeta\n", encoding="utf-8")
        stream = await WorkspaceLogStreamRepository(session).create_or_get(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
            path=str(log_path),
        )
        stream.byte_count = len("alpha\nbeta\n")
        stream.line_count = 2
        await session.commit()
        return {
            "workspace_id": workspace.id,
            "operation_id": operation.id,
            "task_ref": task.id,
            "stream_id": stream.stream_id,
        }


def _read_rest_path(capability: ContractCapability, ids: dict[str, str]) -> str:
    return capability.rest_path.format(
        workspace_id=ids["workspace_id"],
        task_ref=ids["task_ref"],
        operation_id=ids["operation_id"],
        stream_id=ids["stream_id"],
    )


def _read_rest_params(capability_name: str) -> dict[str, object]:
    if capability_name == "read_workspace_log":
        return {"offset": 0, "limit_bytes": 5}
    if capability_name in {
        "workspace_overview",
        "merge_queue",
        "list_tasks",
        "list_task_attempts",
        "workspace_validation",
        "workspace_stale_reasons",
        "workspace_artifacts",
        "locks",
        "overlap_graph",
        "workspace_operations",
        "global_operations",
        "global_events",
    }:
        return {"limit": 2}
    return {}


def _read_mcp_args(capability_name: str, ids: dict[str, str]) -> dict[str, object]:
    if capability_name in {"get_workspace", "workspace_runtime"}:
        return {"workspace_id": ids["workspace_id"]}
    if capability_name == "wait_for_workspace":
        return {
            "workspace_id": ids["workspace_id"],
            "terminal_statuses": ["requested"],
            "poll_interval_seconds": 0.1,
            "timeout_seconds": 1.0,
        }
    if capability_name == "list_task_attempts":
        return {"task_ref": ids["task_ref"], "limit": 2}
    if capability_name in {
        "workspace_validation",
        "workspace_stale_reasons",
        "workspace_artifacts",
        "workspace_operations",
        "workspace_events",
    }:
        return {"workspace_id": ids["workspace_id"], "limit": 2}
    if capability_name == "global_events":
        return {"limit": 2}
    if capability_name == "workspace_logs":
        return {"workspace_id": ids["workspace_id"]}
    if capability_name == "read_workspace_log":
        return {
            "workspace_id": ids["workspace_id"],
            "stream_id": ids["stream_id"],
            "offset": 0,
            "limit_bytes": 5,
        }
    if capability_name == "get_operation":
        return {"operation_id": ids["operation_id"]}
    if capability_name in {
        "workspace_overview",
        "merge_queue",
        "list_tasks",
        "locks",
        "overlap_graph",
        "global_operations",
    }:
        return {"limit": 2}
    if capability_name == "core_release_readiness":
        return {"allow_generic_failures": True, "allow_slo_breach": True}
    return {}


def _mcp_payload(result: Any) -> Any:
    if isinstance(result, CallToolResult):
        assert result.isError is False, result.structuredContent
        return result.structuredContent
    _content, payload = result
    if isinstance(payload, dict) and list(payload) == ["result"]:
        return payload["result"]
    return payload


def _install_release_readiness_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.api.routes import health as health_route
    from awf.service import readiness as readiness_module
    from awf.service.readiness import CoreReadinessCheck, CoreReadinessReport

    async def collect(**_kwargs: object) -> CoreReadinessReport:
        return CoreReadinessReport(
            status="ok",
            checks=(
                CoreReadinessCheck(
                    name="contract",
                    status="ok",
                    reason_code="CONTRACT_READY",
                    message="contract stub",
                    evidence={},
                ),
            ),
            next_actions=(),
        )

    monkeypatch.setattr(health_route, "collect_core_readiness_report", collect)
    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", collect)


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
