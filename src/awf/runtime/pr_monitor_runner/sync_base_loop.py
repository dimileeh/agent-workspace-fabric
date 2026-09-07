"""SyncBase action branch for the PR monitor decision loop.

Mechanically extracted from :mod:`awf.runtime.pr_monitor_runner.loop`; behavior
is unchanged. Mirrors the ``merge_loop.handle_merge_action`` /
``notify_human_loop.handle_notify_human_action`` delegation pattern, except that
the two #910 post-action terminal helpers are closures over ``_execute``'s
per-cycle arguments, so the caller threads them in explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.constants import _AUDIT_GIT_PUSH_EVENT
from awf.runtime.pr_monitor_runner.helpers import _clear_transient_base_fetch_retry_state
from awf.runtime.pr_monitor_runner.loop_helpers import (
    _post_workflow_scope_notification_best_effort,
)
from awf.runtime.pr_monitor_runner.loop_recovery_ops import (
    _finish_agent_service_recovery_failed_operation,
    _finish_agent_service_recovery_superseded_operation,
)
from awf.runtime.pr_monitor_runner.remote_ops import _git_push_failure_outcome
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)


class _FinishIfPrTerminal(Protocol):
    """``_execute``'s post-action terminal-PR guard for a completed ``_run_*``."""

    async def __call__(self, operation: Any, push_result: Any) -> bool | None: ...


class _FinishIfPrTerminalAfterCleanupError(Protocol):
    """``_execute``'s terminal-PR guard for a ``ComposeExecCleanupError`` escape."""

    async def __call__(
        self, operation: Any, *, context: str, operation_type: str
    ) -> bool | None: ...


async def handle_sync_base_action(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    remote_push_url: str | None,
    compose_project: str,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None,
    finish_if_pr_terminal: _FinishIfPrTerminal,
    finish_if_pr_terminal_after_cleanup_error: _FinishIfPrTerminalAfterCleanupError,
) -> bool:
    """Merge the base branch into the PR branch and push; report terminality.

    The caller dispatches on ``isinstance(action, SyncBase)`` before invoking this
    helper. Returns ``True`` iff the monitor reached a terminal state.
    """
    operation = await self._begin_monitor_operation(
        workspace_id=workspace_id,
        operation_type=OperationType.sync_base,
        action="sync_base",
        requested_action="sync_base",
        reason="PR branch is behind the target branch.",
        reason_code="SYNC_BASE",
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        extra_identity=(state.iter_count,),
    )
    try:
        push_result = await self._run_sync_base(
            workspace_id=workspace_id,
            state=state,
            repo=repo,
            pr_number=pr_number,
            pr_head_sha=status.head_sha,
            base_branch=base_branch,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            compose_project=compose_project,
            compose_file=compose_file,
            operation_id=operation.operation_id if operation is not None else None,
            operation_type=OperationType.sync_base.value,
            monitor_log=monitor_log,
        )
        _clear_transient_base_fetch_retry_state(state, context="sync_base")
    except _MonitorAgentServiceRecoverySupersededError as exc:
        await _finish_agent_service_recovery_superseded_operation(
            self,
            operation,
            exc=exc,
            error_message=str(exc),
        )
        raise
    except _MonitorAgentServiceRecoveryFailedError as exc:
        await _finish_agent_service_recovery_failed_operation(
            self,
            operation,
            exc=exc,
            error_message=str(exc),
        )
        raise
    except ProviderRecoveryRetryError:
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": "provider_retry",
                "reason_code": "PROVIDER_OUTAGE",
                "pushed": False,
            },
            error_code="PROVIDER_OUTAGE",
            error_message="Provider recovery requested retry",
        )
        raise
    except ProviderRecoveryFallbackError:
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": "provider_fallback",
                "reason_code": "PROVIDER_FALLBACK",
                "pushed": False,
            },
            error_code="PROVIDER_FALLBACK",
            error_message="Provider recovery triggered fallback",
        )
        raise
    except ProviderRecoveryAuthError:
        await self._finish_provider_auth_failed_operation(operation)
        raise
    except BaseFetchError as exc:
        base_fetch_result = await self._wait_after_transient_base_fetch_error(
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="sync_base",
            state=state,
            monitor_log=monitor_log,
        )
        if base_fetch_result.retry:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "retrying",
                    "outcome": "transient_base_fetch_error",
                    "reason_code": base_fetch_result.reason_code,
                    "pushed": False,
                },
                error_code=base_fetch_result.reason_code,
                error_message=str(exc),
            )
            return False
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": "base_fetch_failed",
                "reason_code": base_fetch_result.reason_code,
                "pushed": False,
            },
            error_code=base_fetch_result.reason_code,
            error_message=str(exc),
        )
        await self._terminate_failed(
            workspace_id,
            message=f"monitor: could not refresh base branch: {exc}"[:2000],
            reason_code=base_fetch_result.reason_code,
        )
        return True
    except ComposeExecCleanupError as exc:
        cleanup_terminal_result = await finish_if_pr_terminal_after_cleanup_error(
            operation,
            context="sync_base_cleanup_failure",
            operation_type=OperationType.sync_base.value,
        )
        if cleanup_terminal_result is not None:
            return cleanup_terminal_result
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
            },
            error_code=EXEC_PROCESS_CLEANUP_FAILED,
            error_message=cleanup_failure_message(exc),
        )
        await self._terminate_failed(
            workspace_id,
            message=cleanup_failure_message(exc),
            reason_code=EXEC_PROCESS_CLEANUP_FAILED,
        )
        return True
    pr_terminal_result = await finish_if_pr_terminal(operation, push_result)
    if pr_terminal_result is not None:
        return pr_terminal_result
    if push_result.paused_into_blocked:
        # A protected-scope violation in the base-conflict resolution commit
        # paused the workspace into ``blocked`` for an operator decision
        # (WS-2). The row already left ``monitoring_pr`` (preserving the
        # offending commit); end the monitor cycle cleanly — do NOT terminally
        # fail. Persist state so the notification dedupe + preserved-commit
        # marker survive a restart.
        await self._persist_state(workspace_id, state)
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "protected_scope_paused",
                "reason_code": push_result.reason_code,
                "pushed": False,
            },
        )
        return True
    if push_result.failed:
        reason_code = push_result.reason_code
        outcome = _git_push_failure_outcome(push_result)
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": outcome,
                "reason_code": reason_code,
                "pushed": push_result.pushed,
            },
            error_code=reason_code,
            error_message=push_result.error_message,
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_GIT_PUSH_EVENT,
            action="sync_base_push",
            outcome="failed",
            reason_code=reason_code,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation.operation_id if operation is not None else None,
            operation_type=OperationType.sync_base.value,
            monitor_log=monitor_log,
            evidence=push_result.failure_evidence(),
        )
        if push_result.workflow_scope_required:
            notification_moot = await _post_workflow_scope_notification_best_effort(
                self,
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                blocker_reason=push_result.error_message or push_result.reason_code,
            )
            if notification_moot is not None:
                # The notification boundary's fresh read saw the PR go terminal
                # between the pre-push guard and this push rejection, so the terminal
                # fail below would mark a MERGED workspace failed. Run the moot
                # completion path on that observation instead
                # (PRRT_kwDOSJAM6s6fvGsp). ``operation`` is ``None``: this arm
                # already finished it as ``failed`` above, and that push failure is a
                # true audit record worth keeping.
                #
                # The moot envelope always carries the observation, so — unlike the
                # post-``_run_*`` call above, which sees real pushes — this call can
                # never report "not moot". Take the terminate sink's own result
                # directly instead of guarding on an arc this call cannot produce;
                # ``False`` still means the sink refused the write because this
                # runner was superseded.
                return bool(await finish_if_pr_terminal(None, notification_moot))
        if push_result.terminal_monitor_failure or push_result.workflow_scope_required:
            await self._terminate_failed(
                workspace_id,
                message=push_result.error_message or push_result.reason_code,
                reason_code=push_result.reason_code,
                details=push_result.failure_evidence(),
                failure_reason=push_result.failure_reason,
            )
            return True
        self._record_sync_base_progress(
            state=state,
            status=status,
            push_result=push_result,
        )
        state.iter_count += 1
        return False
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.succeeded,
        result={
            "status": "succeeded",
            "outcome": "base_synced",
            "pushed": push_result.pushed,
        },
    )
    self._record_sync_base_progress(
        state=state,
        status=status,
        push_result=push_result,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_GIT_PUSH_EVENT,
        action="sync_base_push",
        outcome="succeeded",
        reason_code="SYNC_BASE",
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation.operation_id if operation is not None else None,
        operation_type=OperationType.sync_base.value,
        monitor_log=monitor_log,
    )
    if push_result.pushed:
        # SyncBase merged ``origin/<base>`` into the workspace branch and
        # pushed. Without this call, the ``STALE_TARGET_ADVANCED`` row
        # the staleness service wrote when target first advanced stays
        # ``status=active, resolved_at=null`` with ``blocks_merge=true``
        # — gating every subsequent merge attempt even though the
        # monitor's own ``base_behind`` check is back to 0. Advance the
        # candidate's validation base to the SHA we just merged in and
        # refresh staleness so the resolution propagates atomically.
        await self._refresh_staleness_after_sync_base(
            workspace_id=workspace_id,
            base_branch=base_branch,
        )
    state.iter_count += 1
    return False
