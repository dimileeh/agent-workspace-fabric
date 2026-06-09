"""Workspace-status value helpers for worker recovery queries."""

from __future__ import annotations

from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    _ACTIVE_EXECUTION_STATUSES,
)
from awf.db.enums import WorkspaceStatus


def _salvage_workspace_status_values(
    workspace_status: WorkspaceStatus,
    *,
    event_type: str,
    reason_code: str,
) -> tuple[str, ...]:
    if (
        workspace_status in _ACTIVE_EXECUTION_STATUSES
        and event_type == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE
        and reason_code == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE
    ):
        return tuple(status.value for status in _ACTIVE_EXECUTION_STATUSES)
    return (workspace_status.value,)


def _preserved_active_execution_status_values(
    workspace_status: WorkspaceStatus,
    *,
    match_active_execution_statuses: bool = False,
) -> tuple[str, ...]:
    if match_active_execution_statuses and workspace_status in _ACTIVE_EXECUTION_STATUSES:
        return tuple(status.value for status in _ACTIVE_EXECUTION_STATUSES)
    return (workspace_status.value,)
