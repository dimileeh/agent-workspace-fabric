"""Detect a forge that accepted a push but never synchronized the PR head (#1167).

GitHub can accept a push to the PR head branch yet keep the pull request on the
old ``headRefOid`` (and, after a sync-base push, ``DIRTY``). Every PR field then
describes a stale head, so the monitor must not act on it: a no-op SyncBase would
count as no progress and abort with ``merge_conflict_not_reproduced``.

The runner gathers the evidence here — local HEAD plus ``git ls-remote`` of the
push remote — and stamps it on ``PRStatus`` so the pure ``decide()`` can wait
within a bounded grace window and then park for a human. AWF never closes or
reopens the PR and never pushes extra commits to nudge it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.commands import CommandResult
from awf.db.repositories import WorkspaceEventCreate
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import MonitorState, PRStatus
from awf.runtime.pr_monitor_actions import PR_HEAD_NOT_SYNCHRONIZED_REASON, PR_HEAD_SYNC_REMEDY
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.logging import _log

PR_HEAD_NOT_SYNCHRONIZED_EVENT = "monitor.pr_head_not_synchronized"
_KEY_PREFIX = "__awf_pr_head_sync_"
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_LS_REMOTE_TIMEOUT_SECONDS = 60.0


def pr_head_sync_first_seen_key(remote_tip: str) -> str:
    """Reserved state key: first poll time (epoch string) a stall showed ``remote_tip``."""
    return f"{_KEY_PREFIX}first_seen:{remote_tip}"


def pr_head_sync_parked_key(remote_tip: str) -> str:
    """Reserved state key: the park event for ``remote_tip`` was already recorded."""
    return f"{_KEY_PREFIX}parked:{remote_tip}"


async def _clear_markers(runner: Any, workspace_id: str, state: MonitorState) -> None:
    """Drop every reserved PR-head-sync marker, persisting state only if one was removed."""
    keys = [key for key in state.threads_addressed_ids if key.startswith(_KEY_PREFIX)]
    for key in keys:
        state.threads_addressed_ids.pop(key, None)
    if keys:
        await runner._persist_state(workspace_id, state)


def _observed_stall_tip(state: MonitorState) -> str | None:
    """Remote tip of a stall an earlier poll observed (its first-seen marker)."""
    prefix = f"{_KEY_PREFIX}first_seen:"
    tips = [key[len(prefix) :] for key in state.threads_addressed_ids if key.startswith(prefix)]
    return tips[-1] if tips else None


def _accepted_push_tip(state: MonitorState, local_head: str | None) -> str | None:
    """AWF's last accepted push, when the local HEAD (if known) still matches it."""
    last_push = (state.last_push_sha or "").lower()
    if last_push and local_head in (None, last_push):
        return last_push
    return None


def _is_suspicious(status: PRStatus, state: MonitorState) -> bool:
    """Whether this poll could be a stall, so ordinary polls run no git commands.

    Either AWF's last push differs from the PR head, or a SyncBase was a no-op
    (a re-adoption that resynced onto an already-pushed commit).
    """
    last_push = (state.last_push_sha or "").lower()
    if last_push and last_push != status.head_sha.lower():
        return True
    return state.sync_base_no_progress_count > 0


async def _remote_branch_tip(
    runner: Any,
    *,
    workspace_id: str,
    remote_branch: str,
    remote_push_url: str | None,
) -> str | None:
    """Resolve the head-branch tip on the push remote, or ``None`` (logged)."""
    ref = f"refs/heads/{remote_branch}"
    result: CommandResult = await runner._deps.runner.run(
        git_worktree_command(
            runner._worktrees_root / workspace_id,
            "ls-remote",
            remote_push_url or "origin",
            ref,
        ),
        env=git_env_without_object_lookup_overrides(),
        timeout_seconds=_LS_REMOTE_TIMEOUT_SECONDS,
    )
    if result.ok:
        for line in result.stdout.splitlines():
            sha, _, line_ref = line.strip().partition("\t")
            if line_ref == ref and _SHA_RE.fullmatch(sha.lower()):
                return sha.lower()
    _log.warning(
        "monitor.pr_head_sync_probe_failed",
        workspace_id=workspace_id,
        probe="ls_remote",
        remote_branch=remote_branch,
        returncode=result.returncode,
        stderr=redact_audit_text(result.stderr or "", limit=400),
    )
    return None


async def observe_pr_head_sync(
    runner: Any,
    *,
    workspace_id: str,
    status: PRStatus,
    state: MonitorState,
    remote_branch: str,
    remote_push_url: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> PRStatus:
    """Stamp ``status`` when the forge has not synchronized the PR head.

    The stall holds when the push remote's head-branch tip equals the local HEAD
    (the commit AWF pushed or resynced to) but differs from the PR's reported
    head. A per-tip first-seen marker drives the grace window (persisted, because
    ``run()`` reloads state each poll); the first expired observation records one
    ``monitor.pr_head_not_synchronized`` event. Markers clear once the condition
    no longer holds. A probe failure stamps the stall an earlier poll observed,
    else AWF's accepted push; with neither, ``status`` is unchanged.
    """
    terminal = status.merged or status.closed or status.base_ref_drifted
    if terminal or not _is_suspicious(status, state):
        await _clear_markers(runner, workspace_id, state)
        return status
    pr_head = status.head_sha.lower()
    local_head = await runner._rev_parse_head(runner._worktrees_root / workspace_id)
    remote_tip: str | None
    if local_head is None:
        _log.warning(
            "monitor.pr_head_sync_probe_failed",
            workspace_id=workspace_id,
            probe="rev_parse_head",
        )
        remote_tip = None
    else:
        local_head = local_head.strip().lower()
        if local_head == pr_head:
            await _clear_markers(runner, workspace_id, state)
            return status
        remote_tip = await _remote_branch_tip(
            runner,
            workspace_id=workspace_id,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
        )
        if remote_tip is not None and remote_tip != local_head:
            await _clear_markers(runner, workspace_id, state)
            return status
    if remote_tip is None:
        # Fail closed during a stall an earlier poll proved, or after an accepted
        # push the PR head does not show: falling back to the stale snapshot
        # would run no-op SyncBases whose no-progress count can still reach
        # ``merge_conflict_not_reproduced`` across transient failures.
        remote_tip = _observed_stall_tip(state) or _accepted_push_tip(state, local_head)
        if remote_tip is None or remote_tip == pr_head:
            return status

    grace_seconds = runner._config.pr_head_sync_grace_seconds
    now = runner._deps.now()
    now_ts = (now if now.tzinfo else now.replace(tzinfo=UTC)).timestamp()
    first_seen_key = pr_head_sync_first_seen_key(remote_tip)
    state_changed = False
    try:
        first_seen = float(state.threads_addressed_ids[first_seen_key])
    except (KeyError, TypeError, ValueError):
        first_seen = now_ts
        state.mark_addressed(first_seen_key, f"{now_ts:.6f}")
        state_changed = True
    grace_active = grace_seconds > 0 and now_ts - first_seen < grace_seconds
    parked_key = pr_head_sync_parked_key(remote_tip)
    if not grace_active and parked_key not in state.threads_addressed_ids:
        state.mark_addressed(parked_key, "1")
        state_changed = True
        payload: dict[str, object] = {
            "pr_number": status.number,
            "pr_head_sha": status.head_sha,
            "remote_branch": remote_branch,
            "remote_branch_head_sha": remote_tip,
            "last_push_sha": state.last_push_sha,
            "first_seen_at": datetime.fromtimestamp(first_seen, UTC).isoformat(),
            "grace_seconds": grace_seconds,
            "remedy": PR_HEAD_SYNC_REMEDY,
        }
        _log.warning(PR_HEAD_NOT_SYNCHRONIZED_EVENT, workspace_id=workspace_id, **payload)
        await runner._write_monitor_log(
            monitor_log,
            {"event": PR_HEAD_NOT_SYNCHRONIZED_EVENT, "workspace_id": workspace_id, **payload},
        )
        await runner._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type=PR_HEAD_NOT_SYNCHRONIZED_EVENT,
                    reason_code=PR_HEAD_NOT_SYNCHRONIZED_REASON,
                    payload=payload,
                )
            ],
        )
    if state_changed:
        await runner._persist_state(workspace_id, state)
    return replace(
        status,
        unsynchronized_remote_head_sha=remote_tip,
        pr_head_sync_grace_active=grace_active,
    )
