"""AddressOperatorHint action branch for the PR monitor decision loop.

Mechanically extracted from :mod:`awf.runtime.pr_monitor_runner.loop` so that
module stays within the first-party file-line guardrail
(``tests/unit/test_core_decomposition_maintainability.py``). Behavior is
unchanged; this mirrors the ``notify_human_loop.handle_notify_human_action``
delegation pattern — the caller dispatches on
``isinstance(action, AddressOperatorHint)`` before invoking this helper. As in
``sync_base_loop``, the two #910 post-action terminal helpers are closures over
``_execute``'s per-cycle arguments, so the caller threads them in explicitly.
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
    AddressOperatorHint,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.loop_recovery_ops import (
    _finish_agent_service_recovery_failed_operation,
    _finish_agent_service_recovery_superseded_operation,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _git_push_failure_outcome,
)
from awf.runtime.pr_monitor_runner.types import (
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


async def handle_operator_hint_action(
    self: Any,
    *,
    action: AddressOperatorHint,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    compose_project: str,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None,
    remote_push_url: str | None,
    finish_if_pr_terminal: _FinishIfPrTerminal,
    finish_if_pr_terminal_after_cleanup_error: _FinishIfPrTerminalAfterCleanupError,
) -> bool:
    """Run one operator-hint repair cycle; return True iff the monitor is terminal."""
    operation = await self._begin_monitor_operation(
        workspace_id=workspace_id,
        operation_type=OperationType.comment_repair,
        action="operator_hint_repair",
        requested_action="address_operator_hint",
        reason="Operator remonitor hint required repair before merge.",
        reason_code=action.hint.reason_code,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        extra_payload={
            "operator_hint_operation_id": action.hint.operation_id,
            "operator_hint_status": action.hint.status,
        },
        extra_identity=(action.hint.operation_id, action.hint.reason),
    )
    try:
        push_result = await self._run_operator_hint_cycle(
            workspace_id=workspace_id,
            repo=repo,
            pr_number=pr_number,
            pr_head_sha=status.head_sha,
            hint=action.hint,
            state=state,
            base_branch=base_branch,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            compose_project=compose_project,
            compose_file=compose_file,
            _monitor_log=monitor_log,
            _operation_id=operation.operation_id if operation is not None else None,
            _operation_type=OperationType.comment_repair.value,
        )
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
    except ComposeExecCleanupError as exc:
        cleanup_terminal_result = await finish_if_pr_terminal_after_cleanup_error(
            operation,
            context="operator_hint_cleanup_failure",
            operation_type=OperationType.comment_repair.value,
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
        # A directive-revert / grant resume that still trips the protected
        # gate re-paused the workspace into ``blocked`` (WS-2 §2 re-block).
        # End the cycle cleanly without terminally failing.
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
    if push_result.operator_hint_timeout_retry:
        # The #932 watchdog fired with the agent's work preserved and the hint
        # spent its single retry: it stays ``pending`` so ``decide()`` re-issues
        # ``AddressOperatorHint``. Nothing was pushed and no human wait was
        # entered, so the outcome ladder below would call this a succeeded
        # ``operator_hint_needs_human`` and drop the watchdog code. Record the
        # attempt as what it was — a failure that earned a retry — with its
        # reason code intact (AGENTS.md: retries must preserve reason codes).
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": "operator_hint_timeout_retry",
                "reason_code": push_result.reason_code,
                "pushed": False,
            },
            error_code=push_result.reason_code,
            error_message=(
                f"Operator hint agent timed out ({push_result.reason_code}); "
                "preserved work is retried once before a human is notified."
            ),
        )
        state.iter_count += 1
        return False
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
                "pushed": False,
                "failure_evidence": push_result.failure_evidence(),
            },
            error_code=reason_code,
            error_message=push_result.error_message,
        )
        if push_result.terminal_monitor_failure:
            await self._persist_state(workspace_id, state)
            await self._terminate_failed(
                workspace_id,
                message=push_result.error_message or push_result.reason_code,
                reason_code=push_result.reason_code,
                details=push_result.failure_evidence(),
                failure_reason=push_result.failure_reason,
            )
            return True
        state.iter_count += 1
        return False
    if push_result.pushed or (
        not push_result.pushed
        and (
            state.pending_operator_hint is None
            or state.pending_operator_hint.status in {"needs_human", "agent_failed"}
        )
    ):
        # Persist terminal, processed no-op, or pushed hint status before
        # returning to the outer loop so a restart cannot re-run the same
        # hint as pending.
        await self._persist_state(workspace_id, state)
    if push_result.pushed:
        outcome = "operator_hint_pushed"
    elif state.pending_operator_hint is None:
        outcome = "operator_hint_processed"
    elif (
        state.pending_operator_hint is not None
        and state.pending_operator_hint.status == "agent_failed"
    ):
        outcome = "operator_hint_agent_failed"
    else:
        outcome = "operator_hint_needs_human"
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.succeeded,
        result={
            "status": "succeeded",
            "outcome": outcome,
            "pushed": push_result.pushed,
        },
    )
    state.iter_count += 1
    return False
