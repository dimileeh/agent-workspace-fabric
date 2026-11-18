"""MCP coverage for WorkspaceResponse.claim_active on awf_get_workspace."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from tests.postgres import postgres_test_engine

_CREATE_ARGS: dict[str, object] = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "base_branch": "development",
    "task_title": "Claim active MCP",
    "task_prompt": "Exercise claim_active via awf_get_workspace.",
    "agent": "codex",
    "validation_commands": ["pytest -q"],
    "provider_readiness_override": True,
    "provider_readiness_override_reason": "mcp claim_active fixture",
}


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def mcp(factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    return build_mcp_server(service=WorkspaceService(factory))


async def _call(mcp, name: str, args: dict[str, object]) -> object:  # type: ignore[no-untyped-def]
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


@pytest.mark.unit
async def test_get_workspace_includes_claim_active_for_live_claim(
    mcp,  # type: ignore[no-untyped-def]
    factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
    assert isinstance(created, dict)
    workspace_id = str(created["workspace_id"])
    future = datetime.now(UTC) + timedelta(minutes=10)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.execution_claimed_by = "mcp-worker"
        workspace.execution_claim_expires_at = future
        await session.commit()

    fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": workspace_id})

    assert isinstance(fetched, dict)
    assert fetched["claim_active"] is True
