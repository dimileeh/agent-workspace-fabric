"""MCP (Model Context Protocol) surface for AWF.

Exposes the same workspace lifecycle that the REST API offers, but as typed
MCP tools that coding agents like Claude Code and Codex can invoke
without shelling out to curl.

The tool set intentionally mirrors the REST endpoints 1:1 so we don't have
two mental models to keep in sync.
"""

from awf.mcp.server import build_mcp_server

__all__ = ["build_mcp_server"]
