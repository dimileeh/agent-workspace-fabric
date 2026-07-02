"""PR monitor agent-service recovery operation helpers."""

from __future__ import annotations

from collections.abc import Mapping
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
    return updates


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
