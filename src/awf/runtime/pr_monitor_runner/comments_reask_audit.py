"""Audit persistence for NEEDS_HUMAN clarification re-asks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.common.audit import redact_audit_text
from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _NEEDS_HUMAN_REASON_MISSING,
)

if TYPE_CHECKING:
    from awf.runtime.logs import WorkspaceLogSink
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


async def _record_needs_human_reason_missing(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    pr_number: int,
    item_id: str,
    item_kind: str,
    item_author: str | None,
    item_path: str | None,
    item_line: int | None,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
    reason_code: str = _NEEDS_HUMAN_REASON_MISSING,
) -> None:
    """Warn and persist the reason-clarification diagnostic."""
    evidence = {
        "item_id": redact_audit_text(item_id, limit=200),
        "item_kind": item_kind,
        "item_author": redact_audit_text(item_author or "", limit=200),
        "item_path": redact_audit_text(item_path or "", limit=400),
        "item_line": item_line,
    }
    _log.warning(
        "monitor.needs_human_reason_missing",
        workspace_id=workspace_id,
        pr_number=pr_number,
        reason_code=reason_code,
        operation_id=operation_id,
        **evidence,
    )
    await runner._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=f"address_{item_kind}",
        outcome="needs_human",
        reason_code=reason_code,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        evidence=evidence,
    )
