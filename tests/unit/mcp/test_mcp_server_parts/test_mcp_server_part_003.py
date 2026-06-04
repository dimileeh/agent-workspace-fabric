"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway PostgreSQL. This validates:
- All tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    WorkspaceControlResponse,
)
from awf.common.config import Settings
from awf.common.redaction import REDACTION_MARKER
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import (
    OperationRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp import metrics_tools as metrics_tools_mod
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.logs import LogStore
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from awf.service.provider_readiness import KNOWN_SECRET_ENV_KEYS
from tests.postgres import postgres_test_engine

_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "CURSOR_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


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


class _RejectWholeEncodeStr(str):
    """String sentinel that catches accidental full-buffer encoding."""

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        """Reject full-buffer encoding in byte-offset fragment tests."""
        raise AssertionError("whole expanded log text must not be encoded")


def _operation_response() -> OperationResponse:
    return OperationResponse(
        id="op_prevalidated",
        workspace_id="ws_prevalidated",
        type="validate",
        status="succeeded",
        error_code=None,
        error_message=None,
        payload=None,
        result=None,
        idempotency_key=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
        finished_at=None,
    )


def _low_disk_check(settings: Settings) -> DiskCheck:
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=100,
        used_bytes=95,
        free_bytes=5,
        percent_free=5.0,
        threshold_bytes=10,
        ok=False,
        status="fail",
        reason="INSUFFICIENT_DISK",
        detail="free_bytes=5 threshold_bytes=10",
    )


def _ok_disk_check(settings: Settings) -> DiskCheck:
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=100,
        used_bytes=20,
        free_bytes=80,
        percent_free=80.0,
        threshold_bytes=10,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
        detail=None,
    )


@pytest.mark.unit
def test_unknown_leading_log_value_fragment_end_peeks_before_encoding_expanded_text() -> None:
    """Avoid encoding an expanded log window when the first byte is a delimiter."""
    assert (
        metrics_tools_mod._unknown_leading_log_value_fragment_end(
            _RejectWholeEncodeStr(" already-delimited"),
            result_offset=10,
        )
        == 0
    )


@pytest.mark.unit
def test_unknown_leading_log_value_fragment_end_counts_utf8_bytes_to_delimiter() -> None:
    """Keep leading-fragment offsets in bytes when scanning multibyte text."""
    assert metrics_tools_mod._unknown_leading_log_value_fragment_end(
        "αβ done",
        result_offset=10,
    ) == len("αβ".encode())


@pytest.mark.unit
def test_workspace_log_assignment_value_covers_byte_ignores_out_of_range_context() -> None:
    """Only visible assignment values covering the requested byte can redact it."""
    assert not metrics_tools_mod._workspace_log_assignment_value_covers_byte(
        "ordinary SERVICE_TOKEN=value",
        -1,
    )
    assert not metrics_tools_mod._workspace_log_assignment_value_covers_byte(
        "ordinary SERVICE_TOKEN=value",
        0,
    )


@pytest.mark.unit
def test_workspace_log_assignment_value_covers_byte_breaks_using_byte_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop before later assignments when byte offsets prove the value is after start."""
    text = "é SERVICE_TOKEN=value OTHER_TOKEN=secret"
    value_start_chars = text.index("value")
    value_start_bytes = len(text[:value_start_chars].encode())
    requested_byte = value_start_bytes - 1

    class _FakeMatch:
        """Expose the match methods used by the byte-offset helper."""

        def __init__(self, value_start: int, value: str) -> None:
            """Capture the synthetic value span for the fake regex match."""
            self._value_start = value_start
            self._value = value

        def start(self, group: str) -> int:
            """Return the synthetic value start index."""
            assert group == "value"
            return self._value_start

        def group(self, group: str) -> str:
            """Return the synthetic value text."""
            assert group == "value"
            return self._value

    class _FakeTokenAssignmentRe:
        """Yield one fake token assignment before the expected early break."""

        def finditer(self, candidate: str):  # type: ignore[no-untyped-def]
            """Yield the first assignment and fail if scanning continues."""
            assert candidate == text
            yield _FakeMatch(value_start_chars, "value")
            raise AssertionError("byte-aware early break should skip later matches")

    monkeypatch.setattr(metrics_tools_mod, "_LOG_TOKEN_ASSIGNMENT_RE", _FakeTokenAssignmentRe())

    assert not metrics_tools_mod._workspace_log_assignment_value_covers_byte(
        text,
        requested_byte,
    )


async def _call(mcp, name, args) -> object:  # type: ignore[no-untyped-def]
    """Unwrap FastMCP's call_tool payload.

    FastMCP returns ``(content, structured)`` where ``structured`` is the
    tool's return value for dict returns, or ``{"result": <value>}`` for
    primitive / None / list returns. This helper normalises to the underlying
    value so tests can assert against it directly.
    """
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


def _optional_string_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    assert isinstance(any_of, list)
    string_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "string"),
        None,
    )
    assert string_schema is not None, f"Could not find string schema in anyOf: {any_of}"
    assert isinstance(string_schema, dict)
    return string_schema


def _optional_object_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    if any_of is None:
        assert schema.get("type") == "object"
        return schema

    assert isinstance(any_of, list)
    object_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "object"),
        None,
    )
    assert object_schema is not None, f"Could not find object schema in anyOf: {any_of}"
    assert isinstance(object_schema, dict)
    return object_schema


def _assert_idempotency_key_schema(schema: dict[str, object]) -> None:
    string_schema = _optional_string_schema(schema)
    assert str(schema["description"]).startswith("Required idempotency key")
    assert schema["minLength"] == 1
    assert string_schema["maxLength"] == 128
    assert "default" not in schema


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


class TestWaitForWorkspace:
    @pytest.mark.unit
    async def test_exits_immediately_when_already_terminal(self, mcp) -> None:  # type: ignore[no-untyped-def]
        # Simulate a workspace that's already terminal by creating one and
        # configuring the terminal_statuses to include 'requested'.
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        result = await _call(
            mcp,
            "awf_wait_for_workspace",
            {
                "workspace_id": ws_id,
                "terminal_statuses": ["requested"],
                "poll_interval_seconds": 0.1,
                "timeout_seconds": 5.0,
            },
        )
        assert result is not None
        assert result["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_returns_current_state_on_timeout(self, mcp) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        # Pick terminal statuses the workspace will never reach + tight timeout.
        result = await _call(
            mcp,
            "awf_wait_for_workspace",
            {
                "workspace_id": ws_id,
                "terminal_statuses": ["completed", "failed"],
                "poll_interval_seconds": 0.1,
                "timeout_seconds": 1.0,
            },
        )
        # On timeout we still return the current state (status=requested).
        assert result is not None
        assert result["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_returns_none_for_unknown_id(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await _call(
            mcp,
            "awf_wait_for_workspace",
            {
                "workspace_id": "ws_never_existed",
                "poll_interval_seconds": 0.1,
                "timeout_seconds": 1.0,
            },
        )
        assert result is None


class TestWorkspaceEvents:
    @pytest.mark.unit
    async def test_lists_workspace_events_with_envelope_and_has_more(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        first = await _call(mcp, "awf_create_workspace", {**_CREATE_ARGS, "task_title": "first"})
        first_id = _workspace_id(first)
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            first_ws = await repo.get(first_id)
            assert first_ws is not None
            old = await repo.add_event(
                first_ws,
                event_type="test.workspace_events.pagination",
                reason_code="OLD",
                payload={"phase": "agent"},
            )
            new = await repo.add_event(
                first_ws,
                event_type="test.workspace_events.pagination",
                reason_code="NEW",
                payload={"phase": "validation"},
            )
            old.occurred_at = base + timedelta(days=30)
            new.occurred_at = base + timedelta(days=30, seconds=2)
            await session.commit()

        result = await mcp.call_tool(
            "awf_list_workspace_events",
            {
                "workspace_id": first_id,
                "event_type": "test.workspace_events.pagination",
                "limit": 1,
            },
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert result.structuredContent is not None
        payload = result.structuredContent
        assert len(payload["items"]) == 1
        assert payload["items"][0]["reason_code"] == "NEW"
        assert payload["has_more"] is True
        assert payload["limit"] == 1
        assert payload["cursor"] is None
        assert payload["next_cursor"] is None

        result_all = await mcp.call_tool(
            "awf_list_workspace_events",
            {"workspace_id": first_id, "limit": 50},
        )
        assert isinstance(result_all, CallToolResult)
        payload_all = result_all.structuredContent
        assert payload_all is not None
        reason_codes = {item["reason_code"] for item in payload_all["items"]}
        assert {"OLD", "NEW"} <= reason_codes
        assert payload_all["has_more"] is False
        assert payload_all["next_cursor"] is None

    @pytest.mark.unit
    async def test_missing_workspace_events_return_null_tool_result(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_list_workspace_events",
            {"workspace_id": "ws_missing"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert result.structuredContent is None

    @pytest.mark.unit
    async def test_workspace_events_filter_by_event_type(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.add_event(
                ws,
                event_type="workspace.phase_started",
                reason_code="STARTED",
                payload={"phase": "agent"},
            )
            await repo.add_event(
                ws,
                event_type="workspace.log",
                reason_code="LOG",
                payload={"stream": "stdout"},
            )
            await session.commit()

        result = await mcp.call_tool(
            "awf_list_workspace_events",
            {"workspace_id": ws_id, "event_type": "workspace.phase_started", "limit": 50},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        payload = result.structuredContent
        assert payload is not None
        for item in payload["items"]:
            assert item["event_type"] == "workspace.phase_started"

    @pytest.mark.unit
    async def test_workspace_events_event_type_validation_bounds(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        props = tools["awf_list_workspace_events"].inputSchema["properties"]
        string_schema = next(s for s in props["event_type"]["anyOf"] if s.get("type") == "string")
        assert string_schema["minLength"] == 1
        assert string_schema["maxLength"] == 64


class TestGlobalEvents:
    @pytest.mark.unit
    async def test_list_events_returns_empty_list(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_list_events",
            {"event_type": "test.global_events.empty_probe", "limit": 50},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        payload = result.structuredContent
        assert payload is not None
        assert payload["items"] == []
        assert payload["has_more"] is False
        assert payload["limit"] == 50
        assert payload["cursor"] is None
        assert payload["next_cursor"] is None

    @pytest.mark.unit
    async def test_list_events_returns_events_across_workspaces(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        first = await _call(mcp, "awf_create_workspace", {**_CREATE_ARGS, "task_title": "first"})
        second = await _call(
            mcp,
            "awf_create_workspace",
            {**_CREATE_ARGS, "task_title": "second"},
        )
        first_id = _workspace_id(first)
        second_id = _workspace_id(second)

        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            first_ws = await repo.get(first_id)
            second_ws = await repo.get(second_id)
            assert first_ws is not None
            assert second_ws is not None
            first_event = await repo.add_event(
                first_ws,
                event_type="workspace.phase_started",
                reason_code="FIRST",
                payload={"phase": "agent"},
            )
            first_event.occurred_at = base
            second_event = await repo.add_event(
                second_ws,
                event_type="workspace.phase_started",
                reason_code="SECOND",
                payload={"phase": "agent"},
            )
            second_event.occurred_at = base + timedelta(seconds=2)
            await session.commit()

        result = await mcp.call_tool("awf_list_events", {"limit": 50})
        assert isinstance(result, CallToolResult)
        payload = result.structuredContent
        assert payload is not None
        assert payload["has_more"] is False
        assert payload["limit"] == 50
        assert payload["cursor"] is None
        assert payload["next_cursor"] is None
        workspace_ids = {item["workspace_id"] for item in payload["items"]}
        assert first_id in workspace_ids
        assert second_id in workspace_ids
        occurred_at_times = [item["occurred_at"] for item in payload["items"]]
        assert occurred_at_times == sorted(occurred_at_times, reverse=True)

    @pytest.mark.unit
    async def test_list_events_filters_by_workspace_id(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        first = await _call(mcp, "awf_create_workspace", {**_CREATE_ARGS, "task_title": "first"})
        second = await _call(
            mcp,
            "awf_create_workspace",
            {**_CREATE_ARGS, "task_title": "second"},
        )
        first_id = _workspace_id(first)
        second_id = _workspace_id(second)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            first_ws = await repo.get(first_id)
            second_ws = await repo.get(second_id)
            assert first_ws is not None
            assert second_ws is not None
            await repo.add_event(
                first_ws,
                event_type="workspace.phase_started",
                reason_code="FIRST",
                payload={"phase": "agent"},
            )
            await repo.add_event(
                second_ws,
                event_type="workspace.phase_started",
                reason_code="SECOND",
                payload={"phase": "agent"},
            )
            await session.commit()

        result = await mcp.call_tool("awf_list_events", {"workspace_id": first_id, "limit": 50})
        assert isinstance(result, CallToolResult)
        payload = result.structuredContent
        assert payload is not None
        assert payload["has_more"] is False
        assert payload["limit"] == 50
        assert payload["cursor"] is None
        assert payload["next_cursor"] is None
        first_items = [i for i in payload["items"] if i["workspace_id"] == first_id]
        second_items = [i for i in payload["items"] if i["workspace_id"] == second_id]
        assert len(first_items) >= 1
        assert len(second_items) == 0

    @pytest.mark.unit
    async def test_list_events_filters_by_event_type(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.add_event(
                ws,
                event_type="workspace.phase_started",
                reason_code="STARTED",
                payload={"phase": "agent"},
            )
            await repo.add_event(
                ws,
                event_type="workspace.log",
                reason_code="LOG",
                payload={"stream": "stdout"},
            )
            await session.commit()

        result = await mcp.call_tool(
            "awf_list_events", {"event_type": "workspace.phase_started", "limit": 50}
        )
        assert isinstance(result, CallToolResult)
        payload = result.structuredContent
        assert payload is not None
        phase_started_events = [
            i for i in payload["items"] if i["event_type"] == "workspace.phase_started"
        ]
        assert len(phase_started_events) >= 1
        for item in payload["items"]:
            assert item["event_type"] == "workspace.phase_started"

    @pytest.mark.unit
    async def test_list_events_respects_limit(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(ws_id)
            assert ws is not None
            for i in range(5):
                event = await repo.add_event(
                    ws,
                    event_type="test.global_events.pagination",
                    reason_code=f"EVENT_{i}",
                    payload={"i": i},
                )
                event.occurred_at = base + timedelta(days=30, seconds=i)
            await session.commit()

        result = await mcp.call_tool(
            "awf_list_events",
            {"event_type": "test.global_events.pagination", "limit": 2},
        )
        assert isinstance(result, CallToolResult)
        payload = result.structuredContent
        assert payload is not None
        assert len(payload["items"]) == 2
        assert [item["reason_code"] for item in payload["items"]] == ["EVENT_4", "EVENT_3"]
        assert payload["has_more"] is True
        assert payload["limit"] == 2
        assert payload["cursor"] is None
        assert payload["next_cursor"] is None

    @pytest.mark.unit
    async def test_list_events_limit_bounds(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert "awf_list_events" in tools
        props = tools["awf_list_events"].inputSchema["properties"]
        assert props["limit"]["default"] == 50
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == 500

    @pytest.mark.unit
    async def test_list_events_event_type_validation_bounds(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        props = tools["awf_list_events"].inputSchema["properties"]
        string_schema = next(s for s in props["event_type"]["anyOf"] if s.get("type") == "string")
        assert string_schema["minLength"] == 1
        assert string_schema["maxLength"] == 64


class TestWorkspaceRuntime:
    @pytest.mark.unit
    async def test_get_workspace_runtime_returns_container_snapshot(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        class FakeRuntimeInspector:
            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                assert compose_project_name == "awf_ws_mcp_runtime"
                return RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="abc123",
                            image="awf-agent-runtime:latest",
                            state="running",
                            status="Up 1 minute",
                            health="healthy",
                            ports=["127.0.0.1:8000->8000/tcp"],
                            started_at="2026-04-25T10:00:00Z",
                        )
                    ],
                )

        service = WorkspaceService(factory, runtime_inspector=FakeRuntimeInspector())
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe runtime",
                task_prompt="Inspect runtime.",
                agent="codex",
                test_commands=[],
            )
            workspace.compose_project_name = "awf_ws_mcp_runtime"
            await session.commit()

        runtime = await _call(
            mcp,
            "awf_get_workspace_runtime",
            {"workspace_id": workspace.id},
        )

        assert runtime == {
            "workspace_id": workspace.id,
            "compose_project_name": "awf_ws_mcp_runtime",
            "stack_state": "running",
            "services": [
                {
                    "name": "agent",
                    "container_id": "abc123",
                    "image": "awf-agent-runtime:latest",
                    "state": "running",
                    "status": "Up 1 minute",
                    "health": "healthy",
                    "ports": ["127.0.0.1:8000->8000/tcp"],
                    "started_at": "2026-04-25T10:00:00Z",
                }
            ],
            "app_endpoints": [],
            "logs_available": True,
            "control_available": True,
            "reason": None,
        }

    @pytest.mark.unit
    async def test_get_workspace_runtime_missing_workspace_returns_none(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await _call(
            mcp,
            "awf_get_workspace_runtime",
            {"workspace_id": "ws_missing"},
        )

        assert result is None


class TestWorkspaceOperations:
    @pytest.mark.unit
    async def test_list_workspace_operations_respects_limit(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe operations",
                task_prompt="List operations.",
                agent="codex",
                test_commands=[],
            )
            repo = OperationRepository(session)
            create = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.create,
                status=OperationStatus.succeeded,
            )
            validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
            )
            stop = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.stop,
                status=OperationStatus.pending,
            )
            create.created_at = base
            validate.created_at = base + timedelta(seconds=1)
            stop.created_at = base + timedelta(seconds=2)
            await session.commit()

        payload = await _call(
            mcp,
            "awf_list_workspace_operations",
            {"workspace_id": workspace.id, "limit": 2},
        )

        assert isinstance(payload, dict)
        assert [item["id"] for item in payload["items"]] == [stop.id, validate.id]
        assert [item["type"] for item in payload["items"]] == ["stop", "validate"]
        assert [item["status"] for item in payload["items"]] == ["pending", "running"]
        assert payload["has_more"] is True
        assert payload["next_cursor"] is not None
        assert payload["limit"] == 2
        assert payload["cursor"] is None

        second_page = await _call(
            mcp,
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace.id,
                "limit": 2,
                "cursor": payload["next_cursor"],
            },
        )

        assert isinstance(second_page, dict)
        assert [item["id"] for item in second_page["items"]] == [create.id]
        assert second_page["has_more"] is False
        assert second_page["next_cursor"] is None
        assert second_page["cursor"] == payload["next_cursor"]

    @pytest.mark.unit
    async def test_list_workspace_operations_forwards_status_and_type_filters(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Filter workspace operations",
                task_prompt="List filtered operations.",
                agent="codex",
                test_commands=[],
            )
            repo = OperationRepository(session)
            create = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.create,
                status=OperationStatus.succeeded,
            )
            running_validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
            )
            running_create = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.create,
                status=OperationStatus.running,
            )
            pending_validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.pending,
            )
            create.created_at = base
            running_validate.created_at = base + timedelta(seconds=1)
            running_create.created_at = base + timedelta(seconds=2)
            pending_validate.created_at = base + timedelta(seconds=3)
            await session.commit()

        payload = await _call(
            mcp,
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace.id,
                "status": "running",
                "operation_type": "validate",
            },
        )

        assert isinstance(payload, dict)
        assert [item["id"] for item in payload["items"]] == [running_validate.id]
        assert [item["type"] for item in payload["items"]] == ["validate"]
        assert [item["status"] for item in payload["items"]] == ["running"]
        assert payload["has_more"] is False
        assert payload["limit"] == 50

    @pytest.mark.unit
    async def test_list_workspace_operations_missing_workspace_returns_not_found_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_list_workspace_operations", {"workspace_id": "ws_missing"}
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
            "detail": None,
        }

    @pytest.mark.unit
    async def test_list_workspace_operations_rejects_invalid_cursor(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Reject bad operation cursor",
                task_prompt="Exercise invalid operation cursor.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        result = await mcp.call_tool(
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace.id,
                "limit": 2,
                "cursor": "not-valid-cursor",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid operation list cursor.",
            "detail": None,
        }


class TestWorkspaceLogs:
    @pytest.mark.unit
    async def test_lists_and_reads_indexed_log_streams(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
        )
        await sink.write("alpha\nbeta\n")
        await sink.close()

        listed = await _call(
            mcp,
            "awf_list_workspace_logs",
            {"workspace_id": workspace.id},
        )
        assert isinstance(listed, dict)
        assert [stream["stream_id"] for stream in listed["items"]] == ["agent.stdout"]
        assert listed["items"][0]["byte_count"] == len("alpha\nbeta\n")
        assert listed["items"][0]["line_count"] == 2
        assert listed["limit"] == 1

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": 6,
                "limit_bytes": 4,
            },
        )
        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 6,
            "next_offset": 10,
            "eof": False,
            "data": "beta",
        }

        eof = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": len("alpha\nbeta\n"),
                "limit_bytes": 16,
            },
        )
        assert eof == {
            "stream_id": "agent.stdout",
            "offset": len("alpha\nbeta\n"),
            "next_offset": len("alpha\nbeta\n"),
            "eof": True,
            "data": "",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_uses_byte_offsets_after_multibyte_text(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Read workspace logs from byte offsets after multibyte text."""
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        prefix = "\U0001f525alpha\n"
        raw_text = f"{prefix}beta\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        offset = len(prefix.encode())
        limit_bytes = len(b"beta")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": offset,
            "next_offset": offset + limit_bytes,
            "eof": False,
            "data": "beta",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_offsets_when_expanded_context_starts_inside_multibyte_character(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Preserve byte offsets when redaction context starts inside UTF-8."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            prefix = "\U0001f525" + ("x" * 4095) + " "
            raw_text = f"{prefix}TARGET\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = len(prefix.encode())
        limit_bytes = len(b"TARGET")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": offset,
            "next_offset": offset + limit_bytes,
            "eof": False,
            "data": "TARGET",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_offsets_with_invalid_utf8_before_requested_window(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Preserve raw byte offsets when invalid UTF-8 appears before the slice."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe invalid log bytes",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            raw_bytes = b"\xffprefix TARGET\n"
            raw_log.write_bytes(raw_bytes)
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_bytes.index(b"TARGET")
        limit_bytes = len(b"TARGET")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": offset,
            "next_offset": offset + limit_bytes,
            "eof": False,
            "data": "TARGET",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_leading_invalid_utf8_at_offset_zero(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Return leading invalid UTF-8 bytes when the caller reads from offset zero."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe invalid leading log bytes",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            raw_bytes = b"\x80prefix\n"
            raw_log.write_bytes(raw_bytes)
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": 0,
                "limit_bytes": len(raw_bytes),
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 0,
            "next_offset": len(raw_bytes),
            "eof": True,
            "data": "\ufffdprefix\n",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_does_not_skip_short_non_eof_expanded_read(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Advance only through returned caller-window bytes on short non-EOF reads."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        service = WorkspaceService(factory)

        async def short_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a short non-EOF byte chunk for cursor advancement checks."""
            assert workspace_id == "ws_short"
            assert stream_id == "agent.stdout"
            assert offset == 0
            assert limit_bytes > 10
            assert include_bytes is True
            return {
                "stream_id": stream_id,
                "offset": offset,
                "next_offset": 8,
                "eof": False,
                "text": "01234567",
                "raw_bytes": b"01234567",
            }

        monkeypatch.setattr(service, "read_log", short_read_log)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_short",
                "stream_id": "agent.stdout",
                "offset": 5,
                "limit_bytes": 10,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 5,
            "next_offset": 8,
            "eof": False,
            "data": "567",
        }

    @pytest.mark.unit
    async def test_missing_workspace_or_stream_returns_none(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        missing_workspace = await _call(
            mcp,
            "awf_list_workspace_logs",
            {"workspace_id": "ws_missing"},
        )
        missing_stream = await _call(
            mcp,
            "awf_read_workspace_log",
            {"workspace_id": workspace.id, "stream_id": "agent.stderr"},
        )

        assert missing_workspace is None
        assert missing_stream is None

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_setup_secret_refs(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Redact setup credential references returned through MCP log reads."""
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        token = "ghp_mcpWorkspaceLogSecret123456"
        plain_ref = "plain-file:///home/user/.awf/secrets/codex.default"
        env_ref = "env://OPENAI_API_KEY"
        raw_text = f"setup token={token} ref={plain_ref} env={env_ref}\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": 0,
                "limit_bytes": len(raw_text),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["stream_id"] == "setup.stdout"
        assert chunk["offset"] == 0
        assert int(chunk["next_offset"]) > 0
        assert chunk["eof"] is True
        data = str(chunk["data"])
        for raw in (token, plain_ref, env_ref, "/home/user/.awf/secrets/codex.default"):
            assert raw not in data
        assert "<redacted>" in data

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_compose_env_provider_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redact provider tokens sourced only from the local Compose env file."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        secret = "compose-only-anthropic-provider-secret"
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"ANTHROPIC_AUTH_TOKEN={secret}\n", encoding="utf-8")
        monkeypatch.setattr(
            metrics_tools_mod.service_config,
            "resolve_local_service_compose_env_file",
            lambda _env_file=metrics_tools_mod.service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE: (
                compose_env_file
            ),
        )

        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe Compose env redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        raw_text = f"provider emitted {secret} without assignment context\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": raw_text.index(secret),
                "limit_bytes": len(secret),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["data"] == REDACTION_MARKER
        assert secret not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_compose_env_custom_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redact Compose-only exact secrets whose keys use service secret naming."""
        key = "CUSTOM_CLIENT_SECRET"
        assert key not in KNOWN_SECRET_ENV_KEYS
        for env_key in (*KNOWN_SECRET_ENV_KEYS, key, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(env_key, raising=False)

        secret = "bare-compose-custom-value"
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"{key}={secret}\n", encoding="utf-8")
        monkeypatch.setattr(
            metrics_tools_mod.service_config,
            "resolve_local_service_compose_env_file",
            lambda _env_file=metrics_tools_mod.service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE: (
                compose_env_file
            ),
        )

        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe Compose custom secret redaction",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        raw_text = f"service emitted {secret} without assignment context\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": raw_text.index(secret),
                "limit_bytes": len(secret),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["data"] == REDACTION_MARKER
        assert secret not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_custom_compose_env_file_provider_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redact exact provider secrets from the MCP server's selected env file."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        custom_secret = "custom-compose-env-provider-secret"
        default_secret = "default-compose-env-provider-secret"
        default_env_file = tmp_path / "default.env"
        custom_env_file = tmp_path / "custom.env"
        default_env_file.write_text(
            f"ANTHROPIC_AUTH_TOKEN={default_secret}\n",
            encoding="utf-8",
        )
        custom_env_file.write_text(
            f"ANTHROPIC_AUTH_TOKEN={custom_secret}\n",
            encoding="utf-8",
        )

        def _resolve_env_file(
            env_file: Path = metrics_tools_mod.service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE,
        ) -> Path | None:
            return custom_env_file if env_file == custom_env_file else default_env_file

        monkeypatch.setattr(
            metrics_tools_mod.service_config,
            "resolve_local_service_compose_env_file",
            _resolve_env_file,
        )

        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(
            service=service,
            settings=Settings(_env_file=None),
            compose_env_file=custom_env_file,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe custom Compose env redaction",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        raw_text = f"provider emitted {custom_secret} without assignment context\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": raw_text.index(custom_secret),
                "limit_bytes": len(custom_secret),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["data"] == REDACTION_MARKER
        assert custom_secret not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_slice_starting_inside_configured_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Mask a log slice that starts inside a configured extra secret."""
        secret = "opaque-nonpattern-workspace-secret-value"
        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(
            service=service,
            settings=Settings(_env_file=None, github_token=secret),
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "setup.stdout.log"
            raw_log.parent.mkdir(parents=True)
            raw_text = f"setup AWF_GITHUB_TOKEN={secret} done\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="setup.stdout",
                source="setup",
                name="Setup stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_text.index("workspace")
        limit_bytes = len("workspace")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["offset"] == offset
        assert chunk["next_offset"] == offset + limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_pattern_only_secret_assignment_beyond_context(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Mask a slice that starts deep inside a pattern-only assignment value."""
        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "setup.stdout.log"
            raw_log.parent.mkdir(parents=True)
            fragment = "deep-secret-fragment"
            raw_text = f"setup SERVICE_TOKEN={'x' * 4_500}{fragment} done\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="setup.stdout",
                source="setup",
                name="Setup stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_text.index(fragment)
        limit_bytes = len(fragment)
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["offset"] == offset
        assert chunk["next_offset"] == offset + limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_skips_lookback_when_visible_assignment_context_redacts_slice(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Avoid a second log read when the current projection has assignment context."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        requested_offset = 10_000
        fragment = "visible-secret-fragment"
        requested_limit_bytes = len(fragment.encode())
        context_bytes = metrics_tools_mod._LOG_REDACTION_CONTEXT_BYTES  # noqa: SLF001
        first_offset = requested_offset - context_bytes - 1
        slice_start = requested_offset - first_offset
        assignment_prefix = b"SERVICE_TOKEN="
        raw_bytes = (
            assignment_prefix
            + (b"x" * (slice_start - len(assignment_prefix)))
            + fragment.encode()
            + b" done\n"
        )
        first_next_offset = first_offset + len(raw_bytes)
        calls: list[tuple[int, int]] = []

        service = WorkspaceService(factory)

        async def visible_assignment_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a projection whose leading fragment already has assignment context."""
            assert workspace_id == "ws_visible_assignment"
            assert stream_id == "setup.stdout"
            assert include_bytes is True
            calls.append((offset, limit_bytes))
            if len(calls) > 1:
                raise AssertionError("assignment context is already visible")
            assert offset == first_offset
            assert limit_bytes == context_bytes + 1 + requested_limit_bytes + context_bytes
            return {
                "stream_id": stream_id,
                "offset": offset,
                "next_offset": first_next_offset,
                "eof": False,
                "text": raw_bytes.decode(),
                "raw_bytes": raw_bytes,
            }

        monkeypatch.setattr(service, "read_log", visible_assignment_read_log)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_visible_assignment",
                "stream_id": "setup.stdout",
                "offset": requested_offset,
                "limit_bytes": requested_limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert calls == [(first_offset, context_bytes + 1 + requested_limit_bytes + context_bytes)]
        assert chunk["offset"] == requested_offset
        assert chunk["next_offset"] == requested_offset + requested_limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_assignment_lookback_failure(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mask an unknown leading assignment fragment if lookback is short."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        requested_offset = 10_000
        fragment = "leaking-assignment-tail"
        requested_limit_bytes = len(fragment.encode())
        context_bytes = metrics_tools_mod._LOG_REDACTION_CONTEXT_BYTES  # noqa: SLF001
        first_offset = requested_offset - context_bytes - 1
        leading_bytes = b"x" * (requested_offset - first_offset)
        narrow_bytes = leading_bytes + fragment.encode() + b" done\n"
        first_next_offset = first_offset + len(narrow_bytes)
        calls: list[tuple[int, int]] = []

        service = WorkspaceService(factory)

        async def short_lookback_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a short lookback projection for redaction fallback checks."""
            assert workspace_id == "ws_lookback_short"
            assert stream_id == "setup.stdout"
            assert include_bytes is True
            calls.append((offset, limit_bytes))
            if len(calls) == 1:
                assert offset == first_offset
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": first_next_offset,
                    "eof": False,
                    "text": narrow_bytes.decode(),
                    "raw_bytes": narrow_bytes,
                }
            if len(calls) == 2:
                assert offset == 0
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": requested_offset + requested_limit_bytes - 1,
                    "eof": False,
                    "text": "SERVICE_TOKEN=short",
                    "raw_bytes": b"SERVICE_TOKEN=short",
                }
            raise AssertionError("unexpected read_log call")

        monkeypatch.setattr(service, "read_log", short_lookback_read_log)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_lookback_short",
                "stream_id": "setup.stdout",
                "offset": requested_offset,
                "limit_bytes": requested_limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert calls == [
            (first_offset, context_bytes + 1 + requested_limit_bytes + context_bytes),
            (0, first_next_offset),
        ]
        assert chunk["offset"] == requested_offset
        assert chunk["next_offset"] == requested_offset + requested_limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_assignment_lookback_still_mid_fragment(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep masking if assignment lookback still starts inside a long value."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        requested_offset = 100_000
        fragment = "still-leaking-assignment-tail"
        requested_limit_bytes = len(fragment.encode())
        context_bytes = metrics_tools_mod._LOG_REDACTION_CONTEXT_BYTES  # noqa: SLF001
        lookback_bytes = metrics_tools_mod._LOG_REDACTION_ASSIGNMENT_LOOKBACK_BYTES  # noqa: SLF001
        first_offset = requested_offset - context_bytes - 1
        lookback_offset = first_offset - lookback_bytes
        first_bytes = (b"x" * (requested_offset - first_offset)) + fragment.encode() + b" done\n"
        lookback_result_bytes = (
            (b"x" * (requested_offset - lookback_offset)) + fragment.encode() + b" done\n"
        )
        first_next_offset = first_offset + len(first_bytes)
        calls: list[tuple[int, int]] = []

        service = WorkspaceService(factory)

        async def still_mid_fragment_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a covering lookback that still lacks the assignment key."""
            assert workspace_id == "ws_lookback_mid_fragment"
            assert stream_id == "setup.stdout"
            assert include_bytes is True
            calls.append((offset, limit_bytes))
            if len(calls) == 1:
                assert offset == first_offset
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": first_next_offset,
                    "eof": False,
                    "text": first_bytes.decode(),
                    "raw_bytes": first_bytes,
                }
            if len(calls) == 2:
                assert offset == lookback_offset
                assert limit_bytes == first_next_offset - lookback_offset
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": first_next_offset,
                    "eof": False,
                    "text": lookback_result_bytes.decode(),
                    "raw_bytes": lookback_result_bytes,
                }
            raise AssertionError("unexpected read_log call")

        monkeypatch.setattr(service, "read_log", still_mid_fragment_read_log)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_lookback_mid_fragment",
                "stream_id": "setup.stdout",
                "offset": requested_offset,
                "limit_bytes": requested_limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert calls == [
            (first_offset, context_bytes + 1 + requested_limit_bytes + context_bytes),
            (lookback_offset, first_next_offset - lookback_offset),
        ]
        assert chunk["offset"] == requested_offset
        assert chunk["next_offset"] == requested_offset + requested_limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_long_benign_token_without_assignment_context(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Preserve ordinary long tokens when no secret assignment prefix is found."""
        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe readable logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            fragment = "ordinary-fragment"
            raw_text = f"{'a' * 4_500}{fragment} done\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_text.index(fragment)
        limit_bytes = len(fragment)
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["offset"] == offset
        assert chunk["next_offset"] == offset + limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == fragment
