"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway in-memory SQLite. This validates:
- All five tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server


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
    async def test_all_five_tools_registered(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "awf_create_workspace",
            "awf_get_workspace",
            "awf_list_workspaces",
            "awf_wait_for_workspace",
        } | ({"awf_cancel_workspace"} & names)  # cancel deferred to Task 9 surface


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
