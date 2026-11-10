"""MCP discovery/diagnostics share the canonical schema and service."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from sqlalchemy import select

from awf.common.config import Settings
from awf.db.models import Operation, ResourceReservation, Workspace
from awf.db.repositories import WorkerHeartbeatRepository
from awf.db.session import make_session_factory
from awf.mcp.provider_tools import register_provider_tools
from awf.mcp.server import build_mcp_server
from awf.service.disk import DiskCheck
from awf.service.workspaces import WorkspaceService


async def test_tools_use_selected_material_and_same_create_service(monkeypatch):
    mcp = FastMCP("providers")
    query = AsyncMock(
        return_value=SimpleNamespace(
            model_dump=lambda **_k: {"status": "unavailable", "manual_entry_allowed": True}
        )
    )
    create = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda **_k: {"workspace_id": "ws_diag"})
    )
    monkeypatch.setattr("awf.mcp.provider_tools.ProviderCatalog.query", query)
    monkeypatch.setattr("awf.mcp.provider_tools.create_connection_test", create)
    register_provider_tools(
        mcp=mcp,
        service=SimpleNamespace(),
        safe_result=lambda payload, **_k: payload,
        settings_value=Settings(_env_file=None),
        disk_check_provider=None,
    )
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert {"awf_query_provider_models", "awf_test_provider_connection"} <= tools.keys()
    # Exercise registered functions directly without serializing a mocked MCP transport.
    result = await mcp._tool_manager.call_tool(
        "awf_query_provider_models", {"agent": "codex", "provider": "openrouter"}
    )
    assert result["manual_entry_allowed"]
    await mcp._tool_manager.call_tool(
        "awf_test_provider_connection",
        {
            "agent": "codex",
            "provider": "ollama",
            "model": "candidate:cloud",
            "idempotency_key": "once",
        },
    )
    assert create.call_args.kwargs["idempotency_key"] == "once"


@pytest.fixture
async def diagnostic_context(engine, tmp_path, mock_docker_cli_probe):
    factory = make_session_factory(engine)
    async with factory() as session:
        now = datetime.now(UTC)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="mcp-diagnostics",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=2,
        )
        await session.commit()
    settings = Settings(
        _env_file=None, work_dir=str(tmp_path / "awf-state"), min_free_disk_bytes=1234
    )
    return factory, settings


def disk_result(settings, *, ok):
    free = settings.min_free_disk_bytes + 1 if ok else 0
    return DiskCheck(
        path=str(settings.work_dir),
        checked_path=str(settings.work_dir),
        total_bytes=settings.min_free_disk_bytes + 1,
        used_bytes=0 if ok else settings.min_free_disk_bytes + 1,
        free_bytes=free,
        percent_free=100.0 if ok else 0.0,
        threshold_bytes=settings.min_free_disk_bytes,
        ok=ok,
        status="ok" if ok else "blocked",
        reason="SUFFICIENT_DISK" if ok else "INSUFFICIENT_DISK",
    )


@pytest.mark.parametrize("source", ["sync", "async", "filesystem"])
async def test_registered_diagnostic_rejects_disk_pressure_without_resources(
    diagnostic_context, monkeypatch, source
):
    factory, settings = diagnostic_context
    disk = disk_result(settings, ok=False)
    check = AsyncMock(return_value=disk) if source == "async" else Mock(return_value=disk)
    if source == "filesystem":
        monkeypatch.setattr("awf.mcp.server.check_disk_space", check)
    mcp = build_mcp_server(
        service=WorkspaceService(factory, settings=settings),
        settings=settings,
        disk_check_provider=None if source == "filesystem" else check,
    )

    result = await mcp.call_tool(
        "awf_test_provider_connection",
        {
            "agent": "codex",
            "provider": "ollama",
            "model": "candidate:cloud",
            "idempotency_key": "mcp-disk-denied",
        },
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["error_code"] == "INSUFFICIENT_DISK"
    assert result.structuredContent["detail"]["disk"] == disk.to_dict()
    if source == "filesystem":
        check.assert_called_once_with(
            settings.work_dir, min_free_bytes=settings.min_free_disk_bytes
        )
    elif source == "async":
        check.assert_awaited_once_with(settings)
    else:
        check.assert_called_once_with(settings)
    async with factory() as session:
        for model in (Workspace, Operation, ResourceReservation):
            assert list(await session.scalars(select(model.id))) == []


async def test_registered_diagnostic_replay_survives_subsequent_disk_pressure(diagnostic_context):
    factory, settings = diagnostic_context
    check = AsyncMock(side_effect=[disk_result(settings, ok=True), disk_result(settings, ok=False)])
    mcp = build_mcp_server(
        service=WorkspaceService(factory, settings=settings),
        settings=settings,
        disk_check_provider=check,
    )
    arguments = {
        "agent": "codex",
        "provider": "ollama",
        "model": "candidate:cloud",
        "idempotency_key": "mcp-disk-replay",
    }

    original = await mcp.call_tool("awf_test_provider_connection", arguments)
    replay = await mcp.call_tool("awf_test_provider_connection", arguments)

    assert isinstance(original, CallToolResult)
    assert isinstance(replay, CallToolResult)
    assert original.isError is False
    assert replay.isError is False
    assert original.structuredContent["label"] == "provider_connection_test"
    assert replay.structuredContent == original.structuredContent
    assert check.await_args_list == [((settings,), {}), ((settings,), {})]
    async with factory() as session:
        assert list(await session.scalars(select(Workspace.id))) == [
            original.structuredContent["workspace_id"]
        ]
        assert list(await session.scalars(select(Operation.id))) == [
            original.structuredContent["operation_id"]
        ]
        assert len(list(await session.scalars(select(ResourceReservation.id)))) == 1
