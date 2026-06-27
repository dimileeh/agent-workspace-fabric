"""Agent compose-service recovery for PR monitor agent runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.adapters.provider_failures import (
    AGENT_IDLE_TIMEOUT,
    AGENT_SERVICE_UNHEALTHY,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.common.command_evidence import append_command_evidence
from awf.node.compose_manager import ComposeManager
from awf.runtime.inspection import RuntimeInspector, probe_agent_service_health
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.types import _MonitorAgentServiceRecoveryFailedError

_AGENT_SERVICE_TIMEOUT_REASON_CODES = frozenset({AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT})
_AGENT_SERVICE_RESTART_ATTEMPTS = 2
_MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS = 300


async def _run_monitor_agent_with_service_recovery(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    prompt: str,
    log_source: str,
    command_evidence: list[str] | None = None,
) -> AgentRunResult:
    restart_attempts = 0
    while True:
        try:
            result = await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
                log_source=log_source,
            )
        except AgentRunError as exc:
            recovered = await _recover_monitor_agent_service_after_error(
                self,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                restart_attempts=restart_attempts,
                command_evidence=command_evidence,
            )
            if recovered is None:
                raise
            restart_attempts = recovered
            continue
        append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
        return cast(AgentRunResult, result)


async def _recover_monitor_agent_service_after_error(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError,
    restart_attempts: int,
    command_evidence: list[str] | None,
) -> int | None:
    if exc.reason_code not in _AGENT_SERVICE_TIMEOUT_REASON_CODES:
        return None
    service_healthy = await probe_agent_service_health(RuntimeInspector(), compose_project)
    classification = classify_provider_failure(
        reason_code=exc.reason_code,
        stdout=exc.result.stdout,
        stderr=exc.result.stderr,
        provider=_provider_from_error(exc),
        model=_model_from_error(exc),
        service_healthy=service_healthy,
    )
    if classification is None or classification.reason_code != AGENT_SERVICE_UNHEALTHY:
        return None
    append_command_evidence(
        command_evidence,
        stdout=exc.result.stdout,
        stderr=exc.result.stderr,
    )
    return await _restart_monitor_agent_service_or_fail(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        exc=exc,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )


async def _restart_monitor_agent_service_or_fail(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError,
    service_healthy: bool | None,
    restart_attempts: int,
) -> int:
    if restart_attempts >= _AGENT_SERVICE_RESTART_ATTEMPTS:
        await _terminate_monitor_for_unhealthy_agent_service(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            message="agent compose service stayed unhealthy after restart attempts",
        )
    restart_attempts += 1
    manager = ComposeManager(
        work_dir=self._work_dir,
        template_path=_monitor_agent_service_recovery_template_sentinel(self._work_dir),
    )
    try:
        await manager.ensure_project_up(
            project_name=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            wait=True,
            compose_up_timeout_seconds=_MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS,
        )
    except Exception as restart_exc:
        await _terminate_monitor_for_unhealthy_agent_service(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            message=f"agent compose service restart failed: {restart_exc!r}"[:2000],
        )
    _log.warning(
        "monitor.agent_service_restarted",
        workspace_id=workspace_id,
        compose_project=compose_project,
        restart_attempts=restart_attempts,
        reason_code=AGENT_SERVICE_UNHEALTHY,
    )
    return restart_attempts


async def _terminate_monitor_for_unhealthy_agent_service(
    self: Any,
    *,
    workspace_id: str,
    exc: AgentRunError,
    service_healthy: bool | None,
    restart_attempts: int,
    message: str,
) -> None:
    details = dict(exc.details) if isinstance(exc.details, dict) else {}
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
    await self._terminate_failed(
        workspace_id,
        message=message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=details,
    )
    raise _MonitorAgentServiceRecoveryFailedError(message)


def _monitor_agent_service_recovery_template_sentinel(work_dir: Path) -> Path:
    return work_dir / "compose" / ".monitor-agent-service-recovery-does-not-render.yml.j2"


def _provider_from_error(exc: AgentRunError) -> str | None:
    details = exc.details if isinstance(exc.details, dict) else {}
    provider = details.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    provider_recovery = details.get("provider_recovery")
    if isinstance(provider_recovery, dict):
        recovery_provider = provider_recovery.get("provider")
        if isinstance(recovery_provider, str) and recovery_provider.strip():
            return recovery_provider.strip()
    return None


def _model_from_error(exc: AgentRunError) -> str | None:
    details = exc.details if isinstance(exc.details, dict) else {}
    model = details.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    provider_recovery = details.get("provider_recovery")
    if isinstance(provider_recovery, dict):
        recovery_model = provider_recovery.get("model")
        if isinstance(recovery_model, str) and recovery_model.strip():
            return recovery_model.strip()
    return None
