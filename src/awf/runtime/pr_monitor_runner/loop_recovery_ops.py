"""PR monitor agent-service recovery operation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awf.db.enums import OperationStatus
from awf.runtime.pr_monitor_runner.remote_ops import _git_push_failure_outcome
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)

_MONITOR_AGENT_SERVICE_RECOVERY_FAILED_REASON = "MONITOR_RECOVERY_FAILED"
_MONITOR_AGENT_SERVICE_RECOVERY_SUPERSEDED_REASON = "MONITOR_RECOVERY_SUPERSEDED"


def _agent_service_recovery_operation_reason(
    exc: _MonitorAgentServiceRecoveryFailedError | _MonitorAgentServiceRecoverySupersededError,
    *,
    fallback_reason_code: str,
) -> str:
    reason_code = getattr(exc, "reason_code", None)
    return reason_code if isinstance(reason_code, str) and reason_code else fallback_reason_code


def _agent_service_recovery_operation_details(
    exc: _MonitorAgentServiceRecoveryFailedError | _MonitorAgentServiceRecoverySupersededError,
) -> dict[str, object] | None:
    details = getattr(exc, "details", None)
    return dict(details) if isinstance(details, dict) else None


def _attach_provider_recovery_details(
    exc: BaseException,
    details: Mapping[str, object],
) -> None:
    """Merge salvage/repair metadata onto a provider-recovery control-flow exception."""
    if not details:
        return
    existing = getattr(exc, "details", None)
    if isinstance(existing, dict):
        merged = dict(existing)
        merged.update(details)
        exc.details = merged  # type: ignore[attr-defined]
        return
    exc.details = dict(details)  # type: ignore[attr-defined]


def _provider_recovery_operation_result_updates(exc: BaseException) -> dict[str, object]:
    """Extract discoverable salvage metadata from a provider-recovery exception."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return {}
    updates: dict[str, object] = {}
    repair_salvage = details.get("repair_salvage")
    if isinstance(repair_salvage, dict):
        updates["repair_salvage"] = repair_salvage
    stranded_paths = details.get("stranded_paths")
    if isinstance(stranded_paths, list):
        updates["stranded_paths"] = stranded_paths
    phase = details.get("phase")
    if isinstance(phase, str):
        updates["phase"] = phase
    provider_error_stderr = details.get("provider_error_stderr")
    if isinstance(provider_error_stderr, str):
        updates["provider_error_stderr"] = provider_error_stderr
    salvage_error = details.get("salvage_error")
    if isinstance(salvage_error, dict):
        updates["salvage_error"] = salvage_error
    rollback_error = details.get("rollback_error")
    if isinstance(rollback_error, dict):
        updates["rollback_error"] = rollback_error
    return updates


async def _finish_parked_comment_repair_cycle(
    self: Any,
    *,
    workspace_id: str,
    state: Any,
    operation: Any,
    push_result: Any,
    thread_count: int,
    review_comment_count: int,
) -> bool:
    """End an ``AddressComments`` cycle that parked preserved commits for a human (#935).

    The workspace stays in ``monitoring_pr`` with the worktree untouched: nothing was
    reset and nothing was pushed. Persist the monitor state (the item-provenance chain
    rides ``monitor_threads_addressed``), finish the operation ``failed`` carrying the
    preserved reason code, and raise the awaiting-human attention flag naming the
    commits. Re-entering on a later poll re-parks idempotently — the abandon check runs
    before any item work, so no agent is launched.
    """
    state.clear_awaiting_workflow_scope()
    await self._persist_state(workspace_id, state)
    reason_code = push_result.reason_code
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.failed,
        result={
            "status": "failed",
            "outcome": _git_push_failure_outcome(push_result),
            "reason_code": reason_code,
            "thread_count": thread_count,
            "review_comment_count": review_comment_count,
            "pushed": False,
            "failure_evidence": push_result.failure_evidence(),
        },
        error_code=reason_code,
        error_message=push_result.error_message,
    )
    await self._set_workspace_attention(
        workspace_id,
        reason=push_result.error_message or reason_code,
    )
    return True


async def _finish_agent_service_recovery_failed_operation(
    self: Any,
    operation: Any,
    *,
    exc: _MonitorAgentServiceRecoveryFailedError,
    error_message: str,
    extra_result: dict[str, object] | None = None,
) -> None:
    reason_code = _agent_service_recovery_operation_reason(
        exc,
        fallback_reason_code=_MONITOR_AGENT_SERVICE_RECOVERY_FAILED_REASON,
    )
    result: dict[str, object] = {
        "status": "failed",
        "outcome": "agent_service_recovery_failed",
        "reason_code": reason_code,
        "pushed": False,
    }
    details = _agent_service_recovery_operation_details(exc)
    if details is not None:
        result["agent_service_recovery"] = details
    if extra_result:
        result.update(extra_result)
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.failed,
        result=result,
        error_code=reason_code,
        error_message=error_message,
    )


async def _finish_agent_service_recovery_superseded_operation(
    self: Any,
    operation: Any,
    *,
    exc: _MonitorAgentServiceRecoverySupersededError,
    error_message: str,
    extra_result: dict[str, object] | None = None,
) -> None:
    reason_code = _agent_service_recovery_operation_reason(
        exc,
        fallback_reason_code=_MONITOR_AGENT_SERVICE_RECOVERY_SUPERSEDED_REASON,
    )
    result: dict[str, object] = {
        "status": "cancelled",
        "outcome": "agent_service_recovery_superseded",
        "reason_code": reason_code,
        "pushed": False,
    }
    details = _agent_service_recovery_operation_details(exc)
    if details is not None:
        result["agent_service_recovery"] = details
    if extra_result:
        result.update(extra_result)
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.cancelled,
        result=result,
        error_code=reason_code,
        error_message=error_message,
    )
