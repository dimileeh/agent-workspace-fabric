"""Workspace-status helpers for executor callbacks."""

from __future__ import annotations

from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import WorkspaceStatus


def _is_callback_terminal_status(status: str) -> bool:
    try:
        workspace_status = WorkspaceStatus(status)
    except ValueError:  # pragma: no cover - defensive for legacy bad rows
        return False
    return WorkspaceStateMachine.is_callback_terminal(workspace_status)
