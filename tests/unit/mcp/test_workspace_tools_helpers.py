"""Small helper coverage for workspace MCP tools."""

from __future__ import annotations

import pytest

from awf.mcp import workspace_tools
from awf.service.config import Settings


@pytest.mark.unit
def test_resolve_settings_prefers_explicit_settings() -> None:
    settings = Settings()

    assert workspace_tools._resolve_settings(settings) is settings  # noqa: SLF001
