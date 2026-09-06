"""Timeout-preserving ``AgentRunError`` handling for the verdict protocol (#932).

Every ``AgentRunError`` used to roll the item back to ``item_start_head`` before
raising ``AgentVerdictExecutionError``. For a *provider* failure that is right:
whatever the agent left behind is unaccepted residue. For a **timeout** it is
destructive — the watchdog can fire on a healthy run that already committed a
real fix, and ws_84fddb4a98c94f7b8d6aa0d3 (PR #922) lost an hour of work exactly
that way.

``AGENT_IDLE_TIMEOUT`` / ``AGENT_TIMEOUT`` therefore take a preserve path:

1. Sink uncommitted item-scoped edits through the existing dirty-worktree sink.
2. Keep the item's commits — no rollback, ever.
3. Remember the *original* ``item_start_head`` for the item so the re-attempt's
   FIXED evidence range still starts where the item started, and the preserved
   commits count as this item's own work under the #925/#928/#931 rules. That
   restored anchor is for evidence only: the re-attempt's rollback floor stays at
   the preserved HEAD, so a later bad verdict cannot undo the preservation (#934).
4. Raise ``AgentVerdictExecutionError`` carrying the preserved HEAD, which the
   callers record as ``agent_failed`` — already a re-queueing outcome.

Kept in a sibling module so ``comment_verdict`` stays under the line budget;
re-exported from there (``X as X``) so monkeypatch seams keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT
from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
    read_protocol_attempt_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _rollback_or_classify_failure,
)
from awf.runtime.pr_monitor_runner.constants import _TASK_TAG_UNSET, _TaskTagUnset
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

AGENT_TIMEOUT_REASON_CODES = frozenset({AGENT_TIMEOUT, AGENT_IDLE_TIMEOUT})
"""Reason codes that mean "the watchdog fired", not "the agent's work is junk"."""

_ITEM_START_HEAD_STATE_KEY_PREFIX = "__awf_item_start_head__:"

# Infrastructure exits the dirty-worktree sink already declares. They are logged
# and swallowed here: the preserved commits must survive a sink failure, and the
# timeout reason code must still reach the caller.
_SINK_INFRASTRUCTURE_ERRORS = (
    ProviderRecoveryRetryError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryAuthError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
    ProtectedScopeDiffError,
)


def item_start_head_state_key(item_id: str) -> str:
    """Reserved ``MonitorState.threads_addressed_ids`` key for an item's start HEAD."""
    return f"{_ITEM_START_HEAD_STATE_KEY_PREFIX}{item_id}"


def remember_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
) -> None:
    """Persist the item's original start HEAD for the next attempt."""
    if state is None or not item_id or not head:
        return
    state.mark_addressed(item_start_head_state_key(item_id), head)


def consume_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read *and clear* the item's remembered start HEAD.

    Consuming on read is what keeps the marker from outliving the retry it was
    written for: once this attempt produces a verdict the item is finished, and no
    stale anchor can survive into an unrelated later pass over the same item id.
    An attempt that ends *without* a verdict is still owed its anchor, so it is
    re-armed by ``restore_item_start_head`` on the way out (#934 audit).
    """
    if state is None or not item_id:
        return None
    return state.threads_addressed_ids.pop(item_start_head_state_key(item_id), None)


def peek_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read the item's remembered start HEAD without clearing it."""
    if state is None or not item_id:
        return None
    return state.threads_addressed_ids.get(item_start_head_state_key(item_id))


def restore_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
) -> None:
    """Re-arm an anchor consumed by an attempt that died before a verdict.

    ``consume_item_start_head`` runs at the top of the item, before the fallible
    pre-launch ownership/mirror repair, the provider-recovery gate and the agent
    run. Every failure exit from there aborts the fix cycle without marking the
    item addressed, so the item is attempted again — and without the marker that
    attempt would anchor at the *preserved* HEAD and push the timed-out attempt's
    commits out of its own ``FIXED`` evidence range (#934 audit). Consume-on-read
    still holds for a returned verdict: the item is finished, and no stale anchor
    survives into an unrelated later pass. A marker written since — a fresh
    timeout on this very attempt — is newer and wins.
    """
    if state is None or not item_id or not head:
        return
    key = item_start_head_state_key(item_id)
    if key in state.threads_addressed_ids:
        return
    state.mark_addressed(key, head)


async def handle_agent_run_error(
    runner: PullRequestMonitorRunner,
    *,
    exc: AgentRunError,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    item_id: str | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    command_evidence: list[str],
    commit_dirty_changes: bool,
    rev_parse_head: Any,
) -> NoReturn:
    """Classify an ``AgentRunError``: roll back a provider failure, preserve a timeout.

    ``item_start_head`` is the item's evidence anchor (restored to the original
    start on a re-attempt after a preserved timeout) and stays the sink anchor and
    the value remembered for the next attempt. ``rollback_floor_head`` is the
    commit a rollback may rewind to — this attempt's own start — so a provider
    failure on that re-attempt cannot delete the preserved commits (#934).
    """
    from awf.runtime.pr_monitor_runner.comment_verdict import (
        AGENT_VERDICT_PROTOCOL_VIOLATION,
        AgentVerdictExecutionError,
        AgentVerdictProtocolError,
    )

    if exc.reason_code not in AGENT_TIMEOUT_REASON_CODES:
        rollback_ok = await _rollback_or_classify_failure(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_provider_failure_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                rollback_floor_head=rollback_floor_head,
                reason_code=exc.reason_code,
            )
            raise AgentVerdictProtocolError(
                reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                message="Could not roll back unaccepted edits after provider failure.",
            ) from exc
        await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
        raise AgentVerdictExecutionError(reason_code=exc.reason_code) from exc

    dirty_changes_committed = False
    if commit_dirty_changes:
        try:
            dirty_changes_committed = await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"{commit_message} (preserved after agent timeout)",
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                command_evidence=command_evidence,
                task_tag=task_tag,
                operation_start_head=item_start_head,
            )
        except _SINK_INFRASTRUCTURE_ERRORS as sink_exc:
            # The sink failing does not license a rollback: commits the agent
            # already made stay, and the timeout reason code still flows out.
            _log.warning(
                "monitor.agent_verdict_timeout_dirty_sink_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                item_start_head=item_start_head,
                exc_type=type(sink_exc).__name__,
                sink_reason_code=getattr(sink_exc, "reason_code", None),
            )

    preserved_head = await _preserved_head_sha(
        runner,
        worktree_path=worktree_path,
        rev_parse_head=rev_parse_head,
        fallback=item_start_head,
    )
    remember_item_start_head(state, item_id, item_start_head)
    _log.warning(
        "monitor.agent_verdict_timeout_work_preserved",
        workspace_id=workspace_id,
        reason_code=exc.reason_code,
        item_start_head=item_start_head,
        preserved_head=preserved_head,
        dirty_changes_committed=dirty_changes_committed,
    )
    # Recorded after the work is preserved and the marker is written, so a
    # provider-recovery escalation (retry / fallback / auth) still finds both in
    # place. Those escalations deliberately propagate: they short-circuit the
    # monitor cycle so the next pass runs on the fallback provider, and — unlike
    # the pre-#932 handler — nothing between here and the monitor loop rolls the
    # worktree back, so propagating them no longer costs the agent its commits.
    await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
    raise AgentVerdictExecutionError(
        reason_code=exc.reason_code,
        reason=_preserved_work_reason(
            reason_code=exc.reason_code,
            preserved_head=preserved_head,
            item_start_head=item_start_head,
        ),
        preserved_head_sha=preserved_head,
    ) from exc


def _preserved_work_reason(
    *,
    reason_code: str,
    preserved_head: str | None,
    item_start_head: str | None,
) -> str:
    if preserved_head is None:
        return f"agent timed out ({reason_code}); no commit could be read to preserve"
    resume = (
        f" — retrying from the original item start {item_start_head}"
        if item_start_head
        else " — retrying from that state"
    )
    return f"agent timed out ({reason_code}); preserved work at {preserved_head}{resume}"


async def _preserved_head_sha(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    rev_parse_head: Any,
    fallback: str | None,
) -> str | None:
    """Read the HEAD the timeout is leaving behind, falling back to the item start."""
    if not worktree_path.exists():
        return fallback
    try:
        head = await read_protocol_attempt_start_head(
            runner,
            worktree_path=worktree_path,
            rev_parse_head=rev_parse_head if callable(rev_parse_head) else None,
        )
    except (TimeoutError, OSError, RuntimeError) as probe_exc:
        _log.warning(
            "monitor.agent_verdict_timeout_preserved_head_probe_failed",
            worktree_path=str(worktree_path),
            exc_type=type(probe_exc).__name__,
        )
        return fallback
    return head or fallback
