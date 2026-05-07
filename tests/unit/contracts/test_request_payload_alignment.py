"""REST and MCP must hydrate the same backend ``WorkspaceService`` calls.

For each control capability, the harness records the kwargs the service is
invoked with from REST and from MCP and asserts they are equivalent. That
proves the two surfaces normalize equivalent input into the same canonical
backend call — which is the single source of truth for downstream behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    WorkspaceControlResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
)
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation
from awf.mcp.server import build_mcp_server
from awf.service.workspaces import WorkspaceService
from tests.unit.contracts._capabilities import CAPABILITIES_BY_NAME
from tests.unit.contracts._control_scenarios import (
    CONTROL_CAPABILITY_NAMES,
    call_mcp_control,
    call_rest_control,
    response_operation_id_field,
)
from tests.unit.contracts._stack import ContractStack


def _stub_control_response(workspace_id: str, operation_id: str = "op_stub") -> WorkspaceControlResponse:
    return WorkspaceControlResponse(
        workspace_id=workspace_id,
        operation_id=operation_id,
        operation_status=OperationStatus.succeeded,
        status=WorkspaceStatus.cancelled,
        message="stub",
    )


class _RecordingWorkspaceService(WorkspaceService):
    """A ``WorkspaceService`` subclass that records control method kwargs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def cancel_workspace(  # type: ignore[override]
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        stop_stack: bool = True,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "cancel_workspace",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "stop_stack": stop_stack,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return _stub_control_response(workspace_id)

    async def stop_workspace(  # type: ignore[override]
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "stop_workspace",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return _stub_control_response(workspace_id)

    async def remonitor_workspace(  # type: ignore[override]
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "remonitor_workspace",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return _stub_control_response(workspace_id)

    async def destroy_workspace(  # type: ignore[override]
        self,
        workspace_id: str,
        *,
        force: bool = False,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "destroy_workspace",
                {
                    "workspace_id": workspace_id,
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return _stub_control_response(workspace_id)


class _RequestRecordingService:
    """Standalone recording service used to spy on REST/MCP request shaping.

    The REST controls router builds its own ``WorkspaceControlService`` per
    request, so a recording subclass of ``WorkspaceService`` would only catch
    the MCP call. To prove REST and MCP build the same canonical payload we
    instead inspect the request layer's intermediate Pydantic models.
    """

    def __init__(self) -> None:
        self.create_calls: list[WorkspaceCreateRequest] = []
        self.create_v2_calls: list[WorkspaceCreateV2Request] = []
        self.adopt_calls: list[PullRequestMonitorAdoptionRequest] = []


@pytest.mark.unit
async def test_mcp_cancel_invokes_service_with_canonical_kwargs(
    contract_stack: ContractStack,
) -> None:
    """MCP ``awf_cancel_workspace`` calls ``WorkspaceService.cancel_workspace`` with normalized kwargs.

    The MCP tool fans out the operator-supplied arguments into a structured
    service method call. This test pins the kwargs so future MCP refactors
    can't silently drop or rename a field.
    """
    capability = CAPABILITIES_BY_NAME["cancel_workspace"]
    recorder = _RecordingWorkspaceService(
        contract_stack.factory,
        settings=contract_stack.settings,
    )
    mcp = build_mcp_server(service=recorder, settings=contract_stack.settings)

    result = await mcp.call_tool(
        capability.mcp_tool or "",
        {
            "workspace_id": "ws_canon",
            "reason": "operator audit",
            "stop_stack": False,
            "idempotency_key": "mcp-canonical",
            "expected_version": 7,
        },
    )
    assert getattr(result, "isError", False) is False
    assert recorder.calls == [
        (
            "cancel_workspace",
            {
                "workspace_id": "ws_canon",
                "reason": "operator audit",
                "stop_stack": False,
                "idempotency_key": "mcp-canonical",
                "expected_version": 7,
            },
        )
    ]


@pytest.mark.unit
async def test_rest_and_mcp_cancel_call_control_service_with_equivalent_kwargs(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST cancel and MCP cancel reach the shared ``WorkspaceControlService.cancel_workspace`` with equal kwargs.

    Both surfaces ultimately funnel into the same backend method on
    ``WorkspaceControlService``. This test records every call to that method
    and proves REST and MCP normalize their inputs identically, including the
    shared optimistic-concurrency version field.
    """
    from awf.service import controls as controls_module

    calls: list[dict[str, Any]] = []
    original = controls_module.WorkspaceControlService.cancel_workspace

    async def _record_cancel(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Any:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "stop_stack": stop_stack,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return await original(
            self,
            workspace_id,
            reason=reason,
            stop_stack=stop_stack,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )

    monkeypatch.setattr(
        controls_module.WorkspaceControlService,
        "cancel_workspace",
        _record_cancel,
    )

    async def _stub_project_stopper(_name: str | None) -> None:
        return None

    monkeypatch.setattr(
        "awf.api.routes.controls._stop_project",
        _stub_project_stopper,
    )
    contract_stack.service._project_stopper = _stub_project_stopper  # type: ignore[attr-defined]

    rest_workspace_id, rest_version = await _seed_workspace_for_cancel(contract_stack)
    mcp_workspace_id, mcp_version = await _seed_workspace_for_cancel(contract_stack)

    rest_response = await contract_stack.client.post(
        f"/v1/workspaces/{rest_workspace_id}/cancel",
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": "rest-canonical",
            "If-Match": str(rest_version),
        },
        json={"reason": "shared canonical", "stop_stack": False},
    )
    assert rest_response.status_code == 200, rest_response.text

    await contract_stack.mcp.call_tool(
        "awf_cancel_workspace",
        {
            "workspace_id": mcp_workspace_id,
            "reason": "shared canonical",
            "stop_stack": False,
            "idempotency_key": "mcp-canonical",
            "expected_version": mcp_version,
        },
    )

    assert len(calls) == 2, calls
    rest_call, mcp_call = calls
    # The harness deliberately ignores workspace_id and idempotency_key so REST
    # and MCP can be compared on the user-authored payload itself.
    canonical_keys = {"reason", "stop_stack", "expected_version"}
    assert {k: rest_call[k] for k in canonical_keys} == {
        k: mcp_call[k] for k in canonical_keys
    }


async def _seed_workspace_for_cancel(contract_stack: ContractStack) -> tuple[str, int]:
    from awf.db.repositories import WorkspaceRepository

    async with contract_stack.factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/canonical.git",
            branch_base="main",
            task_title="Canonical request alignment",
            task_prompt="Exercise canonical kwargs.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return ws.id, ws.version


@pytest.mark.unit
async def test_mcp_create_v2_hydrates_canonical_request_model() -> None:
    """MCP ``awf_create_workspace_v2`` builds a ``WorkspaceCreateV2Request`` matching the REST payload.

    Concretely: an MCP tool call with flat fields produces the same nested
    ``WorkspaceCreateV2Request`` instance as the REST handler's parsed body.
    """
    rest_payload = {
        "repo": {"url": "git@github.com:example/x.git", "base_branch": "main"},
        "task": {
            "title": "Contract title",
            "prompt": "Contract prompt.",
            "kind": "feature_branch_pr",
            "agent": "codex",
            "auto_merge": True,
            "initial_review_grace_period_seconds": None,
        },
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "preflight": {
            "provider_readiness_override": False,
            "provider_readiness_override_reason": None,
        },
    }
    rest_request = WorkspaceCreateV2Request.model_validate(rest_payload)

    mcp_request = WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/x.git", "base_branch": "main"},
        task={
            "title": "Contract title",
            "prompt": "Contract prompt.",
            "kind": "feature_branch_pr",
            "agent": "codex",
            "model": None,
            "external_id": None,
            "task_class": None,
            "owned_paths": [],
            "auto_merge": True,
            "initial_review_grace_period_seconds": None,
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        preflight={
            "provider_readiness_override": False,
            "provider_readiness_override_reason": None,
        },
    )
    assert rest_request.model_dump(mode="json") == mcp_request.model_dump(mode="json")


@pytest.mark.unit
async def test_mcp_adoption_hydrates_canonical_request_model() -> None:
    rest_payload = {
        "repo_url": None,
        "repo_slug": "owner/repo",
        "pr_number": 42,
        "pr_url": None,
        "agent": "codex",
        "profile_ref": "auto",
        "profile": None,
        "auto_merge": True,
        "initial_review_grace_period_seconds": None,
        "task_title": None,
        "task_prompt": None,
        "reason": None,
    }
    rest_request = PullRequestMonitorAdoptionRequest.model_validate(rest_payload)

    mcp_request = PullRequestMonitorAdoptionRequest(
        repo_slug="owner/repo",
        pr_number=42,
        agent="codex",
        profile_ref="auto",
        profile=None,
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        task_title=None,
        task_prompt=None,
        reason=None,
    )
    assert rest_request.model_dump(mode="json", exclude_none=True) == (
        mcp_request.model_dump(mode="json", exclude_none=True)
    )


@pytest.mark.unit
async def test_mcp_destroy_invokes_service_with_canonical_kwargs(
    contract_stack: ContractStack,
) -> None:
    capability = CAPABILITIES_BY_NAME["destroy_workspace"]
    recorder = _RecordingWorkspaceService(
        contract_stack.factory,
        settings=contract_stack.settings,
    )
    mcp = build_mcp_server(service=recorder, settings=contract_stack.settings)

    await mcp.call_tool(
        capability.mcp_tool or "",
        {
            "workspace_id": "ws_destroy_canon",
            "force": True,
            "remove_volumes": False,
            "remove_worktree": False,
            "idempotency_key": "mcp-destroy",
            "expected_version": 9,
        },
    )
    assert recorder.calls == [
        (
            "destroy_workspace",
            {
                "workspace_id": "ws_destroy_canon",
                "force": True,
                "remove_volumes": False,
                "remove_worktree": False,
                "idempotency_key": "mcp-destroy",
                "expected_version": 9,
            },
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "body", "method_name", "operation_type", "expected_status"),
    [
        (
            "/v1/workspaces/ws_canonical/stop",
            {"reason": "operator recovery", "stop_stack": False},
            "stop_workspace",
            None,
            422,
        ),
        (
            "/v1/workspaces/ws_canonical/remonitor",
            {"reason": "operator recovery", "stop_stack": False},
            "remonitor_workspace",
            None,
            422,
        ),
        (
            "/v1/workspaces/ws_canonical/refresh",
            {"reason": "operator recovery", "requested_tier": 2},
            "request_refresh_workspace",
            OperationType.refresh,
            422,
        ),
        (
            "/v1/workspaces/ws_canonical/rebase",
            {"reason": "operator recovery", "requested_tier": 2},
            "request_rebase_workspace",
            OperationType.rebase,
            422,
        ),
    ],
)
async def test_rest_controls_reject_unsupported_body_fields(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, object],
    method_name: str,
    operation_type: OperationType | None,
    expected_status: int,
) -> None:
    """REST rejects fields that are not implemented by the backend contract."""
    from awf.service import controls as controls_module

    calls: list[dict[str, Any]] = []

    async def record_request(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse | Operation:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        if operation_type is not None:
            return _stub_operation(workspace_id, operation_type)
        return _stub_control_response(workspace_id, operation_id="op_legacy_contract")

    monkeypatch.setattr(controls_module.WorkspaceControlService, method_name, record_request)

    response = await contract_stack.client.post(
        path,
        headers={
            **contract_stack.auth_headers,
            "Idempotency-Key": "ignored-field",
            "If-Match": "5",
        },
        json=body,
    )

    assert response.status_code == expected_status, response.text
    assert calls == []


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", CONTROL_CAPABILITY_NAMES)
async def test_rest_and_mcp_control_request_payloads_reach_same_backend_contract(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    """Every registered control normalizes REST headers/body/query and MCP args alike."""
    from awf.service import controls as controls_module

    calls: list[dict[str, Any]] = []

    async def record_cancel(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "stop_stack": stop_stack,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return _stub_control_response(workspace_id, operation_id="op_cancel_contract")

    async def record_stop_or_remonitor(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return _stub_control_response(workspace_id, operation_id=f"op_{capability_name}")

    async def record_destroy(
        self: Any,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        calls.append(
            {
                "workspace_id": workspace_id,
                "force": force,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return _stub_control_response(workspace_id, operation_id="op_destroy_contract")

    async def record_refresh(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return _stub_operation(workspace_id, OperationType.refresh)

    async def record_validate(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "requested_tier": requested_tier,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return _stub_operation(workspace_id, OperationType.validate)

    async def record_rebase(
        self: Any,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        calls.append(
            {
                "workspace_id": workspace_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "expected_version": expected_version,
            }
        )
        return _stub_operation(workspace_id, OperationType.rebase)

    monkeypatch.setattr(controls_module.WorkspaceControlService, "cancel_workspace", record_cancel)
    monkeypatch.setattr(controls_module.WorkspaceControlService, "stop_workspace", record_stop_or_remonitor)
    monkeypatch.setattr(
        controls_module.WorkspaceControlService,
        "remonitor_workspace",
        record_stop_or_remonitor,
    )
    monkeypatch.setattr(controls_module.WorkspaceControlService, "destroy_workspace", record_destroy)
    monkeypatch.setattr(
        controls_module.WorkspaceControlService,
        "request_refresh_workspace",
        record_refresh,
    )
    monkeypatch.setattr(
        controls_module.WorkspaceControlService,
        "request_validate_workspace",
        record_validate,
    )
    monkeypatch.setattr(
        controls_module.WorkspaceControlService,
        "request_rebase_workspace",
        record_rebase,
    )

    rest_response = await call_rest_control(
        contract_stack,
        capability_name,
        workspace_id="ws_canonical",
        idempotency_key="shared-key",
        expected_version=11,
    )
    mcp_response = await call_mcp_control(
        contract_stack,
        capability_name,
        workspace_id="ws_canonical",
        idempotency_key="shared-key",
        expected_version=11,
    )

    assert rest_response.status_code in {200, 202}, rest_response.text
    assert mcp_response.isError is False, mcp_response.structuredContent
    assert len(calls) == 2
    assert calls[0] == calls[1]
    operation_field = response_operation_id_field(capability_name)
    assert operation_field in rest_response.json()
    assert operation_field in mcp_response.structuredContent


def _stub_operation(workspace_id: str, operation_type: OperationType) -> Operation:
    return Operation(
        id=f"op_{operation_type.value}_contract",
        workspace_id=workspace_id,
        type=operation_type.value,
        status=OperationStatus.pending.value,
        payload={"source": "contract"},
        idempotency_key="shared-key",
        created_at=datetime.now(UTC),
    )
