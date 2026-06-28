"""Agent compose-service recovery for PR monitor agent runs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn, cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.adapters.provider_failures import (
    AGENT_IDLE_TIMEOUT,
    AGENT_SERVICE_UNHEALTHY,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.companion_services import companion_specs_from_task_policy
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
    verify_head_object_exists,
)
from awf.node.stack_launcher import effective_compose_up_timeout_seconds
from awf.profiles.models import WorkspaceProfile
from awf.runtime.inspection import RuntimeInspector, probe_agent_service_health
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_RECOVERED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.remote_repair import (
    _recover_missing_head_object_from_filesystem,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)

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
    operation_start_head: str | None = None,
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
            await _rerun_monitor_agent_pre_launch_guards(self, workspace_id=workspace_id)
            continue
        except ComposeExecCleanupError as exc:
            recovered = await _recover_monitor_agent_service_after_cleanup_error(
                self,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                restart_attempts=restart_attempts,
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
            )
            if recovered is None:
                raise
            restart_attempts = recovered
            await _rerun_monitor_agent_pre_launch_guards(self, workspace_id=workspace_id)
            continue
        append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
        return cast(AgentRunResult, result)


async def _rerun_monitor_agent_pre_launch_guards(
    self: Any,
    *,
    workspace_id: str,
) -> None:
    if await self._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    worktree_path = self._worktrees_root / workspace_id
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is None:
        return
    try:
        await repair_mirror_hooks_path(mirror_path)
    except (GitOperationError, OSError) as exc:
        repair_details = mirror_hooks_repair_failure_details(
            exc,
            repair_stage="before_recovered_monitor_agent_retry",
            mirror_path=mirror_path,
        )
        _log.warning(
            "monitor.agent_service_recovery_mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            **repair_details,
        )
        raise _MonitorMirrorHooksPathRepairFailedError() from exc


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
    if not compose_file.is_file():
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


async def _recover_monitor_agent_service_after_cleanup_error(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: ComposeExecCleanupError,
    restart_attempts: int,
    command_evidence: list[str] | None,
    operation_start_head: str | None,
) -> int | None:
    service_healthy = await probe_agent_service_health(RuntimeInspector(), compose_project)
    if service_healthy is not False or not _cleanup_failure_indicates_agent_service_down(exc):
        return None
    cleanup_result = exc.cleanup_result
    append_command_evidence(
        command_evidence,
        stdout=cleanup_result.stdout if cleanup_result is not None else "",
        stderr=cleanup_result.stderr if cleanup_result is not None else str(exc),
    )
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts + 1,
    )
    await _repair_monitor_git_after_recoverable_agent_cleanup_failure(
        self,
        workspace_id=workspace_id,
        operation_start_head=operation_start_head,
        command_evidence=command_evidence,
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


async def _repair_monitor_git_after_recoverable_agent_cleanup_failure(
    self: Any,
    *,
    workspace_id: str,
    operation_start_head: str | None,
    command_evidence: list[str] | None,
) -> None:
    worktree_path = self._worktrees_root / workspace_id
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            repair_details = mirror_hooks_repair_failure_details(
                exc,
                repair_stage="after_monitor_agent_cleanup_failure",
                mirror_path=mirror_path,
            )
            _log.warning(
                "monitor.agent_cleanup_mirror_hooks_path_repair_failed",
                workspace_id=workspace_id,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                **repair_details,
            )
            raise _MonitorMirrorHooksPathRepairFailedError() from exc

    if await verify_head_object_exists(worktree_path):
        return

    recovery_head = operation_start_head or await self._open_merge_candidate_head_sha(workspace_id)
    if recovery_head is None:
        _log.warning(
            "monitor.agent_cleanup_head_object_missing",
            workspace_id=workspace_id,
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        raise _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            f"HEAD object missing for workspace {workspace_id} after agent cleanup failure",
        )
    recovered = await _recover_missing_head_object_from_filesystem(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_start_head=recovery_head,
        command_evidence=tuple(command_evidence or ()),
    )
    if recovered is None:
        _log.warning(
            "monitor.agent_cleanup_head_object_recovery_failed",
            workspace_id=workspace_id,
            recovery_head=recovery_head[:10],
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        raise _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            f"HEAD object recovery failed for workspace {workspace_id} after agent cleanup failure",
        )
    _log.info(
        "monitor.agent_cleanup_head_object_recovered",
        workspace_id=workspace_id,
        recovered_head=recovered[:10],
        reason_code=_HEAD_OBJECT_MISSING_RECOVERED_REASON,
    )


async def _restart_monitor_agent_service_or_fail(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError | ComposeExecCleanupError,
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
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    manager = ComposeManager(
        work_dir=self._work_dir,
        template_path=_monitor_agent_service_recovery_template_sentinel(self._work_dir),
    )
    compose_up_timeout_seconds = await _monitor_agent_service_restart_timeout_seconds(
        self,
        workspace_id=workspace_id,
    )
    try:
        await manager.ensure_project_up(
            project_name=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            wait=True,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
            force_recreate=True,
            services=("agent",),
        )
    except ComposeOperationError as restart_exc:
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
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    return restart_attempts


async def _raise_if_monitor_agent_service_recovery_was_superseded(
    self: Any,
    *,
    workspace_id: str,
    source_reason_code: str,
    service_healthy: bool | None,
    restart_attempts: int,
) -> None:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            message = "agent compose service recovery superseded: workspace disappeared"
            _log.warning(
                "monitor.agent_service_recovery_superseded",
                workspace_id=workspace_id,
                reason="workspace_missing",
            )
            raise _MonitorAgentServiceRecoverySupersededError(
                message,
                reason_code=AGENT_SERVICE_UNHEALTHY,
                details=_agent_service_recovery_source_details(
                    source_reason_code=source_reason_code,
                    service_healthy=service_healthy,
                    restart_attempts=restart_attempts,
                    superseded_reason="workspace_missing",
                ),
            )
        if workspace.status != WorkspaceStatus.monitoring_pr.value:
            message = "agent compose service recovery superseded: workspace left monitoring_pr"
            _log.warning(
                "monitor.agent_service_recovery_superseded",
                workspace_id=workspace_id,
                reason="status_changed",
                status=workspace.status,
            )
            raise _MonitorAgentServiceRecoverySupersededError(
                message,
                reason_code=AGENT_SERVICE_UNHEALTHY,
                details=_agent_service_recovery_source_details(
                    source_reason_code=source_reason_code,
                    service_healthy=service_healthy,
                    restart_attempts=restart_attempts,
                    superseded_reason="status_changed",
                ),
            )
        monitor_owner_id = getattr(self, "_monitor_owner_id", None)
        superseded_claimed_runner = (
            monitor_owner_id is not None and workspace.monitor_claimed_by != monitor_owner_id
        )
        superseded_inline_handoff = (
            monitor_owner_id is None and workspace.monitor_claimed_by is not None
        )
        if superseded_claimed_runner or superseded_inline_handoff:
            message = "agent compose service recovery superseded: monitor claim changed"
            _log.warning(
                "monitor.agent_service_recovery_superseded",
                workspace_id=workspace_id,
                reason="monitor_claim_changed",
                monitor_owner_id=monitor_owner_id,
                monitor_claimed_by=workspace.monitor_claimed_by,
            )
            raise _MonitorAgentServiceRecoverySupersededError(
                message,
                reason_code=AGENT_SERVICE_UNHEALTHY,
                details=_agent_service_recovery_source_details(
                    source_reason_code=source_reason_code,
                    service_healthy=service_healthy,
                    restart_attempts=restart_attempts,
                    superseded_reason="monitor_claim_changed",
                ),
            )


async def _monitor_agent_service_restart_timeout_seconds(
    self: Any,
    *,
    workspace_id: str,
) -> int:
    try:
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None or not workspace.resolved_profile:
                return _MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS
            profile = WorkspaceProfile.model_validate(workspace.resolved_profile)
            task_policy = (
                workspace.task_policy if isinstance(workspace.task_policy, Mapping) else {}
            )
            return effective_compose_up_timeout_seconds(
                profile=profile,
                companions=companion_specs_from_task_policy(task_policy),
            )
    except (SQLAlchemyError, ValidationError):
        _log.exception(
            "monitor.agent_service_restart_timeout_resolution_failed",
            workspace_id=workspace_id,
        )
        return _MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS


def _agent_service_recovery_source_details(
    *,
    source_reason_code: str,
    service_healthy: bool | None,
    restart_attempts: int,
    superseded_reason: str | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": source_reason_code,
        "service_healthy": service_healthy,
        "restart_attempts": restart_attempts,
    }
    if superseded_reason is not None:
        details["superseded_reason"] = superseded_reason
    return details


async def _terminate_monitor_for_unhealthy_agent_service(
    self: Any,
    *,
    workspace_id: str,
    exc: AgentRunError | ComposeExecCleanupError,
    service_healthy: bool | None,
    restart_attempts: int,
    message: str,
) -> NoReturn:
    exc_details = getattr(exc, "details", None)
    details = dict(exc_details) if isinstance(exc_details, dict) else {}
    details["provider_recovery"] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "failure_type": "runtime_unhealthy",
        "failure_scope": "infra",
        "retryable": True,
        "failure_fingerprint": "",
        "fallback_allowed": False,
    }
    agent_service_recovery_details = _agent_service_recovery_source_details(
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    details["agent_service_recovery"] = agent_service_recovery_details
    await self._terminate_failed(
        workspace_id,
        message=message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=details,
    )
    raise _MonitorAgentServiceRecoveryFailedError(
        message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=agent_service_recovery_details,
    )


def _monitor_agent_service_recovery_template_sentinel(work_dir: Path) -> Path:
    return work_dir / "compose" / ".monitor-agent-service-recovery-does-not-render.yml.j2"


def _cleanup_failure_indicates_agent_service_down(exc: ComposeExecCleanupError) -> bool:
    if exc.reason_code != EXEC_PROCESS_CLEANUP_FAILED:
        return False
    result = exc.cleanup_result
    if result is None:
        output = str(exc)
    else:
        output = f"{result.stdout}\n{result.stderr}"
        if not output.strip():
            output = str(exc)
    normalized = output.lower()
    return (
        'service "agent" is not running' in normalized
        or "service 'agent' is not running" in normalized
    )


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
