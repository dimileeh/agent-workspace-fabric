"""Emit deprecation audit events when a deprecated agent runtime is selected."""

from __future__ import annotations

from typing import Any

from awf.db.enums import AgentRuntime
from awf.db.models import Workspace
from awf.db.repositories.workspace_repo import WorkspaceRepository

AGENT_RUNTIME_DEPRECATED_EVENT_TYPE = "workspace.agent_deprecated"
AGENT_RUNTIME_DEPRECATED_REASON = "AGENT_RUNTIME_DEPRECATED"
_DEPRECATED_AGENT_RUNTIMES = frozenset({AgentRuntime.gemini.value})


def is_deprecated_agent_runtime(agent: AgentRuntime | str | None) -> bool:
    """Return whether ``agent`` is a deprecated AWF coding-CLI runtime."""
    if agent is None:
        return False
    value = agent.value if isinstance(agent, AgentRuntime) else str(agent)
    return value in _DEPRECATED_AGENT_RUNTIMES


async def emit_agent_deprecated_event(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    agent: AgentRuntime | str,
    selection_path: str,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Append ``workspace.agent_deprecated`` when a deprecated agent is selected.

    No-op for non-deprecated agents. Strings only — no DB migration.
    """
    if not is_deprecated_agent_runtime(agent):
        return
    agent_value = agent.value if isinstance(agent, AgentRuntime) else str(agent)
    payload: dict[str, Any] = {
        "agent": agent_value,
        "selection_path": selection_path,
        "successor_agent": AgentRuntime.antigravity.value,
    }
    if extra_payload:
        payload.update(extra_payload)
    await repo.add_event(
        workspace,
        event_type=AGENT_RUNTIME_DEPRECATED_EVENT_TYPE,
        reason_code=AGENT_RUNTIME_DEPRECATED_REASON,
        payload=payload,
    )
