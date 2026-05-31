"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT
from awf.common.config import Settings, get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import (
    Task,
    TaskAttempt,
    Workspace,
)
from awf.db.repositories import (
    OperationRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import has_terminal_runtime_released_event
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    build_planning_scope_retry_prompt,
)
from awf.service.conformance_salvage import (
    CONFORMANCE_SALVAGE_POLICY_KEY,
    SALVAGE_NO_IMPLEMENTATION_DIFF,
    ConformanceSalvageError,
    build_agent_timeout_salvage_retry_prompt,
    build_conformance_salvage_retry_prompt,
    capture_conformance_salvage,
)
from awf.service.coordination import (
    owned_path_overlap_coordination_warnings,
    task_policy_with_coordination_warnings,
)
from awf.service.provider_readiness import (
    HttpGet,
    SubprocessRun,
)
from awf.service.scheduler import (
    SCHEDULER_POLICY_KEY,
    scheduler_retry_policy_context,
    scheduler_score_from_workspace,
)

if TYPE_CHECKING:
    from awf.service.workspaces import (
        _AgentTimeoutRetryContext,
        _ConformanceRetryContext,
        _PlanningScopeRetryContext,
    )


def _workspace_create() -> Any:
    """Import create helpers lazily to avoid module-load cycles."""
    from awf.service import workspaces_create

    return workspaces_create


def _workspace_service() -> Any:
    """Import workspace service symbols lazily to avoid module-level cycles."""
    from awf.service import workspaces

    return workspaces


def workspace_failure_details_payload(workspace: Workspace) -> dict[str, Any] | None:
    """Import the response helper lazily to avoid a module-load cycle."""
    from awf.service.workspaces_response import workspace_failure_details_payload as _payload

    return _payload(workspace)


async def _source_runtime_not_yet_released(
    session: AsyncSession,
    source: Workspace,
) -> bool:
    """Return True if the source workspace is in a terminal status but its
    compose runtime has not been released yet (no ``terminal_runtime_released``
    event).  In that state the source's host ports are still claimed and a
    retry would collide at Docker Compose time."""
    if WorkspaceStatus(source.status) not in (
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.completed,
        WorkspaceStatus.destroyed,
    ):
        return False
    if source.compose_project_name is None:
        return False
    return not await has_terminal_runtime_released_event(session, source.id)


async def retry_workspace_row(
    session: AsyncSession,
    workspace_id: str,
    *,
    provider_readiness_override: bool = False,
    provider_readiness_override_reason: str | None = None,
    settings: Settings | None = None,
    provider_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> Any:
    """Create a fresh requested workspace cloned from a failed/cancelled attempt."""
    workspaces = _workspace_service()
    workspaces_create = _workspace_create()
    resolved_settings = settings or get_settings()
    repo = WorkspaceRepository(session)
    source = await repo.get_for_update(workspace_id)
    if source is None:
        raise workspaces.WorkspaceRetryNotFoundError(workspace_id)

    if WorkspaceStatus(source.status) not in workspaces.RETRYABLE_WORKSPACE_STATUSES:
        raise workspaces.WorkspaceRetryNotAllowedError(source)

    planning_scope_context = _planning_scope_retry_context(source)
    conformance_context = (
        None if planning_scope_context is not None else _conformance_retry_context(source)
    )
    conformance_retry_requested = planning_scope_context is None and (
        conformance_context is not None or _is_plan_conformance_unsatisfied(source)
    )
    agent_timeout_context = (
        _agent_timeout_retry_context(source)
        if planning_scope_context is None and not conformance_retry_requested
        else None
    )
    conformance_evidence: Mapping[str, Any] = (
        conformance_context.evidence if conformance_context is not None else {}
    )

    if planning_scope_context is not None:
        retried_prompt = build_planning_scope_retry_prompt(
            task_prompt=source.task_prompt,
            evidence=planning_scope_context.evidence,
        )
    else:
        retried_prompt = source.task_prompt

    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        owned_paths=list(source.owned_paths),
        resolved_profile=source.resolved_profile,
        workspace_id=source.id,
    )
    retried_task_policy = _retry_task_policy(
        source,
        owned_path_overlap_coordination_warnings(overlaps),
        planning_scope_context=planning_scope_context,
    )
    preflight = await workspaces_create._selected_provider_preflight_for_task_async(
        resolved_settings,
        agent=source.agent,
        task_policy=retried_task_policy,
        override=provider_readiness_override,
        override_reason=provider_readiness_override_reason,
        provider_environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )
    preflight = {**preflight, "source_workspace_id": source.id}
    workspaces_create._raise_if_provider_preflight_blocks(preflight)
    conformance_salvage: dict[str, Any] | None = None
    salvage_recovery_payload: dict[str, Any] | None = None
    if conformance_retry_requested:
        try:
            salvage_capture = await asyncio.to_thread(
                capture_conformance_salvage,
                work_dir=resolved_settings.work_dir,
                source_workspace_id=source.id,
                source_base_commit=source.base_commit,
                conformance_evidence=conformance_evidence,
                conformance_evidence_ref=(
                    conformance_context.evidence_ref if conformance_context is not None else None
                ),
                source_branch_name=source.branch_name,
                source_remote_push_branch=source.remote_push_branch,
                run_subprocess=run_subprocess,
            )
        except ConformanceSalvageError as exc:
            raise workspaces.WorkspaceRetrySalvageUnavailableError(
                source,
                reason_code=exc.reason_code,
                message=str(exc),
                evidence=conformance_evidence,
                detail=exc.detail,
            ) from exc
        conformance_salvage = salvage_capture.as_policy()
        retried_task_policy[CONFORMANCE_SALVAGE_POLICY_KEY] = conformance_salvage
        retried_prompt = build_conformance_salvage_retry_prompt(
            task_prompt=source.task_prompt,
            evidence=conformance_evidence,
            salvage=conformance_salvage,
        )
        salvage_recovery_payload = _conformance_salvage_recovery_payload(
            conformance_context=conformance_context,
            salvage=conformance_salvage,
        )
    elif agent_timeout_context is not None:
        try:
            salvage_capture = await asyncio.to_thread(
                capture_conformance_salvage,
                work_dir=resolved_settings.work_dir,
                source_workspace_id=source.id,
                source_base_commit=source.base_commit,
                conformance_evidence=agent_timeout_context.evidence,
                conformance_evidence_ref=agent_timeout_context.evidence_ref,
                source_branch_name=source.branch_name,
                source_remote_push_branch=source.remote_push_branch,
                run_subprocess=run_subprocess,
            )
        except ConformanceSalvageError as exc:
            if exc.reason_code == SALVAGE_NO_IMPLEMENTATION_DIFF:
                workspaces._log.debug(
                    "workspace.agent_timeout_salvage_skipped_no_diff",
                    workspace_id=source.id,
                )
            else:
                workspaces._log.info(
                    "workspace.agent_timeout_salvage_unavailable",
                    workspace_id=source.id,
                    reason_code=exc.reason_code,
                    detail=exc.detail,
                )
                raise workspaces.WorkspaceRetrySalvageUnavailableError(
                    source,
                    source_reason_code=agent_timeout_context.reason_code,
                    reason_code=exc.reason_code,
                    message=str(exc),
                    evidence=agent_timeout_context.evidence,
                    detail=exc.detail,
                ) from exc
        else:
            conformance_salvage = {
                **salvage_capture.as_policy(),
                "salvage_kind": "agent_timeout",
            }
            retried_task_policy[CONFORMANCE_SALVAGE_POLICY_KEY] = conformance_salvage
            retried_prompt = build_agent_timeout_salvage_retry_prompt(
                task_prompt=source.task_prompt,
                evidence=agent_timeout_context.evidence,
                salvage=conformance_salvage,
            )
            salvage_recovery_payload = _agent_timeout_salvage_recovery_payload(
                context=agent_timeout_context,
                salvage=conformance_salvage,
            )
    retried_task_policy = {
        **retried_task_policy,
        "provider_readiness_preflight": preflight,
    }

    host_ports: list[int] = []
    host_ports.extend(
        workspaces.host_ports_from_task_policy_companions(
            source.task_policy,
        )
    )
    host_ports.extend(
        workspaces.host_ports_from_resolved_profile(source.resolved_profile),
    )
    if await _source_runtime_not_yet_released(session, source):
        raise workspaces.WorkspaceRetrySourceRuntimeNotReleasedError(
            source_workspace_id=source.id,
        )
    if host_ports:
        await repo.acquire_host_port_admission_lock(host_ports=host_ports)
        conflicts = await repo.find_host_port_conflicts(
            host_ports=host_ports,
            excluding_workspace_id=source.id,
            node_id=source.node_id,
        )
        if conflicts:
            raise workspaces.WorkspaceCreateHostPortConflictError(
                host_port=conflicts[0].host_port,
                conflicting_workspace_id=conflicts[0].workspace_id,
            )

    retried = await repo.create(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        task_title=source.task_title,
        task_prompt=retried_prompt,
        task_external_id=source.task_external_id,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
        task_policy=retried_task_policy,
        auto_merge=source.auto_merge,
        initial_review_grace_period_seconds=(source.initial_review_grace_period_seconds),
        agent=source.agent,
        env_profile=source.env_profile,
        profile_ref=source.profile_ref,
        requested_profile=deepcopy(source.requested_profile),
        resolved_profile=deepcopy(source.resolved_profile),
        test_commands=list(source.test_commands),
        requires_database=source.requires_database,
        idempotency_key=None,
        task_kind=source.task_kind,
        remote_push_branch=(
            source.remote_push_branch
            if planning_scope_context is None
            or source.task_kind in workspaces.PRESERVE_RETRY_REMOTE_PUSH_BRANCH_TASK_KINDS
            else None
        ),
    )

    attempt_repo = TaskAttemptRepository(session)
    source_attempt = await attempt_repo.get_by_workspace_id(source.id)
    task = await _retry_task_for_source(session, source, source_attempt=source_attempt)
    attempt = await attempt_repo.create_for_workspace(
        task=task,
        workspace=retried,
        parent_attempt_id=source_attempt.id if source_attempt is not None else None,
        redispatch_from_attempt_id=source_attempt.id if source_attempt is not None else None,
    )
    retry_policy = dict(retried.task_policy or {})
    retry_scheduler_value = retry_policy.get(SCHEDULER_POLICY_KEY)
    retry_scheduler_policy = (
        dict(retry_scheduler_value) if isinstance(retry_scheduler_value, Mapping) else {}
    )
    retry_scheduler_policy["retry_attempt_number"] = max(0, attempt.attempt_number - 1)
    retry_policy[SCHEDULER_POLICY_KEY] = retry_scheduler_policy
    retried.task_policy = retry_policy
    latest_source_reservation = await ResourceReservationRepository(session).list_for_workspace(
        source.id, limit=1
    )
    retry_resource_summary: dict[str, Any] = {}
    if latest_source_reservation:
        source_reservation = latest_source_reservation[0]
        retry_reservation = workspaces.ResourceReservationPlan(
            node_id=resolved_settings.worker_node_id or source_reservation.node_id,
            steady_cpu=resolved_settings.workspace_steady_cpu,
            steady_memory_gb=resolved_settings.workspace_steady_memory_gb,
            peak_cpu=resolved_settings.workspace_peak_cpu,
            peak_memory_gb=resolved_settings.workspace_peak_memory_gb,
            disk_mb=source_reservation.disk_mb,
            dind_slots=source_reservation.dind_slots,
            dind_mode="dind" if source_reservation.dind_slots else "none",
            phase=source_reservation.phase,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=retried.id,
            attempt_id=attempt.id,
            node_id=retry_reservation.node_id,
            steady_cpu=retry_reservation.steady_cpu,
            steady_memory_gb=retry_reservation.steady_memory_gb,
            peak_cpu=retry_reservation.peak_cpu,
            peak_memory_gb=retry_reservation.peak_memory_gb,
            disk_mb=retry_reservation.disk_mb,
            dind_slots=retry_reservation.dind_slots,
            phase=retry_reservation.phase,
        )
        retry_resource_summary = retry_reservation.summary(settings=resolved_settings)
    retry_score = scheduler_score_from_workspace(retried, now=retried.created_at)
    await QueueDecisionRepository(session).create(
        workspace_id=retried.id,
        task_id=task.id,
        attempt_id=attempt.id,
        decision=workspaces.QUEUE_DECISION_ADMITTED,
        reason_code=workspaces.QUEUE_DECISION_ADMITTED_LOCAL_REASON,
        class_priority=retry_score.class_priority,
        computed_priority=retry_score.effective_score,
        age_boost=retry_score.age_boost,
        retry_bonus=retry_score.retry_bonus,
        resource_summary=retry_resource_summary,
        overlap_risk_summary=workspaces_create.overlap_risk_summary(overlaps),
        score_summary=retry_score.score_summary,
    )

    operation_repo = OperationRepository(session)
    operation_payload: dict[str, Any] = {"source_workspace_id": source.id}
    if planning_scope_context is not None:
        operation_payload.update(_planning_scope_recovery_payload(planning_scope_context))
    if salvage_recovery_payload is not None:
        operation_payload.update(salvage_recovery_payload)
    operation = await operation_repo.create(
        workspace_id=retried.id,
        operation_type=OperationType.retry,
        status=OperationStatus.running,
        payload=operation_payload,
    )
    event_payload = {
        "source_workspace_id": source.id,
        "new_workspace_id": retried.id,
        "attempt_number": attempt.attempt_number,
        "provider_readiness_preflight": preflight,
    }
    if planning_scope_context is not None:
        event_payload.update(_planning_scope_recovery_payload(planning_scope_context))
    if salvage_recovery_payload is not None:
        event_payload.update(salvage_recovery_payload)
    await repo.add_event(
        source,
        event_type="workspace.retry_requested",
        reason_code="RETRY_REQUESTED",
        payload=event_payload,
    )
    await repo.add_event(
        retried,
        event_type="workspace.retry_created",
        reason_code="RETRY_CREATED",
        payload=event_payload,
    )
    await workspaces_create._record_provider_readiness_preflight(repo, retried, preflight)
    await workspaces_create._record_owned_path_overlap_risk(repo, retried, overlaps)
    await operation_repo.finish(
        operation,
        status=OperationStatus.succeeded,
        result={
            "new_workspace_id": retried.id,
            "attempt_number": attempt.attempt_number,
            "status": retried.status,
        }
        | (salvage_recovery_payload or {})
        | (
            _planning_scope_recovery_payload(planning_scope_context)
            if planning_scope_context is not None
            else {}
        ),
    )
    await session.flush()
    return workspaces.WorkspaceRetryResult(
        source_workspace_id=source.id,
        new_workspace=retried,
        operation=operation,
        attempt_number=attempt.attempt_number,
    )


def _retry_task_policy(
    source: Workspace,
    coordination_warnings: Sequence[Mapping[str, Any]],
    *,
    planning_scope_context: _PlanningScopeRetryContext | None,
) -> dict[str, Any]:
    """Build the task policy dict for a retried workspace."""
    policy = task_policy_with_coordination_warnings(
        scheduler_retry_policy_context(
            deepcopy(source.task_policy),
            source_workspace_id=source.id,
            parent_failure_reason=source.failure_reason,
        ),
        coordination_warnings,
    )
    if planning_scope_context is not None and planning_scope_context.fallback_model is not None:
        policy["agent_model"] = planning_scope_context.fallback_model["model"]
    return policy


def _planning_scope_recovery_payload(
    context: _PlanningScopeRetryContext,
) -> dict[str, Any]:
    """Build the planning-scope recovery payload dict from a retry context."""
    payload: dict[str, Any] = {
        "source_reason_code": context.reason_code,
        "planning_scope_evidence_ref": context.evidence_ref,
        "recovery_strategy": context.recovery_strategy,
        "salvage_policy": context.salvage_policy,
    }
    if context.salvage is not None:
        payload["salvage"] = context.salvage
    if context.fallback_model is not None:
        payload["fallback_model"] = context.fallback_model
    return payload


def _conformance_salvage_recovery_payload(
    *,
    conformance_context: _ConformanceRetryContext | None,
    salvage: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the conformance-salvage recovery payload dict for a retry."""
    payload: dict[str, Any] = {
        "source_reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        "conformance_salvage": dict(salvage),
    }
    remaining_gaps = _compact_string_list(salvage.get("remaining_gaps"))
    if remaining_gaps:
        payload["remaining_gaps"] = remaining_gaps
    if conformance_context is not None:
        payload["conformance_evidence_ref"] = conformance_context.evidence_ref
    elif salvage.get("conformance_evidence_ref") is not None:
        payload["conformance_evidence_ref"] = salvage.get("conformance_evidence_ref")
    return payload


def _agent_timeout_salvage_recovery_payload(
    *,
    context: _AgentTimeoutRetryContext,
    salvage: Mapping[str, Any],
) -> dict[str, Any]:
    """Build retry payload metadata for an agent-timeout salvage continuation."""
    payload: dict[str, Any] = {
        "source_reason_code": context.reason_code,
        "recovery_strategy": "continue_from_timeout_salvage",
        "conformance_salvage": dict(salvage),
        "agent_timeout_evidence_ref": context.evidence_ref,
    }
    message = _optional_retry_evidence_str(context.evidence.get("message"))
    if message is not None:
        payload["source_failure_message"] = message
    return payload


async def _retry_task_for_source(
    session: AsyncSession,
    source: Workspace,
    *,
    source_attempt: TaskAttempt | None = None,
) -> Task:
    """Retrieve or create the task associated with a source workspace for retry."""
    if source_attempt is None:
        source_attempt = await TaskAttemptRepository(session).get_by_workspace_id(source.id)
    if source_attempt is not None:
        task = await TaskRepository(session).get(source_attempt.task_id)
        if task is not None:
            return task

    fallback_idempotency_key = f"retry-source-workspace:{source.id}"
    return await TaskRepository(session).create_or_get(
        repo_url=source.repo_url,
        base_branch=source.branch_base,
        title=source.task_title,
        prompt=source.task_prompt,
        external_id=source.task_external_id,
        idempotency_key=fallback_idempotency_key,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
    )


def _latest_failed_state_event(workspace: Workspace) -> Any | None:
    """Return the most recent workspace state-changed event with a failed status, or None."""
    for event in reversed(getattr(workspace, "events", []) or []):
        if (
            getattr(event, "event_type", None) == "workspace.state_changed"
            and getattr(event, "new_state", None) == WorkspaceStatus.failed.value
        ):
            return event
    return None


def _compact_conformance_payload(value: object) -> dict[str, Any] | None:
    """Extract a compact conformance payload with only relevant string and integer fields."""
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "summary",
        "reason_code",
        "report_reason_code",
        "plan_path",
        "report_path",
    ):
        item = value.get(key)
        if isinstance(item, str):
            payload[key] = item
    gaps = value.get("gaps")
    if isinstance(gaps, list):
        payload["gaps"] = [gap for gap in gaps if isinstance(gap, str)]
    for key in ("iterations_used", "max_iterations"):
        item = value.get(key)
        if isinstance(item, int):
            payload[key] = item
    return payload or None


def _compact_planning_scope_payload(value: object) -> dict[str, Any] | None:
    """Extract a compact planning-scope payload with only relevant string and list fields."""
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "scope_phase",
        "recommended_action",
        "recovery_strategy",
        "salvage_policy",
        "plan_artifact",
    ):
        item = value.get(key)
        if isinstance(item, str) and item:
            payload[key] = item
    for key in ("required_paths", "offending_paths", "offending_commands"):
        items = _compact_string_list(value.get(key))
        if items:
            payload[key] = items
    if "offending_paths" not in payload:
        forbidden = _compact_string_list(value.get("forbidden_paths"))
        if forbidden:
            payload["offending_paths"] = forbidden
    fallback_model = _compact_fallback_model(value.get("fallback_model"))
    if fallback_model is not None:
        payload["fallback_model"] = fallback_model
    return payload or None


def _compact_string_list(value: object) -> list[str]:
    """Filter a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _compact_fallback_model(value: object) -> dict[str, str] | None:
    """Extract a compact fallback model dict with model name and optional source."""
    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    source = value.get("source")
    if not isinstance(model, str) or not model.strip():
        return None
    payload = {"model": model.strip()}
    if isinstance(source, str) and source.strip():
        payload["source"] = source.strip()
    return payload


def _compact_salvage_payload(value: object) -> dict[str, str] | None:
    """Extract a compact salvage payload with hint, worktree, branch, and remote-push fields."""
    if not isinstance(value, Mapping):
        return None
    payload = {
        key: item
        for key in ("hint", "worktree_path", "branch_name", "remote_push_branch")
        if isinstance((item := value.get(key)), str) and item
    }
    return payload or None


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    """Return a string value from a payload dict by key, or None if not a string."""
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _is_plan_conformance_unsatisfied(workspace: Workspace) -> bool:
    """Check whether the workspace's latest failure is a plan-conformance-unsatisfied reason."""
    details = workspace_failure_details_payload(workspace)
    if details is None:
        return False
    return details.get("reason_code") == PLAN_CONFORMANCE_UNSATISFIED


def _agent_timeout_retry_context(workspace: Workspace) -> Any:
    """Build a timeout retry context from the workspace's failure details if applicable."""
    workspaces = _workspace_service()
    details = workspace_failure_details_payload(workspace)
    if details is None:
        return None
    reason_code = details.get("reason_code")
    if reason_code not in {AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT}:
        return None
    message = _optional_retry_evidence_str(details.get("message"))
    evidence: dict[str, Any] = {
        "reason_code": reason_code,
        "gaps": [
            "The previous agent run timed out before it could finish.",
            "Continue from the recovered implementation diff and complete the original task.",
        ],
    }
    if message is not None:
        evidence["message"] = message
    return workspaces._AgentTimeoutRetryContext(
        reason_code=str(reason_code),
        evidence=evidence,
        evidence_ref={
            "source_workspace_id": workspace.id,
            "event_type": "workspace.state_changed",
            "reason_code": str(reason_code),
        },
    )


def _conformance_retry_context(workspace: Workspace) -> Any:
    """Build a conformance retry context from the workspace's failure details if applicable."""
    workspaces = _workspace_service()
    details = workspace_failure_details_payload(workspace)
    if details is None or details.get("reason_code") != PLAN_CONFORMANCE_UNSATISFIED:
        return None
    evidence = details.get("conformance")
    if not isinstance(evidence, Mapping):
        return None
    return workspaces._ConformanceRetryContext(
        reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        evidence=evidence,
        evidence_ref={
            "source_workspace_id": workspace.id,
            "event_type": "workspace.state_changed",
            "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        },
    )


def _retry_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    """Extract a list of non-empty evidence gap strings from conformance evidence."""
    value = evidence.get("gaps")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _optional_retry_evidence_str(value: object) -> str | None:
    """Return a stripped non-empty string from a value, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _planning_scope_retry_context(workspace: Workspace) -> Any:
    """Build a planning-scope retry context from the workspace's failure details if applicable."""
    workspaces = _workspace_service()
    details = workspace_failure_details_payload(workspace)
    if details is None or details.get("reason_code") != AGENT_PLAN_PHASE_SCOPE_VIOLATION:
        return None
    evidence = details.get("planning_scope")
    if not isinstance(evidence, Mapping):
        return None
    recovery_strategy_value = details.get("recovery_strategy")
    recovery_strategy = (
        recovery_strategy_value
        if isinstance(recovery_strategy_value, str)
        else "discard_and_replan"
    )
    salvage_policy_value = details.get("salvage_policy")
    salvage_policy = (
        salvage_policy_value
        if isinstance(salvage_policy_value, str)
        else "explicit_salvage_required"
    )
    fallback_model = workspaces._approved_planning_scope_fallback_model(workspace)
    evidence_payload = dict(evidence)
    if fallback_model is not None:
        evidence_payload["fallback_model"] = fallback_model
    return workspaces._PlanningScopeRetryContext(
        reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        evidence=evidence_payload,
        evidence_ref={
            "source_workspace_id": workspace.id,
            "event_type": "workspace.state_changed",
            "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        },
        recovery_strategy=recovery_strategy,
        salvage_policy=salvage_policy,
        salvage=_compact_salvage_payload(details.get("salvage")),
        fallback_model=fallback_model,
    )


def _approved_planning_scope_fallback_model(
    workspace: Workspace,
) -> dict[str, str] | None:
    """Return the approved fallback model from the workspace's planning-scope recovery policy."""
    task_policy = getattr(workspace, "task_policy", None)
    if not isinstance(task_policy, Mapping):
        return None
    recovery_policy = task_policy.get("planning_scope_recovery")
    if not isinstance(recovery_policy, Mapping):
        return None
    model = recovery_policy.get("approved_fallback_model")
    if not isinstance(model, str) or not model.strip():
        return None
    return {
        "model": model.strip(),
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
