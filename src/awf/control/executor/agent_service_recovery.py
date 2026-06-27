"""Agent compose-service recovery helpers for the workspace executor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentAdapter, AgentRunError
from awf.adapters.provider_failures import (
    AGENT_IDLE_TIMEOUT,
    AGENT_SERVICE_UNHEALTHY,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.profiles.models import WorkspaceProfile
from awf.runtime.inspection import RuntimeInspector, probe_agent_service_health

_AGENT_SERVICE_TIMEOUT_REASON_CODES = frozenset({AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT})
_AGENT_SERVICE_RESTART_ATTEMPTS = 2


async def _run_agent_task_with_service_recovery(
    self: Any,
    *,
    adapter: AgentAdapter,
    workspace: Any,
    profile: WorkspaceProfile,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
    model: str | None,
    command_evidence: list[str],
    workspace_id: str,
    before_mark_failed: Callable[[], None] | None = None,
) -> tuple[bool, Any]:
    restart_attempts = 0
    while True:
        try:
            return (
                True,
                await self._run_agent_task_with_optional_planning(
                    adapter=adapter,
                    workspace=workspace,
                    profile=profile,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    worktree_path=worktree_path,
                    model=model,
                    command_evidence=command_evidence,
                ),
            )
        except AgentRunError as exc:
            if exc.reason_code not in _AGENT_SERVICE_TIMEOUT_REASON_CODES:
                raise
            service_healthy = await probe_agent_service_health(
                RuntimeInspector(),
                compose_project,
            )
            classification = _classify_timeout_with_service_health(
                exc,
                model=model,
                service_healthy=service_healthy,
            )
            if classification is None or classification.reason_code != AGENT_SERVICE_UNHEALTHY:
                raise
            append_command_evidence(
                command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )
            restart_attempts, restarted = await _restart_agent_service_or_mark_unhealthy(
                self,
                workspace_id=workspace_id,
                profile=profile,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                service_healthy=service_healthy,
                restart_attempts=restart_attempts,
                before_mark_failed=before_mark_failed,
            )
            if not restarted:
                return False, None
        except ComposeExecCleanupError as exc:
            service_healthy = await probe_agent_service_health(
                RuntimeInspector(),
                compose_project,
            )
            if service_healthy is not False or not _cleanup_failure_indicates_agent_service_down(
                exc
            ):
                raise
            cleanup_result = exc.cleanup_result
            append_command_evidence(
                command_evidence,
                stdout=cleanup_result.stdout if cleanup_result is not None else "",
                stderr=cleanup_result.stderr if cleanup_result is not None else str(exc),
            )
            restart_attempts, restarted = await _restart_agent_service_or_mark_unhealthy(
                self,
                workspace_id=workspace_id,
                profile=profile,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                service_healthy=service_healthy,
                restart_attempts=restart_attempts,
                before_mark_failed=before_mark_failed,
            )
            if not restarted:
                return False, None


def _classify_timeout_with_service_health(
    exc: AgentRunError,
    *,
    model: str | None,
    service_healthy: bool | None,
) -> Any:
    details = exc.details if isinstance(exc.details, Mapping) else {}
    provider_recovery = details.get("provider_recovery")
    if not isinstance(provider_recovery, Mapping):
        provider_recovery = {}
    provider = _mapping_str(details, "provider") or _mapping_str(provider_recovery, "provider")
    classified_model = (
        _mapping_str(details, "model") or _mapping_str(provider_recovery, "model") or model
    )
    return classify_provider_failure(
        reason_code=exc.reason_code,
        stdout=exc.result.stdout,
        stderr=exc.result.stderr,
        provider=provider,
        model=classified_model,
        service_healthy=service_healthy,
    )


async def _restart_agent_service_or_mark_unhealthy(
    self: Any,
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError | ComposeExecCleanupError,
    service_healthy: bool | None,
    restart_attempts: int,
    before_mark_failed: Callable[[], None] | None = None,
) -> tuple[int, bool]:
    if restart_attempts >= _AGENT_SERVICE_RESTART_ATTEMPTS:
        await _mark_agent_service_unhealthy(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            message="agent compose service stayed unhealthy after restart attempts",
            before_mark_failed=before_mark_failed,
        )
        return restart_attempts, False
    restart_attempts += 1
    try:
        await self._compose.ensure_project_up(
            project_name=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            wait=True,
            compose_up_timeout_seconds=profile.docker.startup_timeout_seconds,
        )
    except Exception as restart_exc:
        await _mark_agent_service_unhealthy(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            message=f"agent compose service restart failed: {restart_exc!r}"[:2000],
            before_mark_failed=before_mark_failed,
        )
        return restart_attempts, False
    return restart_attempts, True


async def _mark_agent_service_unhealthy(
    self: Any,
    *,
    workspace_id: str,
    exc: AgentRunError | ComposeExecCleanupError,
    service_healthy: bool | None,
    restart_attempts: int,
    message: str,
    before_mark_failed: Callable[[], None] | None = None,
) -> None:
    exc_details = getattr(exc, "details", None)
    details = dict(exc_details) if isinstance(exc_details, Mapping) else {}
    details["provider_recovery"] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "failure_type": "runtime_unhealthy",
        "failure_scope": "infra",
        "retryable": True,
        "failure_fingerprint": "",
        "fallback_allowed": False,
    }
    details["agent_service_recovery"] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": exc.reason_code,
        "service_healthy": service_healthy,
        "restart_attempts": restart_attempts,
    }
    if before_mark_failed is not None:
        before_mark_failed()
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message=message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=details,
    )


def _mapping_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _cleanup_failure_indicates_agent_service_down(exc: ComposeExecCleanupError) -> bool:
    if exc.reason_code != EXEC_PROCESS_CLEANUP_FAILED:
        return False
    result = exc.cleanup_result
    output = f"{result.stdout}\n{result.stderr}" if result is not None else str(exc)
    normalized = output.lower()
    return (
        'service "agent" is not running' in normalized
        or "service 'agent' is not running" in normalized
    )
