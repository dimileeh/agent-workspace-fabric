"""Recovery for interrupted, unpublished PR comment repairs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.db.repositories import OperationRepository, WorkspaceEventCreate, WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    linked_worktree_git_dir,
    linked_worktree_path_from_git_dir,
    mirror_path_for_worktree,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.constants import (
    _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
    _COMMENT_REPAIR_ROLLBACK_FAILED,
    _COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_pinned_worktree_command,
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _git_env_for_merge_safety_object_lookup,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

_COMMENT_REPAIR_UNPUBLISHED_ABANDONED = "COMMENT_REPAIR_UNPUBLISHED_ABANDONED"
_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED = "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
# Durable retry marker when ``monitor.comment_repair_unpublished_abandoned`` could
# not be appended after a verified reset. Value is JSON event payload.
_UNPUBLISHED_ABANDON_EVENT_PENDING_KEY = "__awf_unpublished_abandon_event_pending__"

_OPERATOR_HINT_REPAIR_ACTION = "operator_hint_repair"
_NON_COMMENT_REPAIR_UNPUBLISHED_TYPES = frozenset(
    {
        OperationType.ci_repair.value,
        OperationType.sync_base.value,
    }
)
_RECOVERY_RESET_GIT_TIMEOUT_SECONDS = 30.0

_UNPUBLISHED_REPAIR_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.pending.value,
        OperationStatus.running.value,
        OperationStatus.failed.value,
        OperationStatus.cancelled.value,
    }
)


@dataclass(frozen=True)
class _RecoveryResetOutcome:
    ready: bool
    live_head: str | None
    worktree_dirty: bool
    reset_ok: bool
    reset_stderr: str = ""
    writer_lock_failed: bool = False


def _operation_payload_source_head_sha(operation: Operation) -> str | None:
    payload = operation.payload
    if not isinstance(payload, dict):
        return None
    source_head = payload.get("source_head_sha")
    if isinstance(source_head, str) and source_head.strip():
        return source_head.strip()
    return None


def _operation_payload_action(operation: Operation) -> str | None:
    payload = operation.payload
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if isinstance(action, str) and action.strip():
        return action.strip()
    return None


def _is_operator_hint_repair_operation(operation: Operation) -> bool:
    return (
        operation.type == OperationType.comment_repair.value
        and _operation_payload_action(operation) == _OPERATOR_HINT_REPAIR_ACTION
    )


def _operation_result_was_pushed(operation: Operation) -> bool:
    if operation.status != OperationStatus.succeeded.value:
        return False
    result = operation.result
    if not isinstance(result, dict):
        return False
    if result.get("pushed"):
        return True
    outcome = result.get("outcome")
    return isinstance(outcome, str) and outcome.endswith("_pushed")


def _operation_mapping_head_sha(
    mapping: Mapping[str, Any] | None,
    keys: tuple[str, ...],
) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _operation_recorded_local_terminal_head(operation: Operation) -> str | None:
    result = operation.result
    if isinstance(result, dict):
        terminal = _operation_mapping_head_sha(
            result,
            ("local_terminal_head_sha", "terminal_head_sha"),
        )
        if terminal is not None:
            return terminal
        recovery = result.get("agent_service_recovery")
        if isinstance(recovery, dict):
            terminal = _operation_mapping_head_sha(
                recovery,
                ("terminal_head_sha", "local_terminal_head_sha"),
            )
            if terminal is not None:
                return terminal
        evidence = result.get("failure_evidence")
        if isinstance(evidence, dict):
            terminal = _operation_mapping_head_sha(
                evidence,
                ("local_terminal_head_sha", "terminal_head_sha", "head_sha"),
            )
            if terminal is not None:
                return terminal
    payload = operation.payload
    if isinstance(payload, dict):
        return _operation_mapping_head_sha(
            payload,
            ("local_terminal_head_sha", "terminal_head_sha"),
        )
    return None


def _operation_owns_discarded_commits(
    operation: Operation,
    *,
    remote_pr_head: str,
    discarded_local_head: str,
) -> bool:
    """Return whether the operation produced the unpushed commits being abandoned."""
    source_head = _operation_payload_source_head_sha(operation)
    discarded = discarded_local_head.strip()
    remote = remote_pr_head.strip()
    if not source_head or not discarded or not remote:
        return False
    if source_head.lower() != remote.lower():
        return False
    if discarded.lower() == source_head.lower():
        return False

    terminal_head = _operation_recorded_local_terminal_head(operation)
    if terminal_head is not None:
        if terminal_head.lower() == source_head.lower():
            return False
        return discarded.lower() == terminal_head.lower()

    return False


def _is_active_unpublished_repair_operation(operation: Operation) -> bool:
    if operation.status not in _UNPUBLISHED_REPAIR_OPERATION_STATUSES:
        return False
    return not _operation_result_was_pushed(operation)


async def _unpublished_comment_repair_has_operation_provenance(
    runner: Any,
    *,
    workspace_id: str,
    remote_pr_head: str,
    discarded_local_head: str,
    exclude_operation_id: str | None = None,
) -> bool:
    """Return whether a prior comment-repair operation owns unpushed local commits."""
    session_factory = getattr(runner._deps, "session_factory", None)
    if session_factory is None:
        return False
    async with session_factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.comment_repair.value,
            limit=100,
        )
    for operation in operations:
        if exclude_operation_id and operation.id == exclude_operation_id:
            continue
        if _is_operator_hint_repair_operation(operation):
            continue
        if not _is_active_unpublished_repair_operation(operation):
            continue
        if _operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote_pr_head,
            discarded_local_head=discarded_local_head,
        ):
            return True
    return False


async def _unpublished_non_comment_repair_has_operation_provenance(
    runner: Any,
    *,
    workspace_id: str,
    remote_pr_head: str,
    discarded_local_head: str,
) -> bool:
    """Return whether another repair path owns unpushed commits at the PR head."""
    session_factory = getattr(runner._deps, "session_factory", None)
    if session_factory is None:
        return False
    async with session_factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            limit=100,
        )
    for operation in operations:
        if (
            operation.type not in _NON_COMMENT_REPAIR_UNPUBLISHED_TYPES
            and not _is_operator_hint_repair_operation(operation)
        ):
            continue
        if not _is_active_unpublished_repair_operation(operation):
            continue
        if _operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote_pr_head,
            discarded_local_head=discarded_local_head,
        ):
            return True
    return False


def _verified_awf_comment_repair_worktree(
    *,
    runner: Any,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """Verify the exact AWF worktree and its reciprocal Git metadata link."""
    try:
        expected = (runner._worktrees_root / workspace_id).resolve()
        actual = worktree_path.resolve()
    except (OSError, RuntimeError):
        return False
    if actual != expected or actual.name != workspace_id:
        return False
    linked_git_dir = linked_worktree_git_dir(actual)
    mirror_path = mirror_path_for_worktree(actual)
    if linked_git_dir is None or mirror_path is None or not mirror_path.is_dir():
        return False
    try:
        registered_worktree = linked_worktree_path_from_git_dir(linked_git_dir)
    except GitOperationError:
        return False
    return registered_worktree == actual


async def _live_head_matches_pinned_recovery_head(
    runner: Any,
    *,
    worktree_path: Path,
    pinned_head: str,
    git_env: Mapping[str, str],
    git_dir: Path | None = None,
    timeout_seconds: float = _RECOVERY_RESET_GIT_TIMEOUT_SECONDS,
) -> tuple[bool, str | None]:
    """Verify live HEAD still matches the snapshot that passed recovery checks.

    Always bounds the Git subprocess: after config restore, a surviving agent can
    rewrite live include.path to a readerless FIFO between an earlier bounded HEAD
    probe and this recheck (PRRT_kwDOSJAM6s6fG5gp).
    """
    command = (
        git_pinned_worktree_command(git_dir, worktree_path, "rev-parse", "HEAD")
        if git_dir is not None
        else git_worktree_command(worktree_path, "rev-parse", "HEAD")
    )
    live_result = await runner.run(
        command,
        env=git_env,
        timeout_seconds=timeout_seconds,
    )
    live_head = live_result.stdout.strip()
    if not live_result.ok or not live_head:
        return False, live_head or None
    if live_head.lower() != pinned_head.lower():
        return False, live_head
    return True, live_head


async def _run_recovery_hard_reset_under_writer_lock(
    runner: Any,
    *,
    worktree_path: Path,
    pinned_head: str,
    reset_target: str,
    git_env: Mapping[str, str],
) -> _RecoveryResetOutcome:
    """Hold the worktree writer lock while verifying and running ``reset --hard``.

    Serializes the cleanliness gate with the destructive reset so a surviving agent
    or operator cannot land tracked edits after the readiness check but before the
    reset discards them. Git subprocesses run through the cancellation-safe runner
    so monitor cancellation cannot orphan a worker that keeps the writer lock.
    """
    env = dict(git_env)
    timeout_seconds = _RECOVERY_RESET_GIT_TIMEOUT_SECONDS
    try:
        async with hold_exclusive_worktree_writer_lock(worktree_path):
            head_result = await runner.run(
                git_worktree_command(worktree_path, "rev-parse", "HEAD"),
                env=env,
                timeout_seconds=timeout_seconds,
            )
            live_head = head_result.stdout.strip()
            if not head_result.ok or not live_head:
                return _RecoveryResetOutcome(
                    ready=False,
                    live_head=live_head or None,
                    worktree_dirty=False,
                    reset_ok=False,
                )
            if live_head.lower() != pinned_head.lower():
                return _RecoveryResetOutcome(
                    ready=False,
                    live_head=live_head,
                    worktree_dirty=False,
                    reset_ok=False,
                )
            clean_result = await runner.run(
                git_worktree_command(worktree_path, "status", "--porcelain", "-z"),
                env=env,
                timeout_seconds=timeout_seconds,
            )
            if not clean_result.ok:
                return _RecoveryResetOutcome(
                    ready=False,
                    live_head=live_head,
                    worktree_dirty=False,
                    reset_ok=False,
                )
            if clean_result.stdout:
                return _RecoveryResetOutcome(
                    ready=False,
                    live_head=live_head,
                    worktree_dirty=True,
                    reset_ok=False,
                )
            reset_result = await runner.run(
                git_worktree_command(worktree_path, "reset", "--hard", reset_target),
                env=env,
                timeout_seconds=timeout_seconds,
            )
            if not reset_result.ok:
                return _RecoveryResetOutcome(
                    ready=True,
                    live_head=live_head,
                    worktree_dirty=False,
                    reset_ok=False,
                    reset_stderr=(reset_result.stderr or "")[:400],
                )
            return _RecoveryResetOutcome(
                ready=True,
                live_head=live_head,
                worktree_dirty=False,
                reset_ok=True,
            )
    except OSError as exc:
        return _RecoveryResetOutcome(
            ready=False,
            live_head=None,
            worktree_dirty=False,
            reset_ok=False,
            reset_stderr=str(exc)[:400],
            writer_lock_failed=True,
        )


async def _live_worktree_ready_for_recovery_reset(
    runner: Any,
    *,
    worktree_path: Path,
    pinned_head: str,
    git_env: Mapping[str, str],
) -> tuple[bool, str | None, bool]:
    """Verify live HEAD and cleanliness immediately before ``reset --hard``.

    Uncommitted tracked edits do not move HEAD, so a live-HEAD-only gate cannot
    detect writers that land between the pre-existing-dirty guard and recovery.
    """
    head_unchanged, live_head = await _live_head_matches_pinned_recovery_head(
        runner,
        worktree_path=worktree_path,
        pinned_head=pinned_head,
        git_env=git_env,
    )
    if not head_unchanged:
        return False, live_head, False
    clean_result = await runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "-z"),
        env=git_env,
    )
    worktree_dirty = not clean_result.ok or bool(clean_result.stdout)
    if worktree_dirty:
        return False, live_head, True
    return True, live_head, False


def _reconcile_monitor_push_tracking_to_accepted_head(
    state: MonitorState,
    accepted_head: str,
) -> None:
    """Align push-tracking to a verified recovered worktree HEAD.

    Called after a successful hard-reset or fast-forward that moved the
    AWF-managed worktree to the fetched PR head, and on the verified HEAD
    equality short-circuit (worktree already at the accepted tip). Clears any
    hosted terminal advance marker so the next persist / hosted identity cannot
    advertise an abandoned unpublished SHA as ``expected_head_sha``.
    """
    state.last_push_sha = accepted_head
    state.hosted_terminal_head_advanced = False


def _stash_pending_unpublished_abandon_event(
    state: MonitorState,
    event_payload: Mapping[str, object],
) -> None:
    """Persist a retryable abandonment audit payload on monitor state."""
    state.mark_addressed(
        _UNPUBLISHED_ABANDON_EVENT_PENDING_KEY,
        json.dumps(dict(event_payload), separators=(",", ":"), sort_keys=True),
    )


def _clear_pending_unpublished_abandon_event(state: MonitorState) -> None:
    """Drop a successfully flushed abandonment audit retry marker."""
    state.threads_addressed_ids.pop(_UNPUBLISHED_ABANDON_EVENT_PENDING_KEY, None)
    changed = getattr(state, "_changed_thread_ids", None)
    if isinstance(changed, set):
        changed.discard(_UNPUBLISHED_ABANDON_EVENT_PENDING_KEY)


def _pending_unpublished_abandon_event_payload(
    state: MonitorState,
) -> dict[str, object] | None:
    raw = state.threads_addressed_ids.get(_UNPUBLISHED_ABANDON_EVENT_PENDING_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}


async def _append_unpublished_abandon_event(
    self: Any,
    *,
    workspace_id: str,
    event_payload: Mapping[str, object],
) -> None:
    """Append the operator-facing abandonment audit event (raises on failure)."""
    append_events = getattr(self, "_append_workspace_events", None)
    if not callable(append_events):
        return
    await append_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="monitor.comment_repair_unpublished_abandoned",
                reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
                payload=dict(event_payload),
            )
        ],
    )


async def _commit_unpublished_abandon_event_and_clear_pending(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    event_payload: Mapping[str, object],
) -> None:
    """Append the abandon audit and clear its durable retry marker together.

    ``_append_workspace_events`` commits in its own session. Clearing the pending
    marker only in memory leaves the DB marker until a later ``_persist_state``;
    a stop between those commits makes the equality/FF retry append the same
    operator-facing event again (PRRT_kwDOSJAM6s6dzTXI). When a session factory
    is available, append + marker removal share one transaction. Otherwise
    (unit stubs) append via the event sink, clear in memory, then durably
    ``_persist_state`` when present so the clear is not left to the outer loop.
    """
    session_factory = getattr(getattr(self, "_deps", None), "session_factory", None)
    if callable(session_factory):
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(workspace_id)
            if ws is None:
                _log.warning(
                    "monitor.comment_repair_unpublished_abandoned_event_dropped",
                    workspace_id=workspace_id,
                    reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
                    reason="workspace_row_missing",
                )
                _clear_pending_unpublished_abandon_event(state)
                return
            await repo.add_events(
                ws,
                events=[
                    WorkspaceEventCreate(
                        event_type="monitor.comment_repair_unpublished_abandoned",
                        reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
                        payload=dict(event_payload),
                    )
                ],
            )
            threads = dict(ws.monitor_threads_addressed or {})
            if _UNPUBLISHED_ABANDON_EVENT_PENDING_KEY in threads:
                threads.pop(_UNPUBLISHED_ABANDON_EVENT_PENDING_KEY, None)
                ws.monitor_threads_addressed = threads
            await session.commit()
        _clear_pending_unpublished_abandon_event(state)
        return

    await _append_unpublished_abandon_event(
        self,
        workspace_id=workspace_id,
        event_payload=event_payload,
    )
    _clear_pending_unpublished_abandon_event(state)
    persist = getattr(self, "_persist_state", None)
    if callable(persist):
        await persist(workspace_id, state)


async def _flush_pending_unpublished_abandon_event(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
) -> bool:
    """Retry a stashed abandonment audit event. True when none pending or flushed."""
    event_payload = _pending_unpublished_abandon_event_payload(state)
    if event_payload is None:
        if _UNPUBLISHED_ABANDON_EVENT_PENDING_KEY in state.threads_addressed_ids:
            # Corrupt marker — drop it so it cannot wedge the equality path forever.
            _clear_pending_unpublished_abandon_event(state)
            persist = getattr(self, "_persist_state", None)
            if callable(persist):
                await persist(workspace_id, state)
        return True
    try:
        await _commit_unpublished_abandon_event_and_clear_pending(
            self,
            workspace_id=workspace_id,
            state=state,
            event_payload=event_payload,
        )
    except (SQLAlchemyError, OSError) as exc:
        # DB/sink failures keep the durable marker for retry; programming errors propagate.
        _log.warning(
            "monitor.comment_repair_unpublished_abandoned_event_failed",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
            pending_retry=True,
            reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED,
        )
        return False
    _log.warning(
        "monitor.comment_repair_unpublished_abandoned",
        workspace_id=workspace_id,
        reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
        **event_payload,
    )
    return True


@dataclass(frozen=True)
class _EqualityReconcileOutcome:
    reconciled: bool
    live_head: str | None
    writer_lock_failed: bool = False
    lock_stderr: str = ""
    worktree_dirty: bool = False


async def _reconcile_push_tracking_under_live_equality_lock(
    runner: Any,
    *,
    worktree_path: Path,
    expected_head: str,
    state: MonitorState,
    git_env: Mapping[str, str],
    require_clean: bool = False,
) -> _EqualityReconcileOutcome:
    """Hold the writer lock, verify live HEAD still equals the accepted tip, then reconcile.

    The abandon equality short-circuit receives a start-of-operation HEAD snapshot.
    Another worktree writer can advance HEAD after that snapshot but before
    push-tracking mutation; reconciling from the stale argument alone would clear
    ``hosted_terminal_head_advanced`` and rewind ``last_push_sha`` against a live
    checkout that no longer matches. Match the reset/FF race contract: recheck
    under the same exclusive writer lock before changing monitor state.

    Recovery reset/FF paths release the writer lock when ``reset --hard`` returns.
    Post-reset verification and push-tracking reconcile must reacquire the lock and
    recheck HEAD (and cleanliness when ``require_clean``) before mutating state, or a
    concurrent writer can advance the checkout and recreate an invalid hosted
    expected-head identity.
    """
    env = dict(git_env)
    timeout_seconds = _RECOVERY_RESET_GIT_TIMEOUT_SECONDS
    try:
        async with hold_exclusive_worktree_writer_lock(worktree_path):
            head_result = await runner.run(
                git_worktree_command(worktree_path, "rev-parse", "HEAD"),
                env=env,
                timeout_seconds=timeout_seconds,
            )
            live_head = head_result.stdout.strip()
            if not head_result.ok or not live_head or live_head.lower() != expected_head.lower():
                return _EqualityReconcileOutcome(
                    reconciled=False,
                    live_head=live_head or None,
                )
            if require_clean:
                clean_result = await runner.run(
                    git_worktree_command(worktree_path, "status", "--porcelain", "-z"),
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
                if not clean_result.ok or bool(clean_result.stdout):
                    return _EqualityReconcileOutcome(
                        reconciled=False,
                        live_head=live_head,
                        worktree_dirty=bool(clean_result.ok and clean_result.stdout),
                    )
            _reconcile_monitor_push_tracking_to_accepted_head(state, live_head)
            return _EqualityReconcileOutcome(
                reconciled=True,
                live_head=live_head,
            )
    except OSError as exc:
        return _EqualityReconcileOutcome(
            reconciled=False,
            live_head=None,
            writer_lock_failed=True,
            lock_stderr=str(exc)[:400],
        )


async def _abandon_unpublished_comment_repairs(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    expected_remote_head: str,
    local_head: str,
    state: MonitorState,
    remote_push_url: str | None = None,
    current_operation_id: str | None = None,
) -> tuple[str, _GitPushResult | None]:
    """Reset interrupted, unpublished repair commits to the fetched PR head.

    This is intentionally provenance-only recovery: it never interprets prior
    agent stdout or commit contents. A preserved protected-scope transaction and
    a workflow-scope-blocked repair are excluded because their local commits are
    intentional operator-facing state awaiting a later push retry.
    """
    current_head = local_head.strip()
    if state.has_preserved_protected_block or state.awaiting_workflow_scope:
        return current_head, None

    # Hosted execution and unit seams can legitimately operate without a local
    # linked worktree. The ordinary start-HEAD guard remains authoritative for
    # those paths; rollback is only meaningful for a concrete AWF-linked
    # checkout with Git metadata to verify.
    if not worktree_path.exists() or not (worktree_path / ".git").exists():
        return current_head, None

    def failure(
        reason_code: str,
        message: str,
        **details: object,
    ) -> tuple[str, _GitPushResult]:
        return (
            current_head,
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=message,
                reason_code=reason_code,
                details={"phase": "comment_repair_recovery", "pushed": False, **details},
            ),
        )

    try:
        expected_worktree = (self._worktrees_root / workspace_id).resolve()
        actual_worktree = worktree_path.resolve()
    except (OSError, RuntimeError):
        expected_worktree = Path()
        actual_worktree = Path("invalid")
    if actual_worktree != expected_worktree or actual_worktree.name != workspace_id:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not verify the AWF-managed comment-repair worktree; refusing to reset it.",
        )
    expected_head = expected_remote_head.strip()
    if not current_head or not expected_head:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not verify the local and expected PR heads; refusing to reset.",
            local_head=current_head,
            expected_remote_head=expected_head,
        )
    if current_head.lower() == expected_head.lower():
        # Worktree snapshot already matches the accepted remote tip (including
        # the cycle after a reset that crashed before ``_persist_state``, or an
        # upgraded workspace whose DB still holds an orphaned hosted SHA).
        # Re-verify live HEAD under the writer lock before mutating
        # push-tracking; reset/ff paths alone never re-enter once equal.
        equality = await _reconcile_push_tracking_under_live_equality_lock(
            self._deps.runner,
            worktree_path=worktree_path,
            expected_head=expected_head,
            state=state,
            git_env=_git_env_for_merge_safety_object_lookup(),
        )
        if equality.reconciled:
            restored_head = equality.live_head or current_head
            if not await _flush_pending_unpublished_abandon_event(
                self,
                workspace_id=workspace_id,
                state=state,
            ):
                return (
                    restored_head,
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=(
                            "Could not persist the unpublished-repair abandonment audit "
                            "event after equality reconciliation; will retry."
                        ),
                        reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED,
                        details={
                            "phase": "comment_repair_recovery",
                            "pushed": False,
                            "local_head": restored_head,
                            "expected_remote_head": expected_head,
                        },
                    ),
                )
            return restored_head, None
        if equality.writer_lock_failed:
            return failure(
                _COMMENT_REPAIR_ROLLBACK_FAILED,
                "Could not acquire the worktree writer lock before equality reconciliation.",
                local_head=current_head,
                expected_remote_head=expected_head,
                reset_stderr=equality.lock_stderr,
            )
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Local comment-repair HEAD changed before equality reconciliation; "
            "refusing to mutate push-tracking.",
            local_head=current_head,
            live_head=equality.live_head,
            expected_remote_head=expected_head,
        )

    if not _verified_awf_comment_repair_worktree(
        runner=self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    ):
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not verify the AWF-managed comment-repair Git layout; refusing to reset it.",
        )
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="comment_repair_recovery",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        return failure(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            "Could not repair comment-repair worktree ownership before recovery.",
        )

    fetch = await self._remote_branch_fetch_once(
        worktree_path=worktree_path,
        remote=remote_push_url or "origin",
        remote_branch=remote_branch,
    )
    if not fetch.ok:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not fetch the remote PR branch; refusing to reset local repairs.",
            fetch_returncode=fetch.returncode,
            fetch_stderr=fetch.stderr[:400],
        )
    merge_safety_git_env = _git_env_for_merge_safety_object_lookup()
    fetched_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", "FETCH_HEAD"),
        env=merge_safety_git_env,
    )
    fetched_head = fetched_result.stdout.strip()
    if not fetched_result.ok or not fetched_head:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Fetched PR head did not match the monitor snapshot; refusing to reset.",
            local_head=current_head,
            expected_remote_head=expected_head,
            fetched_remote_head=fetched_head,
        )
    stale_snapshot_advance = False
    if fetched_head.lower() != expected_head.lower():
        # A successful monitor-owned push can advance both local and remote HEAD
        # before the next forge status snapshot refreshes. Accept only the exact
        # already-published local HEAD and prove it advances the stale snapshot;
        # every divergent or rewritten remote still fails closed below.
        if current_head.lower() == fetched_head.lower():
            published_descendant = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "merge-base",
                    "--is-ancestor",
                    expected_head,
                    fetched_head,
                ),
                env=merge_safety_git_env,
            )
            if published_descendant.ok:
                if not await _flush_pending_unpublished_abandon_event(
                    self,
                    workspace_id=workspace_id,
                    state=state,
                ):
                    return (
                        fetched_head,
                        _GitPushResult(
                            pushed=False,
                            failed=True,
                            returncode=1,
                            stderr=(
                                "Could not persist the unpublished-repair abandonment audit "
                                "event after published-head recovery; will retry."
                            ),
                            reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED,
                            details={
                                "phase": "comment_repair_recovery",
                                "pushed": False,
                                "local_head": fetched_head,
                                "fetched_remote_head": fetched_head,
                            },
                        ),
                    )
                return fetched_head, None
        stale_snapshot = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                expected_head,
                fetched_head,
            ),
            env=merge_safety_git_env,
        )
        if not stale_snapshot.ok:
            return failure(
                _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                "Fetched PR head did not match the monitor snapshot; refusing to reset.",
                local_head=current_head,
                expected_remote_head=expected_head,
                fetched_remote_head=fetched_head,
            )
        stale_snapshot_advance = True

    descendant = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            fetched_head,
            "HEAD",
        ),
        env=merge_safety_git_env,
    )
    use_stale_snapshot_diff = False
    if not descendant.ok:
        behind = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                "HEAD",
                fetched_head,
            ),
            env=merge_safety_git_env,
        )
        if behind.ok:
            recovery_reset = await _run_recovery_hard_reset_under_writer_lock(
                self._deps.runner,
                worktree_path=worktree_path,
                pinned_head=current_head,
                reset_target=fetched_head,
                git_env=merge_safety_git_env,
            )
            if not recovery_reset.ready:
                if recovery_reset.worktree_dirty:
                    return failure(
                        _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                        "Local comment-repair worktree became dirty before fast-forward recovery; "
                        "refusing to reset.",
                        local_head=current_head,
                        live_head=recovery_reset.live_head,
                        fetched_remote_head=fetched_head,
                    )
                if recovery_reset.writer_lock_failed:
                    return failure(
                        _COMMENT_REPAIR_ROLLBACK_FAILED,
                        "Could not acquire the worktree writer lock before fast-forward recovery.",
                        local_head=current_head,
                        fetched_remote_head=fetched_head,
                        reset_stderr=recovery_reset.reset_stderr,
                    )
                return failure(
                    _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                    "Local comment-repair HEAD changed before fast-forward recovery; refusing to reset.",
                    local_head=current_head,
                    live_head=recovery_reset.live_head,
                    fetched_remote_head=fetched_head,
                )
            if not recovery_reset.reset_ok:
                return failure(
                    _COMMENT_REPAIR_ROLLBACK_FAILED,
                    "Could not fast-forward a lagging comment-repair worktree to the remote PR head.",
                    local_head=current_head,
                    fetched_remote_head=fetched_head,
                    reset_stderr=recovery_reset.reset_stderr,
                )
            # Reset released the writer lock; recheck HEAD/clean under lock before
            # mutating push-tracking (same race as equality short-circuit).
            verified = await _reconcile_push_tracking_under_live_equality_lock(
                self._deps.runner,
                worktree_path=worktree_path,
                expected_head=fetched_head,
                state=state,
                git_env=merge_safety_git_env,
                require_clean=True,
            )
            if verified.reconciled:
                # Same pending-audit retry as equality: a prior abandon may have
                # reset + stashed the event, then the remote advanced so this
                # cycle never re-enters equality (PRRT_kwDOSJAM6s6dzTXE).
                if not await _flush_pending_unpublished_abandon_event(
                    self,
                    workspace_id=workspace_id,
                    state=state,
                ):
                    return (
                        fetched_head,
                        _GitPushResult(
                            pushed=False,
                            failed=True,
                            returncode=1,
                            stderr=(
                                "Could not persist the unpublished-repair abandonment audit "
                                "event after fast-forward recovery; will retry."
                            ),
                            reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED,
                            details={
                                "phase": "comment_repair_recovery",
                                "pushed": False,
                                "local_head": fetched_head,
                                "fetched_remote_head": fetched_head,
                            },
                        ),
                    )
                return fetched_head, None
            if verified.writer_lock_failed:
                return failure(
                    _COMMENT_REPAIR_ROLLBACK_FAILED,
                    "Could not acquire the worktree writer lock before fast-forward verification.",
                    local_head=current_head,
                    fetched_remote_head=fetched_head,
                    reset_stderr=verified.lock_stderr,
                )
            return failure(
                _COMMENT_REPAIR_ROLLBACK_FAILED,
                "Could not verify a lagging comment-repair worktree after fast-forward.",
                local_head=current_head,
                fetched_remote_head=fetched_head,
                verified_head=verified.live_head or "",
            )

        if stale_snapshot_advance:
            on_stale_snapshot_base = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "merge-base",
                    "--is-ancestor",
                    expected_head,
                    "HEAD",
                ),
                env=merge_safety_git_env,
            )
            if not on_stale_snapshot_base.ok:
                return failure(
                    _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                    "Local comment-repair HEAD is not a verified descendant of the remote PR head; "
                    "refusing to reset.",
                    local_head=current_head,
                    fetched_remote_head=fetched_head,
                )
            use_stale_snapshot_diff = True
        else:
            return failure(
                _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                "Local comment-repair HEAD is not a verified descendant of the remote PR head; "
                "refusing to reset.",
                local_head=current_head,
                fetched_remote_head=fetched_head,
            )

    diff_range = f"{expected_head}..HEAD" if use_stale_snapshot_diff else f"{fetched_head}..HEAD"
    delta_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "--name-status",
            "-z",
            diff_range,
        ),
        env=merge_safety_git_env,
    )
    if not delta_result.ok:
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Could not record the unpublished repair delta; refusing to reset.",
            local_head=current_head,
            fetched_remote_head=fetched_head,
            diff_stderr=delta_result.stderr[:400],
        )
    try:
        abandoned_paths = _changed_paths_from_name_status_z(delta_result.stdout)
    except ProtectedScopeDiffError as exc:
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Could not parse the unpublished repair delta; refusing to reset.",
            local_head=current_head,
            fetched_remote_head=fetched_head,
            diff_error=str(exc),
        )

    # Interrupted repairs record source_head_sha from the monitor snapshot at start.
    # When the live remote advanced past that stale snapshot, ownership must still
    # match against the snapshot head, not the freshly fetched PR head.
    provenance_remote_head = expected_head if use_stale_snapshot_diff else fetched_head
    has_comment_repair_provenance = await _unpublished_comment_repair_has_operation_provenance(
        self,
        workspace_id=workspace_id,
        remote_pr_head=provenance_remote_head,
        discarded_local_head=current_head,
        exclude_operation_id=current_operation_id,
    )
    has_conflicting_repair_provenance = (
        await _unpublished_non_comment_repair_has_operation_provenance(
            self,
            workspace_id=workspace_id,
            remote_pr_head=provenance_remote_head,
            discarded_local_head=current_head,
        )
    )
    if not has_comment_repair_provenance or has_conflicting_repair_provenance:
        _log.info(
            "monitor.comment_repair_unpublished_reset_skipped_missing_provenance",
            workspace_id=workspace_id,
            local_head=current_head,
            fetched_remote_head=fetched_head,
            has_comment_repair_provenance=has_comment_repair_provenance,
            has_conflicting_repair_provenance=has_conflicting_repair_provenance,
            current_operation_id=current_operation_id,
        )
        return failure(
            _COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING,
            "Local HEAD is ahead of the remote PR head without comment-repair provenance; refusing to reset or push.",
            local_head=current_head,
            fetched_remote_head=fetched_head,
            has_comment_repair_provenance=has_comment_repair_provenance,
            has_conflicting_repair_provenance=has_conflicting_repair_provenance,
            current_operation_id=current_operation_id,
        )

    recovery_reset = await _run_recovery_hard_reset_under_writer_lock(
        self._deps.runner,
        worktree_path=worktree_path,
        pinned_head=current_head,
        reset_target=fetched_head,
        git_env=merge_safety_git_env,
    )
    if not recovery_reset.ready:
        if recovery_reset.worktree_dirty:
            return failure(
                _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                "Local comment-repair worktree became dirty before unpublished-repair reset; "
                "refusing to reset.",
                local_head=current_head,
                live_head=recovery_reset.live_head,
                fetched_remote_head=fetched_head,
                abandoned_paths=list(abandoned_paths),
            )
        if recovery_reset.writer_lock_failed:
            return failure(
                _COMMENT_REPAIR_ROLLBACK_FAILED,
                "Could not acquire the worktree writer lock before unpublished-repair reset.",
                abandoned_local_head=current_head,
                fetched_remote_head=fetched_head,
                abandoned_paths=list(abandoned_paths),
                reset_stderr=recovery_reset.reset_stderr,
            )
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Local comment-repair HEAD changed before unpublished-repair reset; refusing to reset.",
            local_head=current_head,
            live_head=recovery_reset.live_head,
            fetched_remote_head=fetched_head,
            abandoned_paths=list(abandoned_paths),
        )
    if not recovery_reset.reset_ok:
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Could not reset interrupted comment repairs to the remote PR head.",
            abandoned_local_head=current_head,
            fetched_remote_head=fetched_head,
            abandoned_paths=list(abandoned_paths),
            reset_stderr=recovery_reset.reset_stderr,
        )
    # Reset released the writer lock; recheck HEAD/clean under lock before
    # mutating push-tracking so a concurrent writer cannot recreate an invalid
    # hosted expected-head identity (PRRT_kwDOSJAM6s6dzDv5).
    verified = await _reconcile_push_tracking_under_live_equality_lock(
        self._deps.runner,
        worktree_path=worktree_path,
        expected_head=fetched_head,
        state=state,
        git_env=merge_safety_git_env,
        require_clean=True,
    )
    if not verified.reconciled:
        if verified.writer_lock_failed:
            return failure(
                _COMMENT_REPAIR_ROLLBACK_FAILED,
                "Could not acquire the worktree writer lock before unpublished-repair verification.",
                abandoned_local_head=current_head,
                fetched_remote_head=fetched_head,
                abandoned_paths=list(abandoned_paths),
                reset_stderr=verified.lock_stderr,
            )
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Interrupted comment-repair rollback could not be verified clean.",
            abandoned_local_head=current_head,
            fetched_remote_head=fetched_head,
            verified_head=verified.live_head or "",
            abandoned_paths=list(abandoned_paths),
        )

    # Push-tracking already aligned under the writer lock above. Stage the
    # abandon audit retry marker *before* the cancellable event transaction:
    # ``asyncio.CancelledError`` is BaseException (not SQLAlchemyError/OSError),
    # so a cancel while append awaits would otherwise roll back with no durable
    # payload. On restart HEAD already equals remote and the equality path
    # would have nothing to flush (PRRT_kwDOSJAM6s6d0tKy). Successful append
    # clears the marker atomically with the event (PRRT_kwDOSJAM6s6dzTXI).
    event_payload = {
        "abandoned_local_head": current_head,
        "restored_remote_head": fetched_head,
        "abandoned_paths": list(abandoned_paths),
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    _stash_pending_unpublished_abandon_event(state, event_payload)
    # Durably flush the retry marker (and reconciled push-tracking) before the
    # cancellable window. An in-memory-only stash is lost if the process
    # crashes or cancel/finish-op raises before a later ``_persist_state``
    # (PRRT_kwDOSJAM6s6dy5TU).
    persist = getattr(self, "_persist_state", None)
    if callable(persist):
        await persist(workspace_id, state)
    try:
        await _commit_unpublished_abandon_event_and_clear_pending(
            self,
            workspace_id=workspace_id,
            state=state,
            event_payload=event_payload,
        )
    except (SQLAlchemyError, OSError) as exc:
        # Marker already staged; DB/sink failures fail closed for retry.
        # Programming errors (e.g. TypeError) must not become silent retries.
        _log.warning(
            "monitor.comment_repair_unpublished_abandoned_event_failed",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
            pending_retry=True,
            reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED,
        )
        return (
            fetched_head,
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=(
                    "Interrupted comment repairs were reset, but the abandonment "
                    "audit event could not be persisted; will retry."
                ),
                reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED,
                details={
                    "phase": "comment_repair_recovery",
                    "pushed": False,
                    **event_payload,
                },
            ),
        )
    _log.warning(
        "monitor.comment_repair_unpublished_abandoned",
        workspace_id=workspace_id,
        reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
        **event_payload,
    )
    return fetched_head, None
