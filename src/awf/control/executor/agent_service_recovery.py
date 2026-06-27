"""Agent compose-service recovery helpers for the workspace executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from inspect import isawaitable
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
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.control.executor.quality_gates import _log
from awf.control.executor.types import _PlanningRunFailure
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.node.companion_services import companion_specs_from_task_policy
from awf.node.compose_manager import ComposeOperationError
from awf.node.stack_launcher import effective_compose_up_timeout_seconds
from awf.profiles.models import WorkspaceProfile
from awf.runtime.inspection import RuntimeInspector, probe_agent_service_health
from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

_AGENT_SERVICE_TIMEOUT_REASON_CODES = frozenset({AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT})
_AGENT_SERVICE_RESTART_ATTEMPTS = 2
_BeforeMarkFailed = Callable[[], None | Awaitable[None]]


def _build_agent_service_recovery_callbacks(
    self: Any,
    *,
    workspace_id: str,
    workspace: Any,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
    execution_owner_id: str | None,
    repair_mirror_hooks_path_or_mark_failed: Callable[..., Awaitable[bool]],
    repair_hooks_after_agent_cleanup_failure: Callable[..., Awaitable[bool]],
    recover_missing_head_after_cleanup_failure: Callable[..., Awaitable[bool]],
    deposit_planning_artifacts: Callable[[], None],
    expected_status: WorkspaceStatus = WorkspaceStatus.running,
    cleanup_failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
    cleanup_failure_stage: str = "agent_run_cleanup_failure",
    verify_post_agent_commit: bool = True,
) -> tuple[Callable[[], Awaitable[bool]], Callable[[ComposeExecCleanupError], Awaitable[bool]]]:
    before_agent_retry = partial(
        _rerun_agent_pre_launch_guards,
        self,
        workspace_id=workspace_id,
        workspace=workspace,
        compose_project=compose_project,
        compose_file=compose_file,
        worktree_path=worktree_path,
        execution_owner_id=execution_owner_id,
        repair_mirror_hooks_path_or_mark_failed=repair_mirror_hooks_path_or_mark_failed,
        deposit_planning_artifacts=deposit_planning_artifacts,
        expected_status=expected_status,
        failure_from_status=cleanup_failure_from_status,
    )
    cleanup_repair = partial(
        _repair_after_recoverable_agent_cleanup_failure,
        self,
        workspace_id=workspace_id,
        owned_paths=list(workspace.owned_paths),
        execution_owner_id=execution_owner_id,
        repair_hooks_after_agent_cleanup_failure=repair_hooks_after_agent_cleanup_failure,
        recover_missing_head_after_cleanup_failure=recover_missing_head_after_cleanup_failure,
        deposit_planning_artifacts=deposit_planning_artifacts,
        failure_from_status=cleanup_failure_from_status,
        missing_head_recovery_stage=cleanup_failure_stage,
        verify_post_agent_commit=verify_post_agent_commit,
    )
    return before_agent_retry, cleanup_repair


async def _rerun_agent_pre_launch_guards(
    self: Any,
    *,
    workspace_id: str,
    workspace: Any,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
    execution_owner_id: str | None,
    repair_mirror_hooks_path_or_mark_failed: Callable[..., Awaitable[bool]],
    deposit_planning_artifacts: Callable[[], None],
    expected_status: WorkspaceStatus,
    failure_from_status: WorkspaceStatus,
) -> bool:
    if not await self._run_agent_git_writability_preflight(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        worktree_path=worktree_path,
        from_status=failure_from_status,
    ):
        return False
    if not await self._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=workspace,
        from_status=failure_from_status,
    ):
        return False
    if not await self._recheck_status(
        workspace_id,
        expected=expected_status,
        action="agent_run",
        owner_id=execution_owner_id,
    ):
        return False
    return await repair_mirror_hooks_path_or_mark_failed(
        failure_stage="before agent retry",
        before_mark_failed=deposit_planning_artifacts,
        failure_from_status=failure_from_status,
    )


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
    execution_owner_id: str | None = None,
    before_mark_failed: Callable[[], None] | None = None,
    before_agent_retry: Callable[[], Awaitable[bool]] | None = None,
    after_agent_cleanup_failure_repair: (
        Callable[[ComposeExecCleanupError], Awaitable[bool]] | None
    ) = None,
) -> tuple[bool, Any]:
    async def _run_initial_agent(accept_existing_plan: bool) -> Any:
        return await self._run_agent_task_with_optional_planning(
            adapter=adapter,
            workspace=workspace,
            profile=profile,
            compose_project=compose_project,
            compose_file=compose_file,
            worktree_path=worktree_path,
            model=model,
            command_evidence=command_evidence,
            accept_existing_plan=accept_existing_plan,
        )

    return await _run_agent_callable_with_service_recovery(
        self,
        run_agent=_run_initial_agent,
        workspace=workspace,
        profile=profile,
        compose_project=compose_project,
        compose_file=compose_file,
        model=model,
        command_evidence=command_evidence,
        workspace_id=workspace_id,
        execution_owner_id=execution_owner_id,
        before_mark_failed=before_mark_failed,
        before_agent_retry=before_agent_retry,
        after_agent_cleanup_failure_repair=after_agent_cleanup_failure_repair,
    )


async def _run_agent_callable_with_service_recovery(
    self: Any,
    *,
    run_agent: Callable[[bool], Awaitable[Any]],
    workspace: Any,
    profile: WorkspaceProfile,
    compose_project: str,
    compose_file: Path,
    model: str | None,
    command_evidence: list[str],
    workspace_id: str,
    execution_owner_id: str | None = None,
    before_mark_failed: _BeforeMarkFailed | None = None,
    before_agent_retry: Callable[[], Awaitable[bool]] | None = None,
    after_agent_cleanup_failure_repair: (
        Callable[[ComposeExecCleanupError], Awaitable[bool]] | None
    ) = None,
    expected_status: WorkspaceStatus = WorkspaceStatus.running,
    failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
) -> tuple[bool, Any]:
    restart_attempts = 0
    run_before_retry = False
    restart_compose_up_timeout_seconds = _agent_service_restart_timeout_seconds(
        profile=profile,
        workspace=workspace,
    )
    while True:
        if run_before_retry:
            run_before_retry = False
            if before_agent_retry is not None and not await before_agent_retry():
                await _run_before_mark_failed(before_mark_failed)
                return False, None
        try:
            planning_result = await run_agent(restart_attempts > 0)
            restart_result = await _restart_after_conformance_timeout_failure(
                self,
                planning_result=planning_result,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                model=model,
                restart_attempts=restart_attempts,
                compose_up_timeout_seconds=restart_compose_up_timeout_seconds,
                execution_owner_id=execution_owner_id,
                before_mark_failed=before_mark_failed,
                expected_status=expected_status,
                failure_from_status=failure_from_status,
            )
            if restart_result is None:
                return True, planning_result
            restart_attempts, restarted = restart_result
            if not restarted:
                return False, None
            run_before_retry = True
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
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                service_healthy=service_healthy,
                restart_attempts=restart_attempts,
                compose_up_timeout_seconds=restart_compose_up_timeout_seconds,
                execution_owner_id=execution_owner_id,
                before_mark_failed=before_mark_failed,
                expected_status=expected_status,
                failure_from_status=failure_from_status,
            )
            if not restarted:
                return False, None
            run_before_retry = True
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
            if after_agent_cleanup_failure_repair is not None:
                cleanup_repaired = await after_agent_cleanup_failure_repair(exc)
                if not cleanup_repaired:
                    await _run_before_mark_failed(before_mark_failed)
                    return False, None
            restart_attempts, restarted = await _restart_agent_service_or_mark_unhealthy(
                self,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                service_healthy=service_healthy,
                restart_attempts=restart_attempts,
                compose_up_timeout_seconds=restart_compose_up_timeout_seconds,
                execution_owner_id=execution_owner_id,
                before_mark_failed=before_mark_failed,
                expected_status=expected_status,
                failure_from_status=failure_from_status,
            )
            if not restarted:
                return False, None
            run_before_retry = True


async def _repair_after_recoverable_agent_cleanup_failure(
    self: Any,
    exc: ComposeExecCleanupError,
    *,
    workspace_id: str,
    owned_paths: list[str],
    execution_owner_id: str | None,
    repair_hooks_after_agent_cleanup_failure: Callable[..., Awaitable[bool]],
    recover_missing_head_after_cleanup_failure: Callable[..., Awaitable[bool]],
    deposit_planning_artifacts: Callable[[], None],
    failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
    missing_head_recovery_stage: str = "agent_run_cleanup_failure",
    verify_post_agent_commit: bool = True,
) -> bool:
    if not await repair_hooks_after_agent_cleanup_failure(
        failure_from_status=failure_from_status,
    ):
        return False
    if await recover_missing_head_after_cleanup_failure(
        exc,
        stage=missing_head_recovery_stage,
        from_status=failure_from_status,
        owned_paths=owned_paths,
        execution_owner_id=execution_owner_id,
        verify_post_agent_commit=verify_post_agent_commit,
    ):
        return True
    _log.error(
        "executor.exec_process_cleanup_failed",
        workspace_id=workspace_id,
        source=exc.source,
        label=exc.label,
        invocation_id=exc.invocation_id,
        reason_code=exc.reason_code,
    )
    deposit_planning_artifacts()
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=failure_from_status,
        failure_reason=FailureReason.infrastructure_failure,
        message=cleanup_failure_message(exc),
        reason_code=EXEC_PROCESS_CLEANUP_FAILED,
    )
    return False


def _agent_service_restart_timeout_seconds(
    *,
    profile: WorkspaceProfile,
    workspace: Any,
) -> int:
    task_policy = getattr(workspace, "task_policy", None)
    if not isinstance(task_policy, Mapping):
        return profile.docker.startup_timeout_seconds
    return effective_compose_up_timeout_seconds(
        profile=profile,
        companions=companion_specs_from_task_policy(task_policy),
    )


async def _restart_after_conformance_timeout_failure(
    self: Any,
    *,
    planning_result: Any,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    model: str | None,
    restart_attempts: int,
    compose_up_timeout_seconds: int,
    execution_owner_id: str | None = None,
    before_mark_failed: _BeforeMarkFailed | None = None,
    expected_status: WorkspaceStatus = WorkspaceStatus.running,
    failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
) -> tuple[int, bool] | None:
    source_reason_code = _conformance_stall_timeout_source_reason_code(planning_result)
    if source_reason_code is None:
        return None
    service_healthy = await probe_agent_service_health(
        RuntimeInspector(),
        compose_project,
    )
    classification = _classify_conformance_timeout_failure_with_service_health(
        planning_result,
        source_reason_code=source_reason_code,
        model=model,
        service_healthy=service_healthy,
    )
    if classification is None or classification.reason_code != AGENT_SERVICE_UNHEALTHY:
        return None
    return await _restart_agent_service_or_mark_unhealthy(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        exc=planning_result,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
        compose_up_timeout_seconds=compose_up_timeout_seconds,
        source_reason_code=source_reason_code,
        execution_owner_id=execution_owner_id,
        before_mark_failed=before_mark_failed,
        expected_status=expected_status,
        failure_from_status=failure_from_status,
    )


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


def _classify_conformance_timeout_failure_with_service_health(
    failure: _PlanningRunFailure,
    *,
    source_reason_code: str,
    model: str | None,
    service_healthy: bool | None,
) -> Any:
    details = failure.details if isinstance(failure.details, Mapping) else {}
    provider_recovery = details.get("provider_recovery")
    if not isinstance(provider_recovery, Mapping):
        provider_recovery = {}
    conformance_stall = details.get("conformance_stall")
    if not isinstance(conformance_stall, Mapping):
        conformance_stall = {}
    provider = _mapping_str(details, "provider") or _mapping_str(provider_recovery, "provider")
    classified_model = (
        _mapping_str(details, "model") or _mapping_str(provider_recovery, "model") or model
    )
    return classify_provider_failure(
        reason_code=source_reason_code,
        stdout="",
        stderr=_mapping_str(conformance_stall, "last_output_excerpt") or failure.message,
        provider=provider,
        model=classified_model,
        service_healthy=service_healthy,
    )


async def _restart_agent_service_or_mark_unhealthy(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError | ComposeExecCleanupError | _PlanningRunFailure,
    service_healthy: bool | None,
    restart_attempts: int,
    compose_up_timeout_seconds: int,
    source_reason_code: str | None = None,
    execution_owner_id: str | None = None,
    before_mark_failed: _BeforeMarkFailed | None = None,
    expected_status: WorkspaceStatus = WorkspaceStatus.running,
    failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
) -> tuple[int, bool]:
    if restart_attempts >= _AGENT_SERVICE_RESTART_ATTEMPTS:
        if not await self._recheck_status(
            workspace_id,
            expected=expected_status,
            action="agent_service_restart_terminal",
            owner_id=execution_owner_id,
        ):
            return restart_attempts, False
        await _mark_agent_service_unhealthy(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            source_reason_code=source_reason_code,
            message="agent compose service stayed unhealthy after restart attempts",
            before_mark_failed=before_mark_failed,
            from_status=failure_from_status,
        )
        return restart_attempts, False
    restart_attempts += 1
    try:
        await self._compose.ensure_project_up(
            project_name=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            wait=True,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
        )
    except ComposeOperationError as restart_exc:
        if not await self._recheck_status(
            workspace_id,
            expected=expected_status,
            action="agent_service_restart_terminal",
            owner_id=execution_owner_id,
        ):
            return restart_attempts, False
        await _mark_agent_service_unhealthy(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            source_reason_code=source_reason_code,
            message=f"agent compose service restart failed: {restart_exc!r}"[:2000],
            before_mark_failed=before_mark_failed,
            from_status=failure_from_status,
        )
        return restart_attempts, False
    if not await self._recheck_status(
        workspace_id,
        expected=expected_status,
        action="agent_service_restart_recovery",
        owner_id=execution_owner_id,
    ):
        await _run_before_mark_failed(before_mark_failed)
        return restart_attempts, False
    return restart_attempts, True


async def _mark_agent_service_unhealthy(
    self: Any,
    *,
    workspace_id: str,
    exc: AgentRunError | ComposeExecCleanupError | _PlanningRunFailure,
    service_healthy: bool | None,
    restart_attempts: int,
    message: str,
    source_reason_code: str | None = None,
    before_mark_failed: _BeforeMarkFailed | None = None,
    from_status: WorkspaceStatus = WorkspaceStatus.running,
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
        "source_reason_code": source_reason_code or exc.reason_code,
        "service_healthy": service_healthy,
        "restart_attempts": restart_attempts,
    }
    if before_mark_failed is not None:
        await _run_before_mark_failed(before_mark_failed)
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=from_status,
        failure_reason=FailureReason.infrastructure_failure,
        message=message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=details,
    )


async def _run_before_mark_failed(before_mark_failed: _BeforeMarkFailed | None) -> None:
    if before_mark_failed is None:
        return
    result = before_mark_failed()
    if isawaitable(result):
        await result


def _mapping_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _conformance_stall_timeout_source_reason_code(value: Any) -> str | None:
    if not isinstance(value, _PlanningRunFailure):
        return None
    if value.reason_code != AGENT_STALLED_IN_CONFORMANCE:
        return None
    details = value.details if isinstance(value.details, Mapping) else {}
    conformance_stall = details.get("conformance_stall")
    if not isinstance(conformance_stall, Mapping):
        return None
    source_reason_code = _mapping_str(conformance_stall, "source_reason_code")
    if source_reason_code not in _AGENT_SERVICE_TIMEOUT_REASON_CODES:
        return None
    return source_reason_code


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
