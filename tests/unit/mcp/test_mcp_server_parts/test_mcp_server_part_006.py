"""MCP server + tool behaviour tests — workspace controls."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceControlResponse
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.controls import WorkspaceControlError
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def mcp(factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    service = WorkspaceService(factory)
    return build_mcp_server(service=service)


_CREATE_ARGS: dict[str, object] = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "base_branch": "development",
    "task_title": "Add docstring",
    "task_prompt": "Add a one-line docstring to src/module/__init__.py.",
    "agent": "codex",
    "validation_commands": ["pytest -q"],
    "provider_readiness_override": True,
    "provider_readiness_override_reason": "mcp default create fixture",
}


async def _call(mcp, name, args) -> object:  # type: ignore[no-untyped-def]
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


def _workspace_id(payload: object) -> str:
    assert isinstance(payload, dict)
    return str(payload["workspace_id"])


class _RecordingControlService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "cancel",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "stop_stack": stop_stack,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_cancel",
            operation_status="succeeded",
            status="cancelled",
            message="workspace cancellation requested",
        )

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "stop",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_stop",
            operation_status="succeeded",
            status="cancelled",
            message="workspace stack stopped",
        )

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "destroy",
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
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_destroy",
            operation_status="succeeded",
            status="destroyed",
            message="workspace destroyed",
        )


class _FailingControlService(_RecordingControlService):
    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        del workspace_id, reason, stop_stack, idempotency_key, expected_version
        raise WorkspaceControlError(error_code="NOPE", message="cancel refused")

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        del workspace_id, reason, idempotency_key, expected_version
        raise WorkspaceControlError(error_code="NOPE", message="stop refused")


class TestWorkspaceControls:
    @pytest.mark.unit
    async def test_cancel_workspace_calls_service_and_returns_structured_response(
        self,
    ) -> None:
        service = _RecordingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_cancel_workspace",
            {
                "workspace_id": "ws_control",
                "reason": "stale task",
                "stop_stack": False,
                "idempotency_key": "ik-cancel",
            },
        )

        assert service.calls == [
            (
                "cancel",
                {
                    "workspace_id": "ws_control",
                    "reason": "stale task",
                    "stop_stack": False,
                    "idempotency_key": "ik-cancel",
                    "expected_version": None,
                },
            )
        ]
        assert payload == {
            "workspace_id": "ws_control",
            "operation_id": "op_cancel",
            "operation_status": "succeeded",
            "status": "cancelled",
            "message": "workspace cancellation requested",
            "warnings": [],
        }

    @pytest.mark.unit
    async def test_stop_workspace_calls_service_and_returns_structured_response(
        self,
    ) -> None:
        service = _RecordingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_stop_workspace",
            {
                "workspace_id": "ws_control",
                "reason": "free local resources",
                "idempotency_key": "ik-stop",
            },
        )

        assert service.calls == [
            (
                "stop",
                {
                    "workspace_id": "ws_control",
                    "reason": "free local resources",
                    "idempotency_key": "ik-stop",
                    "expected_version": None,
                },
            )
        ]
        assert payload == {
            "workspace_id": "ws_control",
            "operation_id": "op_stop",
            "operation_status": "succeeded",
            "status": "cancelled",
            "message": "workspace stack stopped",
            "warnings": [],
        }

    @pytest.mark.unit
    async def test_destroy_workspace_calls_service_and_returns_structured_response(
        self,
    ) -> None:
        service = _RecordingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_destroy_workspace",
            {
                "workspace_id": "ws_control",
                "force": True,
                "remove_volumes": False,
                "remove_worktree": False,
                "idempotency_key": "ik-destroy",
            },
        )

        assert service.calls == [
            (
                "destroy",
                {
                    "workspace_id": "ws_control",
                    "force": True,
                    "remove_volumes": False,
                    "remove_worktree": False,
                    "idempotency_key": "ik-destroy",
                    "expected_version": None,
                },
            )
        ]
        assert payload == {
            "workspace_id": "ws_control",
            "operation_id": "op_destroy",
            "operation_status": "succeeded",
            "status": "destroyed",
            "message": "workspace destroyed",
            "warnings": [],
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("tool_name", "expected_message"),
        [
            ("awf_cancel_workspace", "cancel refused"),
            ("awf_stop_workspace", "stop refused"),
        ],
    )
    async def test_control_tool_errors_return_structured_mcp_error(
        self,
        tool_name: str,
        expected_message: str,
    ) -> None:
        service = _FailingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        result = await mcp.call_tool(
            tool_name,
            {"workspace_id": "ws_control", "idempotency_key": "ik-error"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "NOPE",
            "message": expected_message,
            "detail": None,
        }

    @pytest.mark.unit
    async def test_cancel_workspace_records_operation_through_real_service(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        workspace_id = _workspace_id(created)

        payload = await _call(
            mcp,
            "awf_cancel_workspace",
            {
                "workspace_id": workspace_id,
                "reason": "no longer needed",
                "stop_stack": False,
                "idempotency_key": "ik-real-cancel",
            },
        )
        operations_payload = await _call(
            mcp,
            "awf_list_workspace_operations",
            {"workspace_id": workspace_id},
        )

        assert payload["workspace_id"] == workspace_id  # type: ignore[index]
        assert payload["status"] == "cancelled"  # type: ignore[index]
        assert payload["message"] == "workspace cancellation requested"  # type: ignore[index]
        assert isinstance(operations_payload, dict)
        operations = operations_payload["items"]
        assert isinstance(operations, list)
        assert operations_payload["has_more"] is False
        assert operations[0]["type"] == "cancel"
        assert operations[0]["status"] == "succeeded"
        assert operations[0]["payload"] == {
            "owner": "operator_api",
            "source": "operator_api",
            "reason": "no longer needed",
            "reason_code": "OPERATOR_CANCEL",
            "requested_action": "cancel",
            "stop_stack": False,
        }
        assert operations[0]["idempotency_key"] == "ik-real-cancel"
        assert operations[0]["result"] == {"status": "cancelled"}

    @pytest.mark.unit
    async def test_destroy_workspace_requires_force_for_active_workspace(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        from mcp.types import CallToolResult

        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        workspace_id = _workspace_id(created)

        result = await mcp.call_tool(
            "awf_destroy_workspace",
            {"workspace_id": workspace_id, "idempotency_key": "ik-destroy-active"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "WORKSPACE_ACTIVE",
            "message": "Active workspaces require force=true before destroy.",
            "detail": None,
        }
