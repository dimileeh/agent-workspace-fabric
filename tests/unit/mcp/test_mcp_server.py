"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway in-memory SQLite. This validates:
- All five tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.runtime.logs import LogStore


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def mcp(factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    service = WorkspaceService(factory)
    return build_mcp_server(service=service)


_CREATE_ARGS: dict[str, object] = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add docstring",
    "task_prompt": "Add a one-line docstring to src/module/__init__.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


async def _call(mcp, name, args) -> object:  # type: ignore[no-untyped-def]
    """Unwrap FastMCP's call_tool payload.

    FastMCP returns ``(content, structured)`` where ``structured`` is the
    tool's return value for dict returns, or ``{"result": <value>}`` for
    primitive / None / list returns. This helper normalises to the underlying
    value so tests can assert against it directly.
    """
    _, payload = await mcp.call_tool(name, args)
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


class TestToolRegistration:
    @pytest.mark.unit
    async def test_existing_and_observability_tools_registered(
        self, mcp
    ) -> None:  # type: ignore[no-untyped-def]
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert {
            "awf_create_workspace",
            "awf_get_workspace",
            "awf_list_workspaces",
            "awf_wait_for_workspace",
        } <= names
        assert {
            "awf_create_workspace_v2",
            "awf_list_workspace_events",
            "awf_list_workspace_logs",
            "awf_read_workspace_log",
        } <= names


class TestCreateWorkspace:
    @pytest.mark.unit
    async def test_happy_path_returns_workspace_payload(self, mcp) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)

        assert isinstance(payload, dict)
        assert payload["status"] == "requested"
        assert payload["id"].startswith("ws_")
        assert payload["task_title"] == _CREATE_ARGS["task_title"]
        assert payload["agent"] == "codex"
        assert payload["test_commands"] == ["pytest -q"]

    @pytest.mark.unit
    async def test_rejects_unknown_agent(self, mcp) -> None:  # type: ignore[no-untyped-def]
        bad = {**_CREATE_ARGS, "agent": "not-a-real-cli"}
        from mcp.shared.exceptions import McpError  # imported lazily to keep top clean

        with pytest.raises((McpError, Exception)):
            await _call(mcp, "awf_create_workspace", bad)


class TestCreateWorkspaceV2:
    @pytest.mark.unit
    async def test_persists_clean_v2_contract_fields(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "main",
                "task_title": "Add planner hook",
                "task_prompt": "Implement the planner hook.",
                "task_kind": "refactor_task",
                "agent": "claude_code",
                "task_external_id": "AIRA-42",
                "profile_ref": "python",
                "profile": {
                    "name": "inline-python",
                    "validation": {"requested_tier": 2},
                    "monitor": {"initial_review_grace_period_seconds": 333},
                },
                "validation_commands": ["uv run pytest tests/unit -q"],
                "requested_tier": 2,
                "auto_merge": False,
                "initial_review_grace_period_seconds": 12.5,
            },
        )

        assert isinstance(payload, dict)
        ws_id = payload["id"]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(ws_id))

        assert ws is not None
        assert ws.repo_url == "git@github.com:example/app.git"
        assert ws.branch_base == "main"
        assert ws.task_title == "Add planner hook"
        assert ws.task_prompt == "Implement the planner hook."
        assert ws.task_external_id == "AIRA-42"
        assert ws.task_kind == "refactor_task"
        assert ws.agent == "claude_code"
        assert ws.profile_ref == "python"
        assert ws.requested_profile is not None
        assert ws.requested_profile["name"] == "inline-python"
        assert ws.resolved_profile is not None
        assert ws.resolved_profile["validation"]["requested_tier"] == 2
        assert [
            item["command"] for item in ws.resolved_profile["phases"]["validate"]
        ] == ["uv run pytest tests/unit -q"]
        assert ws.test_commands == ["uv run pytest tests/unit -q"]
        assert ws.auto_merge is False
        assert ws.initial_review_grace_period_seconds == 12.5

    @pytest.mark.unit
    async def test_unknown_profile_ref_returns_structured_invalid_profile_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        from mcp.types import CallToolResult

        result = await mcp.call_tool(
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "main",
                "task_title": "Add planner hook",
                "task_prompt": "Implement the planner hook.",
                "profile_ref": "missing-profile",
            },
        )

        message = "unknown workspace profile_ref: missing-profile"
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_PROFILE",
            "message": message,
            "detail": None,
        }
        assert result.content[0].type == "text"


class TestGetAndList:
    @pytest.mark.unit
    async def test_get_returns_the_workspace_just_created(self, mcp) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = created["id"]  # type: ignore[index]

        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})
        assert fetched is not None
        assert fetched["id"] == ws_id  # type: ignore[index]
        assert fetched["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_get_unknown_id_returns_none(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await _call(mcp, "awf_get_workspace", {"workspace_id": "ws_nope"})
        assert result is None

    @pytest.mark.unit
    async def test_list_returns_newest_first(self, mcp) -> None:  # type: ignore[no-untyped-def]
        ids: list[str] = []
        for title in ["first", "second", "third"]:
            args = {**_CREATE_ARGS, "task_title": title}
            created = await _call(mcp, "awf_create_workspace", args)
            ids.append(created["id"])  # type: ignore[index]

        listed = await _call(mcp, "awf_list_workspaces", {"limit": 10})
        assert isinstance(listed, list)
        assert [r["id"] for r in listed] == list(reversed(ids))


class TestWaitForWorkspace:
    @pytest.mark.unit
    async def test_exits_immediately_when_already_terminal(self, mcp) -> None:  # type: ignore[no-untyped-def]
        # Simulate a workspace that's already terminal by creating one and
        # configuring the terminal_statuses to include 'requested'.
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = created["id"]  # type: ignore[index]

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
        ws_id = created["id"]  # type: ignore[index]

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
    async def test_lists_requested_workspace_events_with_limit_and_type(
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
        first_id = str(first["id"])  # type: ignore[index]
        second_id = str(second["id"])  # type: ignore[index]
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            first_ws = await repo.get(first_id)
            second_ws = await repo.get(second_id)
            assert first_ws is not None
            assert second_ws is not None
            first_old = await repo.add_event(
                first_ws,
                event_type="workspace.phase_started",
                reason_code="OLD",
                payload={"phase": "agent"},
            )
            first_new = await repo.add_event(
                first_ws,
                event_type="workspace.phase_started",
                reason_code="NEW",
                payload={"phase": "validation"},
            )
            wrong_workspace = await repo.add_event(
                second_ws,
                event_type="workspace.phase_started",
                reason_code="OTHER",
                payload={"phase": "validation"},
            )
            ignored_type = await repo.add_event(
                first_ws,
                event_type="workspace.log",
                reason_code="IGNORED",
                payload={"stream": "agent.stdout"},
            )
            first_old.occurred_at = base
            first_new.occurred_at = base + timedelta(seconds=2)
            wrong_workspace.occurred_at = base + timedelta(seconds=3)
            ignored_type.occurred_at = base + timedelta(seconds=4)
            await session.commit()

        events = await _call(
            mcp,
            "awf_list_workspace_events",
            {
                "workspace_id": first_id,
                "event_type": "workspace.phase_started",
                "limit": 1,
            },
        )

        assert isinstance(events, list)
        assert [event["workspace_id"] for event in events] == [first_id]
        assert [event["reason_code"] for event in events] == ["NEW"]
        assert [event["payload"] for event in events] == [{"phase": "validation"}]

    @pytest.mark.unit
    async def test_missing_workspace_events_return_none(
        self, mcp
    ) -> None:  # type: ignore[no-untyped-def]
        result = await _call(
            mcp,
            "awf_list_workspace_events",
            {"workspace_id": "ws_missing"},
        )

        assert result is None


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
        assert isinstance(listed, list)
        assert [stream["stream_id"] for stream in listed] == ["agent.stdout"]
        assert listed[0]["byte_count"] == len("alpha\nbeta\n")
        assert listed[0]["line_count"] == 2

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
            "text": "beta",
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
            "text": "",
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
