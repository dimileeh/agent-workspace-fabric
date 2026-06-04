"""Shared helpers for MCP setup-tools tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

from awf.common.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        AWF_DATABASE_URL="sqlite+aiosqlite:///unused.db",
        AWF_WORK_DIR=str(tmp_path / "work"),
        AWF_API_TOKEN="test-token-for-mcp-setup-tools",
    )


def _payload(result: CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


def _json_text(result: CallToolResult) -> str:
    return json.dumps(
        {
            "structured": result.structuredContent,
            "content": [getattr(item, "text", "") for item in result.content],
        },
        sort_keys=True,
        default=str,
    )
