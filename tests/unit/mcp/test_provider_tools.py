"""MCP discovery/diagnostics share the canonical schema and service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP

from awf.mcp.provider_tools import register_provider_tools


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
        mcp=mcp, service=SimpleNamespace(), safe_result=lambda payload, **_k: payload
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
