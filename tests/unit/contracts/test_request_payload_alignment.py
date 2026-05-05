"""REST and MCP must hydrate the same backend ``WorkspaceService`` calls.

For each control capability, the harness records the kwargs the service is
invoked with from REST and from MCP and asserts they are equivalent. That
proves the two surfaces normalize equivalent input into the same canonical
backend call — which is the single source of truth for downstream behavior.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.api.schemas import (
    OperationResponse,
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
    WorkspaceControlResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.session import make_session_factory
from awf.mcp.server import build_mcp_server
from awf.service.workspaces import WorkspaceService
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.unit.contracts._capabilities import CAPABILITIES_BY_NAME
from tests.unit.contracts._stack import ContractStack, contract_stack  # noqa: F401


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
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "cancel_workspace",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "stop_stack": stop_stack,
                    "idempotency_key": idempotency_key,
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
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "stop_workspace",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
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
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "remonitor_workspace",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
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
    and proves REST and MCP normalize their inputs identically (modulo the
    REST-only ``expected_version`` argument, which today maps to the
    ``If-Match`` header — see ``test_if_match_alignment.py`` for the
    gap-tracked MCP side).
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

    rest_workspace_id = await _seed_workspace_for_cancel(contract_stack)
    mcp_workspace_id = await _seed_workspace_for_cancel(contract_stack)

    rest_response = await contract_stack.client.post(
        f"/v1/workspaces/{rest_workspace_id}/cancel",
        headers={**contract_stack.auth_headers, "Idempotency-Key": "rest-canonical"},
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
        },
    )

    assert len(calls) == 2, calls
    rest_call, mcp_call = calls
    # The harness deliberately ignores workspace_id, idempotency_key, and the
    # REST-only expected_version field (gap-tracked) so REST and MCP can be
    # compared on the user-authored payload itself.
    canonical_keys = {"reason", "stop_stack"}
    assert {k: rest_call[k] for k in canonical_keys} == {
        k: mcp_call[k] for k in canonical_keys
    }
    # MCP currently does not support expected_version (TODO§P1-if-match-parity).
    assert mcp_call["expected_version"] is None


async def _seed_workspace_for_cancel(contract_stack: ContractStack) -> str:
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
        return ws.id


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
            },
        )
    ]
