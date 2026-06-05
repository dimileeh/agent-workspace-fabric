"""Shared MCP tool result type contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from mcp.types import CallToolResult


class SafeResult(Protocol):
    """Protocol for constructing redacted MCP tool result objects."""

    def __call__(
        self,
        payload: dict[str, Any],
        *,
        is_error: bool = False,
        extra_secrets: Iterable[str] = (),
    ) -> CallToolResult:
        """Build a safe MCP tool result from a JSON payload."""
        ...
