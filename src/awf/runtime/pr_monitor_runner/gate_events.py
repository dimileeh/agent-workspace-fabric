"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from typing import Any

from awf.db.repositories import WorkspaceEventCreate
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import PRStatus
from awf.runtime.pr_monitor_runner.gates import (
    _NonCheckReviewerSettleDecision,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _datetime_iso,
    _supply_chain_policy_blocked_message,
)
from awf.runtime.pr_monitor_runner.logging import _log


async def _active_policy_block_message(self: Any, workspace_id: str) -> str | None:
    from awf.db.repositories import PolicyFindingRepository

    async with self._deps.session_factory() as s:
        active_findings = await PolicyFindingRepository(s).list_active_for_workspace(workspace_id)
    blocking_codes = [
        finding.reason_code
        for finding in active_findings
        if finding.severity == "blocking" and finding.reason_code.startswith("SUPPLY_CHAIN_")
    ]
    if not blocking_codes:
        return None
    return _supply_chain_policy_blocked_message(blocking_codes)


async def _record_merge_coordination_event(
    self: Any,
    event: str,
    *,
    monitor_log: WorkspaceLogSink | None,
    workspace_id: str,
    repo_url: str,
    base_branch: str,
    pr_number: int,
    status: PRStatus,
) -> None:
    payload = {
        "workspace_id": workspace_id,
        "repo_url": repo_url,
        "base_branch": base_branch,
        "pr_number": pr_number,
        "head_sha": status.head_sha[:10],
    }
    _log.info(event, **payload)
    await self._write_monitor_log(monitor_log, {"event": event, **payload})


async def _record_pre_merge_settle_event(
    self: Any,
    event: str,
    *,
    monitor_log: WorkspaceLogSink | None,
    workspace_id: str,
    repo_url: str,
    base_branch: str,
    pr_number: int,
    status: PRStatus,
    wait_seconds: float,
    elapsed_seconds: float | None = None,
) -> None:
    payload: dict[str, object] = {
        "workspace_id": workspace_id,
        "repo_url": repo_url,
        "base_branch": base_branch,
        "pr_number": pr_number,
        # Pre-merge settle events intentionally carry the full SHA,
        # unlike merge coordination events.
        "head_sha": status.head_sha,
        "wait_seconds": wait_seconds,
    }
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = elapsed_seconds
    _log.info(event, **payload)
    await self._write_monitor_log(monitor_log, {"event": event, **payload})


async def _record_non_check_reviewer_settle_decision(
    self: Any,
    *,
    decision: _NonCheckReviewerSettleDecision,
    workspace_id: str,
    pr_number: int,
    status: PRStatus,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    event_by_action = {
        "started": "monitor.non_check_reviewer_settle_started",
        "waiting": "monitor.non_check_reviewer_settle_waiting",
        "elapsed": "monitor.non_check_reviewer_settle_elapsed",
        "visible_check": "monitor.non_check_reviewer_settle_skipped_visible_check",
    }
    event = event_by_action.get(decision.action)
    if event is None:
        return

    payload: dict[str, object] = {
        "workspace_id": workspace_id,
        "pr_number": pr_number,
        "head_sha": status.head_sha,
        "wait_seconds": decision.wait_seconds,
        "settle_seconds": self._config.non_check_reviewer_settle_seconds,
        "poll_interval_seconds": self._config.poll_interval_seconds,
        "configured_reviewers": list(decision.configured_reviewers),
        "missing_reviewers": list(decision.missing_reviewers),
        "visible_reviewers": list(decision.visible_reviewers),
        "started_at": decision.started_at,
        "elapsed_seconds": decision.elapsed_seconds,
        "remaining_seconds": decision.remaining_seconds,
        "activity_anchor_at": _datetime_iso(decision.activity_anchor_at),
        "activity_anchor_source": decision.activity_anchor_source,
        "quiet_until": _datetime_iso(decision.quiet_until),
        "latest_external_review_activity_at": _datetime_iso(
            decision.latest_external_review_activity_at
        ),
        "latest_external_review_activity_source": (decision.latest_external_review_activity_source),
    }
    _log.info(event, **payload)
    await self._write_monitor_log(monitor_log, {"event": event, **payload})

    if decision.action == "waiting" or not decision.state_changed:
        return
    reason_by_action = {
        "started": "NON_CHECK_REVIEWER_SETTLE_STARTED",
        "elapsed": "NON_CHECK_REVIEWER_SETTLE_ELAPSED",
        "visible_check": "NON_CHECK_REVIEWER_VISIBLE_CHECK",
    }
    reason_code = reason_by_action[decision.action]
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type=event,
                reason_code=reason_code,
                payload={k: v for k, v in payload.items() if k != "workspace_id"},
            )
        ],
    )
