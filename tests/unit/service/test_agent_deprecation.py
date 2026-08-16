"""Deprecation events when selecting the retained-but-deprecated gemini agent."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.db.enums import AgentRuntime
from awf.service.agent_deprecation import (
    AGENT_RUNTIME_DEPRECATED_EVENT_TYPE,
    AGENT_RUNTIME_DEPRECATED_REASON,
    emit_agent_deprecated_event,
    is_deprecated_agent_runtime,
)


@pytest.mark.unit
def test_gemini_is_deprecated_antigravity_is_not() -> None:
    assert is_deprecated_agent_runtime(AgentRuntime.gemini) is True
    assert is_deprecated_agent_runtime("gemini") is True
    assert is_deprecated_agent_runtime(AgentRuntime.antigravity) is False
    assert is_deprecated_agent_runtime("antigravity") is False
    assert is_deprecated_agent_runtime(None) is False


@pytest.mark.unit
async def test_emit_agent_deprecated_event_for_gemini_selection() -> None:
    repo = SimpleNamespace(add_event=AsyncMock())
    workspace = SimpleNamespace(id="ws_deprecated")

    await emit_agent_deprecated_event(
        repo,  # type: ignore[arg-type]
        workspace,  # type: ignore[arg-type]
        agent=AgentRuntime.gemini,
        selection_path="workspace_create",
    )

    repo.add_event.assert_awaited_once()
    kwargs = repo.add_event.await_args.kwargs
    assert kwargs["event_type"] == AGENT_RUNTIME_DEPRECATED_EVENT_TYPE
    assert kwargs["reason_code"] == AGENT_RUNTIME_DEPRECATED_REASON
    assert kwargs["payload"]["agent"] == "gemini"
    assert kwargs["payload"]["selection_path"] == "workspace_create"
    assert kwargs["payload"]["successor_agent"] == "antigravity"


@pytest.mark.unit
async def test_emit_agent_deprecated_event_noop_for_antigravity() -> None:
    repo = SimpleNamespace(add_event=AsyncMock())
    workspace = SimpleNamespace(id="ws_ok")

    await emit_agent_deprecated_event(
        repo,  # type: ignore[arg-type]
        workspace,  # type: ignore[arg-type]
        agent=AgentRuntime.antigravity,
        selection_path="workspace_create",
    )

    repo.add_event.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    "selection_path",
    ["workspace_create", "adopt_pr", "provider_recovery_fallback"],
)
async def test_deprecated_selection_paths_share_event_contract(selection_path: str) -> None:
    """All three selection paths emit the same event-type/reason strings."""
    repo = SimpleNamespace(add_event=AsyncMock())
    workspace = SimpleNamespace(id="ws_path")
    extra: dict[str, Any] = (
        {"source_workspace_id": "ws_src"} if "recovery" in selection_path else {}
    )

    await emit_agent_deprecated_event(
        repo,  # type: ignore[arg-type]
        workspace,  # type: ignore[arg-type]
        agent="gemini",
        selection_path=selection_path,
        extra_payload=extra or None,
    )

    kwargs = repo.add_event.await_args.kwargs
    assert kwargs["event_type"] == "workspace.agent_deprecated"
    assert kwargs["reason_code"] == "AGENT_RUNTIME_DEPRECATED"
    assert kwargs["payload"]["selection_path"] == selection_path
