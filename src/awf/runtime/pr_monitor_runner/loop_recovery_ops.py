"""PR monitor agent-service recovery operation helpers."""

from __future__ import annotations

from typing import Any

from awf.db.enums import OperationStatus
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
