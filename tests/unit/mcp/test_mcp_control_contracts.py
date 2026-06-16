"""MCP control-tool contract tests for safe operator actions.

Covers: success, replay/idempotency, version conflict, invalid-state errors,
and auth-like failure mapping for the registered control tools.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import OperationResponse, WorkspaceControlResponse
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.controls import (
    VersionConflictError,
    WorkspaceControlError,
    WorkspaceRebaseStateError,
    WorkspaceRefreshStateError,
)
from tests.postgres import postgres_test_engine

_CONTROL_TOOLS = [
    "awf_cancel_workspace",
    "awf_stop_workspace",
    "awf_destroy_workspace",
    "awf_remonitor_workspace",
    "awf_guide_workspace",
    "awf_request_workspace_validation",
    "awf_refresh_workspace",
    "awf_rebase_workspace",
    "awf_retry_workspace",
]

_CONTROL_TOOLS_WITH_EXPECTED_VERSION = [
    "awf_cancel_workspace",
    "awf_stop_workspace",
    "awf_destroy_workspace",
    "awf_remonitor_workspace",
    "awf_guide_workspace",
    "awf_request_workspace_validation",
    "awf_refresh_workspace",
    "awf_rebase_workspace",
]

_IDEMPOTENCY_CONTROL_TOOLS = [
    "awf_cancel_workspace",
    "awf_stop_workspace",
    "awf_destroy_workspace",
    "awf_remonitor_workspace",
    "awf_request_workspace_validation",
    "awf_refresh_workspace",
    "awf_rebase_workspace",
]


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _call(mcp, name: str, args: dict[str, object]) -> object:
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        assert result.isError is False
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


async def _call_result(mcp, name: str, args: dict[str, object]) -> CallToolResult:
    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    return result


class _MockService:
    """Minimal mock that records calls and returns canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def cancel_workspace(
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
        reason: str | None = None,
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
        force: bool = False,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
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

    async def remonitor_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "remonitor",
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
            operation_id="op_remonitor",
            operation_status="succeeded",
            status="monitoring_pr",
            message="workspace PR monitor recovery requested",
        )

    async def guide_workspace(
        self,
        workspace_id: str,
        *,
        directive: str,
        reason: str | None = None,
        grants: list[str] | None = None,
        approve_policy_downgrade: bool = False,
        operator: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "guide",
                {
                    "workspace_id": workspace_id,
                    "directive": directive,
                    "reason": reason,
                    "grants": grants,
                    "approve_policy_downgrade": approve_policy_downgrade,
                    "operator": operator,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_guide",
            operation_status="succeeded",
            status="monitoring_pr",
            message="workspace operator guidance recorded",
        )

    async def request_validate_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> OperationResponse:
        self.calls.append(
            (
                "validate",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "requested_tier": requested_tier,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return OperationResponse(
            id="op_validate",
            workspace_id=workspace_id,
            type="validate",
            status="pending",
            error_code=None,
            error_message=None,
            payload=None,
            result=None,
            idempotency_key=idempotency_key,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            finished_at=None,
        )

    async def request_refresh_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> OperationResponse:
        self.calls.append(
            (
                "refresh",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        finished_at = datetime(2026, 1, 1, tzinfo=UTC)
        return OperationResponse(
            id="op_refresh",
            workspace_id=workspace_id,
            type="refresh",
            status="succeeded",
            error_code=None,
            error_message=None,
            payload=None,
            result={
                "status": "monitoring_pr",
                "reason_code": "OPERATOR_REFRESH",
                "requested_action": "refresh",
            },
            idempotency_key=idempotency_key,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=finished_at,
            finished_at=finished_at,
        )

    async def request_rebase_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> OperationResponse:
        self.calls.append(
            (
                "rebase",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return OperationResponse(
            id="op_rebase",
            workspace_id=workspace_id,
            type="rebase",
            status="pending",
            error_code=None,
            error_message=None,
            payload=None,
            result=None,
            idempotency_key=idempotency_key,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            finished_at=None,
        )

    async def retry_workspace(
        self,
        workspace_id: str,
        *,
        provider_readiness_override: bool = False,
        provider_readiness_override_reason: str | None = None,
    ) -> Any:
        self.calls.append(
            (
                "retry",
                {
                    "workspace_id": workspace_id,
                    "provider_readiness_override": provider_readiness_override,
                    "provider_readiness_override_reason": provider_readiness_override_reason,
                },
            )
        )
        return {
            "source_workspace_id": workspace_id,
            "new_workspace_id": "ws_new",
            "operation_id": "op_retry",
            "status": "requested",
            "attempt_number": 2,
        }

    def session_factory(self) -> Any:
        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        return _FakeSession()


class _FailingMockService(_MockService):
    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        stop_stack: bool = True,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        raise WorkspaceControlError(error_code="NOPE", message="cancel refused")

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        raise WorkspaceControlError(error_code="NOPE", message="stop refused")

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool = False,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        raise WorkspaceControlError(error_code="NOPE", message="destroy refused")

    async def remonitor_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        raise WorkspaceControlError(error_code="NOPE", message="remonitor refused")

    async def request_validate_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> OperationResponse:
        raise WorkspaceControlError(error_code="NOPE", message="validate refused")

    async def request_refresh_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> OperationResponse:
        raise WorkspaceControlError(error_code="NOPE", message="refresh refused")

    async def request_rebase_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> OperationResponse:
        raise WorkspaceControlError(error_code="NOPE", message="rebase refused")


@pytest.mark.unit
class TestToolRegistrationAndSchema:
    async def test_all_control_tools_are_registered(self) -> None:
        mcp = build_mcp_server(service=_MockService())
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        for name in _CONTROL_TOOLS:
            assert name in names, f"Missing MCP tool: {name}"

    async def test_refresh_and_rebase_tools_exist(self) -> None:
        mcp = build_mcp_server(service=_MockService())
        tools = {t.name: t for t in await mcp.list_tools()}
        assert "awf_refresh_workspace" in tools
        assert "awf_rebase_workspace" in tools

    async def test_all_control_tools_have_expected_version_in_schema(self) -> None:
        mcp = build_mcp_server(service=_MockService())
        tools = {t.name: t for t in await mcp.list_tools()}
        for name in _CONTROL_TOOLS_WITH_EXPECTED_VERSION:
            props = tools[name].inputSchema.get("properties", {})
            assert "expected_version" in props, f"{name} missing expected_version"
            schema = props["expected_version"]
            assert schema.get("type") == "integer" or any(
                s.get("type") == "integer" for s in schema.get("anyOf", []) if isinstance(s, dict)
            )
            assert schema.get("default") is None
            required = tools[name].inputSchema.get("required", [])
            assert "expected_version" not in required, f"{name} should not require expected_version"

    async def test_retry_workspace_does_not_gain_expected_version(self) -> None:
        mcp = build_mcp_server(service=_MockService())
        tools = {t.name: t for t in await mcp.list_tools()}
        props = tools["awf_retry_workspace"].inputSchema.get("properties", {})
        assert "expected_version" not in props

    async def test_guide_grants_mirrors_rest_request_contract(self) -> None:
        # The MCP guide tool must mirror the REST ``WorkspaceGuideRequest``
        # contract 1:1: ``grants`` is a non-null list defaulting to empty with a
        # 64-item bound, not a nullable field defaulting to ``None``.
        mcp = build_mcp_server(service=_MockService())
        tools = {t.name: t for t in await mcp.list_tools()}
        schema = tools["awf_guide_workspace"].inputSchema
        grants = schema["properties"]["grants"]
        assert grants.get("type") == "array"
        assert grants.get("items") == {"type": "string"}
        assert grants.get("maxItems") == 64
        # Non-null list defaulting via factory: never a required input.
        assert "grants" not in schema.get("required", [])


@pytest.mark.unit
class TestSuccessPaths:
    @pytest.mark.parametrize(
        ("tool_name", "args", "expected_call"),
        [
            (
                "awf_cancel_workspace",
                {
                    "workspace_id": "ws_cancel",
                    "reason": "r",
                    "stop_stack": False,
                    "idempotency_key": "ik-3",
                    "expected_version": 3,
                },
                (
                    "cancel",
                    {
                        "workspace_id": "ws_cancel",
                        "reason": "r",
                        "stop_stack": False,
                        "idempotency_key": "ik-3",
                        "expected_version": 3,
                    },
                ),
            ),
            (
                "awf_stop_workspace",
                {
                    "workspace_id": "ws_stop",
                    "reason": "r",
                    "idempotency_key": "ik-4",
                    "expected_version": 4,
                },
                (
                    "stop",
                    {
                        "workspace_id": "ws_stop",
                        "reason": "r",
                        "idempotency_key": "ik-4",
                        "expected_version": 4,
                    },
                ),
            ),
            (
                "awf_destroy_workspace",
                {
                    "workspace_id": "ws_destroy",
                    "force": True,
                    "idempotency_key": "ik-5",
                    "expected_version": 5,
                },
                (
                    "destroy",
                    {
                        "workspace_id": "ws_destroy",
                        "force": True,
                        "remove_volumes": True,
                        "remove_worktree": True,
                        "idempotency_key": "ik-5",
                        "expected_version": 5,
                    },
                ),
            ),
            (
                "awf_remonitor_workspace",
                {
                    "workspace_id": "ws_remonitor",
                    "idempotency_key": "ik-6",
                    "expected_version": 6,
                },
                (
                    "remonitor",
                    {
                        "workspace_id": "ws_remonitor",
                        "reason": None,
                        "idempotency_key": "ik-6",
                        "expected_version": 6,
                    },
                ),
            ),
            (
                "awf_guide_workspace",
                {
                    "workspace_id": "ws_guide",
                    "directive": "implement, do not defer",
                    "reason": "operator decision",
                    "idempotency_key": "ik-g",
                    "expected_version": 11,
                },
                (
                    "guide",
                    {
                        "workspace_id": "ws_guide",
                        "directive": "implement, do not defer",
                        "reason": "operator decision",
                        "grants": [],
                        "approve_policy_downgrade": False,
                        "operator": None,
                        "idempotency_key": "ik-g",
                        "expected_version": 11,
                    },
                ),
            ),
            (
                "awf_request_workspace_validation",
                {
                    "workspace_id": "ws_validate",
                    "reason": "r",
                    "requested_tier": 2,
                    "idempotency_key": "ik-7",
                    "expected_version": 7,
                },
                (
                    "validate",
                    {
                        "workspace_id": "ws_validate",
                        "reason": "r",
                        "requested_tier": 2,
                        "idempotency_key": "ik-7",
                        "expected_version": 7,
                    },
                ),
            ),
            (
                "awf_refresh_workspace",
                {
                    "workspace_id": "ws_refresh",
                    "reason": "r",
                    "idempotency_key": "ik-8",
                    "expected_version": 8,
                },
                (
                    "refresh",
                    {
                        "workspace_id": "ws_refresh",
                        "reason": "r",
                        "idempotency_key": "ik-8",
                        "expected_version": 8,
                    },
                ),
            ),
            (
                "awf_rebase_workspace",
                {
                    "workspace_id": "ws_rebase",
                    "reason": "r",
                    "idempotency_key": "ik-9",
                    "expected_version": 9,
                },
                (
                    "rebase",
                    {
                        "workspace_id": "ws_rebase",
                        "reason": "r",
                        "idempotency_key": "ik-9",
                        "expected_version": 9,
                    },
                ),
            ),
        ],
    )
    async def test_control_tool_calls_service_with_expected_arguments(
        self,
        tool_name: str,
        args: dict[str, object],
        expected_call: tuple[str, dict[str, object]],
    ) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        payload = await _call(mcp, tool_name, args)

        assert isinstance(payload, dict)
        assert service.calls == [expected_call]

    @pytest.mark.parametrize("tool_name", _IDEMPOTENCY_CONTROL_TOOLS)
    async def test_control_tools_preserve_nonblank_idempotency_key_verbatim(
        self, tool_name: str
    ) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)
        idempotency_key = "  literal mcp key  "

        payload = await _call(
            mcp,
            tool_name,
            {"workspace_id": "ws_x", "idempotency_key": idempotency_key},
        )

        assert isinstance(payload, dict)
        assert service.calls[0][1]["idempotency_key"] == idempotency_key

    async def test_guide_requires_idempotency_key(self) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        # ``directive`` is now optional (a blocked workspace can be resolved with
        # grants alone); the directive-or-grant contract is enforced by the
        # service. Blank idempotency key → structured invalid-request before the
        # service is called.
        result = await _call_result(
            mcp,
            "awf_guide_workspace",
            {"workspace_id": "ws_x", "directive": "do it", "idempotency_key": ""},
        )
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_REQUEST"
        assert service.calls == []

    async def test_refresh_and_rebase_return_operation_response_shape(self) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        refresh = await _call(
            mcp,
            "awf_refresh_workspace",
            {"workspace_id": "ws_r", "idempotency_key": "ik-r"},
        )
        rebase = await _call(
            mcp,
            "awf_rebase_workspace",
            {"workspace_id": "ws_b", "idempotency_key": "ik-b"},
        )

        assert isinstance(refresh, dict)
        assert refresh["type"] == "refresh"
        assert refresh["status"] == "succeeded"
        assert isinstance(rebase, dict)
        assert rebase["type"] == "rebase"
        assert rebase["status"] == "pending"


@pytest.mark.unit
class TestErrorMapping:
    @pytest.mark.parametrize("tool_name", _IDEMPOTENCY_CONTROL_TOOLS)
    async def test_idempotency_key_schema_documents_required_control_contract(
        self, tool_name: str
    ) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        tools = {tool.name: tool for tool in await mcp.list_tools()}
        schema = tools[tool_name].inputSchema
        required = schema.get("required", [])
        idempotency_key = schema["properties"]["idempotency_key"]
        string_schema = next(
            item for item in idempotency_key["anyOf"] if item.get("type") == "string"
        )

        assert "idempotency_key" in required
        assert idempotency_key["description"].startswith("Required idempotency key")
        assert idempotency_key["minLength"] == 1
        assert string_schema["maxLength"] == 128
        assert any(item.get("type") == "null" for item in idempotency_key["anyOf"])
        assert "default" not in idempotency_key
        assert service.calls == []

    @pytest.mark.parametrize("tool_name", _IDEMPOTENCY_CONTROL_TOOLS)
    async def test_blank_idempotency_key_returns_structured_mcp_error(self, tool_name: str) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        result = await _call_result(mcp, tool_name, {"workspace_id": "ws_x", "idempotency_key": ""})

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_REQUEST"
        assert (
            result.structuredContent["message"]
            == "Idempotency-Key header is required for this endpoint."
        )
        assert service.calls == []

    @pytest.mark.parametrize("tool_name", _IDEMPOTENCY_CONTROL_TOOLS)
    async def test_null_idempotency_key_returns_structured_mcp_error(self, tool_name: str) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        result = await _call_result(
            mcp, tool_name, {"workspace_id": "ws_x", "idempotency_key": None}
        )

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_REQUEST"
        assert (
            result.structuredContent["message"]
            == "Idempotency-Key header is required for this endpoint."
        )
        assert service.calls == []

    @pytest.mark.parametrize("tool_name", _IDEMPOTENCY_CONTROL_TOOLS)
    async def test_omitted_idempotency_key_is_rejected_by_required_mcp_schema(
        self, tool_name: str
    ) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        with pytest.raises(ToolError, match="idempotency_key"):
            await _call_result(mcp, tool_name, {"workspace_id": "ws_x"})
        assert service.calls == []

    @pytest.mark.parametrize(
        "tool_name",
        [
            "awf_cancel_workspace",
            "awf_stop_workspace",
            "awf_destroy_workspace",
            "awf_remonitor_workspace",
            "awf_request_workspace_validation",
            "awf_refresh_workspace",
            "awf_rebase_workspace",
        ],
    )
    @pytest.mark.parametrize(
        "args",
        [
            {"workspace_id": "ws_x", "idempotency_key": None},
            {"workspace_id": "ws_x", "idempotency_key": "   "},
        ],
    )
    async def test_missing_idempotency_key_returns_structured_invalid_request(
        self,
        tool_name: str,
        args: dict[str, object],
    ) -> None:
        service = _MockService()
        mcp = build_mcp_server(service=service)

        result = await _call_result(mcp, tool_name, args)

        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_REQUEST",
            "message": "Idempotency-Key header is required for this endpoint.",
            "detail": None,
        }
        assert service.calls == []

    @pytest.mark.parametrize(
        "tool_name",
        [
            "awf_cancel_workspace",
            "awf_stop_workspace",
            "awf_destroy_workspace",
            "awf_remonitor_workspace",
            "awf_request_workspace_validation",
            "awf_refresh_workspace",
            "awf_rebase_workspace",
        ],
    )
    async def test_workspace_control_error_returns_structured_mcp_error(
        self, tool_name: str
    ) -> None:
        service = _FailingMockService()
        mcp = build_mcp_server(service=service)

        args: dict[str, object] = {"workspace_id": "ws_x", "idempotency_key": "ik-x"}

        result = await _call_result(mcp, tool_name, args)

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOPE"
        assert "refused" in result.structuredContent["message"]
        assert "detail" in result.structuredContent

    async def test_version_conflict_returns_structured_error_with_versions(self) -> None:
        class _VersionConflictService(_MockService):
            async def request_refresh_workspace(
                self,
                workspace_id: str,
                *,
                reason: str | None = None,
                idempotency_key: str | None = None,
                expected_version: int | None = None,
            ) -> OperationResponse:
                raise VersionConflictError(expected_version=5, actual_version=7)

        service = _VersionConflictService()
        mcp = build_mcp_server(service=service)

        result = await _call_result(
            mcp,
            "awf_refresh_workspace",
            {"workspace_id": "ws_x", "idempotency_key": "ik-x", "expected_version": 5},
        )

        assert result.isError is True
        assert result.structuredContent["error_code"] == "VERSION_CONFLICT"
        assert result.structuredContent["detail"] == {"expected_version": 5, "actual_version": 7}

    async def test_invalid_state_errors_return_is_error_true(self) -> None:
        from types import SimpleNamespace

        class _StateErrorService(_MockService):
            async def request_refresh_workspace(
                self,
                workspace_id: str,
                *,
                reason: str | None = None,
                idempotency_key: str | None = None,
                expected_version: int | None = None,
            ) -> OperationResponse:
                raise WorkspaceRefreshStateError(SimpleNamespace(status="destroyed"))

            async def request_rebase_workspace(
                self,
                workspace_id: str,
                *,
                reason: str | None = None,
                idempotency_key: str | None = None,
                expected_version: int | None = None,
            ) -> OperationResponse:
                raise WorkspaceRebaseStateError(SimpleNamespace(status="destroyed"))

        service = _StateErrorService()
        mcp = build_mcp_server(service=service)

        for tool_name, expected_code in [
            ("awf_refresh_workspace", "WORKSPACE_STATE_NOT_REFRESHABLE"),
            ("awf_rebase_workspace", "WORKSPACE_STATE_NOT_REBASEABLE"),
        ]:
            result = await _call_result(
                mcp, tool_name, {"workspace_id": "ws_x", "idempotency_key": "ik-x"}
            )
            assert result.isError is True, tool_name
            assert result.structuredContent["error_code"] == expected_code, tool_name


@pytest.mark.unit
class TestIdempotencyAndReplay:
    async def test_same_idempotency_key_and_payload_returns_same_operation(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Idempotency test",
                task_prompt="Test idempotency.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/1"
            workspace.pr_number = 1
            workspace.status = WorkspaceStatus.monitoring_pr.value
            await session.commit()

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        first = await _call(
            mcp,
            "awf_refresh_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "refresh",
                "idempotency_key": "ik-refresh-1",
                "expected_version": None,
            },
        )
        second = await _call(
            mcp,
            "awf_refresh_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "refresh",
                "idempotency_key": "ik-refresh-1",
                "expected_version": None,
            },
        )

        assert isinstance(first, dict)
        assert isinstance(second, dict)
        assert first["id"] == second["id"]

    async def test_same_key_different_payload_returns_idempotency_conflict(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Idempotency conflict",
                task_prompt="Test conflict.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/1"
            workspace.pr_number = 1
            workspace.status = WorkspaceStatus.monitoring_pr.value
            await session.commit()

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        first = await _call(
            mcp,
            "awf_refresh_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "refresh",
                "idempotency_key": "ik-refresh-2",
            },
        )
        assert isinstance(first, dict)

        result = await _call_result(
            mcp,
            "awf_refresh_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "different",
                "idempotency_key": "ik-refresh-2",
            },
        )

        assert result.isError is True
        assert result.structuredContent["error_code"] == "IDEMPOTENCY_CONFLICT"

    async def test_version_conflict_rejects_without_mutating(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Version conflict",
                task_prompt="Test version.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/1"
            workspace.pr_number = 1
            workspace.status = WorkspaceStatus.monitoring_pr.value
            await session.commit()

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        result = await _call_result(
            mcp,
            "awf_refresh_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "refresh",
                "idempotency_key": "ik-vc",
                "expected_version": workspace.version + 1,
            },
        )

        assert result.isError is True
        assert result.structuredContent["error_code"] == "VERSION_CONFLICT"
        assert result.structuredContent["detail"]["expected_version"] == workspace.version + 1
        assert result.structuredContent["detail"]["actual_version"] == workspace.version


@pytest.mark.unit
class TestRealDbPaths:
    async def test_refresh_creates_operation_row(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Refresh DB test",
                task_prompt="Test refresh.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/1"
            workspace.pr_number = 1
            workspace.status = WorkspaceStatus.monitoring_pr.value
            await session.commit()

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        payload = await _call(
            mcp,
            "awf_refresh_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "operator refresh",
                "idempotency_key": "ik-op-refresh",
            },
        )

        assert isinstance(payload, dict)
        assert payload["type"] == "refresh"
        assert payload["status"] == OperationStatus.pending.value

        async with factory() as session:
            ops = await OperationRepository(session).list_for_workspace(workspace.id, limit=10)
            refresh_ops = [o for o in ops if o.type == OperationType.refresh.value]
            assert len(refresh_ops) == 1
            assert refresh_ops[0].status == OperationStatus.pending.value
            assert refresh_ops[0].result is None

    async def test_rebase_creates_operation_row(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Rebase DB test",
                task_prompt="Test rebase.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/2"
            workspace.pr_number = 2
            workspace.status = WorkspaceStatus.monitoring_pr.value
            await session.commit()

            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=None,
                owned_paths=list(workspace.owned_paths),
            )
            attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await MergeCandidateRepository(session).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha="h" * 40,
                base_sha="b" * 40,
            )
            await session.commit()

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        payload = await _call(
            mcp,
            "awf_rebase_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "operator rebase",
                "idempotency_key": "ik-op-rebase",
            },
        )

        assert isinstance(payload, dict)
        assert payload["type"] == "rebase"

        async with factory() as session:
            ops = await OperationRepository(session).list_for_workspace(workspace.id, limit=10)
            rebase_ops = [o for o in ops if o.type == OperationType.rebase.value]
            assert len(rebase_ops) == 1
            assert rebase_ops[0].status == OperationStatus.pending.value

    async def test_validate_with_expected_version_matches(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Validate version",
                task_prompt="Test version.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/1"
            workspace.pr_number = 1
            workspace.status = WorkspaceStatus.monitoring_pr.value
            await session.commit()
            version = workspace.version

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        payload = await _call(
            mcp,
            "awf_request_workspace_validation",
            {
                "workspace_id": workspace.id,
                "reason": "validate",
                "idempotency_key": "ik-validate-version",
                "expected_version": version,
            },
        )

        assert isinstance(payload, dict)
        assert payload["type"] == "validate"

    async def test_rebase_version_conflict_on_destroyed_workspace(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Rebase destroyed",
                task_prompt="Test destroyed.",
                agent="codex",
                test_commands=["pytest -q"],
            )
            workspace.pr_url = "https://github.com/example/app/pull/3"
            workspace.pr_number = 3
            workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        service = WorkspaceService(factory)
        mcp = build_mcp_server(service=service)

        result = await _call_result(
            mcp,
            "awf_rebase_workspace",
            {
                "workspace_id": workspace.id,
                "reason": "rebase",
                "idempotency_key": "ik-rebase-destroyed",
            },
        )

        assert result.isError is True
        assert result.structuredContent["error_code"] in {
            "WORKSPACE_STATE_NOT_REBASEABLE",
            "NOT_FOUND",
        }
