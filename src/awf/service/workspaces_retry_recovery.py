"""Recovery/policy helpers for workspace retry flows.

Mechanically extracted from ``awf.service.workspaces_retry`` so that module stays
under the first-party line-count guardrail. Retry-row orchestration remains in
``workspaces_retry``; this module owns policy pruning, recovery payloads, and
failure-context builders. Re-exported from ``workspaces_retry`` for import
compatibility and test monkeypatches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT
from awf.db.models import Task, TaskAttempt, Workspace
from awf.db.repositories import TaskAttemptRepository, TaskRepository
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.service.coordination import task_policy_with_coordination_warnings
from awf.service.scheduler import scheduler_retry_policy_context
from awf.service.workspaces_retry_payloads import (
    _compact_salvage_payload,
    _compact_string_list,
    _optional_retry_evidence_str,
)

if TYPE_CHECKING:
    from awf.service.workspaces import (
        _AgentTimeoutRetryContext,
        _ConformanceRetryContext,
        _PlanningScopeRetryContext,
    )


def _workspace_service() -> Any:
    """Import workspace service symbols lazily to avoid module-level cycles."""
    from awf.service import workspaces

    return workspaces


def workspace_failure_details_payload(workspace: Workspace) -> dict[str, Any] | None:
    """Import the response helper lazily to avoid a module-load cycle."""
    from awf.service.workspaces_response import workspace_failure_details_payload as _payload

    return _payload(workspace)


def _prune_and_migrate_retired_agent(
    policy: dict[str, Any],
    current_agent: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Prune retired or unsupported fallback entries from a cloned retry policy,
    and promote a launchable fallback if current_agent is retired.

    Replacing retired slots with None placeholders preserves fallback attempt indexes.
    If current_agent is retired/unlaunchable and a remaining approved launchable
    fallback exists (respecting provider_recovery_state and max_fallback_attempts),
    promotes that fallback as the new primary agent, updates agent_model, and sets
    its slot in fallbacks to None.
    """
    recovery = policy.get("provider_recovery")
    if not isinstance(recovery, Mapping):
        return policy, current_agent
    raw_fallbacks = recovery.get("fallbacks")
    if not isinstance(raw_fallbacks, Sequence) or isinstance(raw_fallbacks, str):
        return policy, current_agent

    from awf.service.provider_readiness import is_launchable_agent

    is_primary_launchable = True if current_agent is None else is_launchable_agent(current_agent)
    promoted_index: int | None = None
    target_agent = current_agent

    if not is_primary_launchable:
        from awf.service.provider_recovery import (
            PROVIDER_RECOVERY_STATE_KEY,
            _select_fallback_target_with_index,
            parse_provider_recovery_policy,
            parse_provider_recovery_state,
        )

        rec_policy = parse_provider_recovery_policy(policy)
        rec_state = parse_provider_recovery_state(policy)
        fallback_target, target_index = _select_fallback_target_with_index(rec_policy, rec_state)
        if fallback_target is not None:
            promoted_index = target_index
            target_agent = fallback_target.agent
            policy["agent_model"] = fallback_target.model

            raw_state = policy.get(PROVIDER_RECOVERY_STATE_KEY)
            state_dict = dict(raw_state) if isinstance(raw_state, Mapping) else {}
            state_dict["fallback_attempt_number"] = target_index + 1
            state_dict["launched_fallback_attempts"] = rec_state.launched_fallback_attempts + 1
            state_dict["retry_attempt_number"] = 0
            policy[PROVIDER_RECOVERY_STATE_KEY] = state_dict

    pruned: list[Any] = []
    for idx, item in enumerate(raw_fallbacks):
        if idx == promoted_index:
            pruned.append(None)
        elif isinstance(item, Mapping):
            fb_agent = item.get("agent")
            if fb_agent is not None and is_launchable_agent(fb_agent):
                pruned.append(item)
            else:
                pruned.append(None)
        else:
            pruned.append(None)

    updated_recovery = dict(recovery)
    updated_recovery["fallbacks"] = pruned
    policy["provider_recovery"] = updated_recovery
    return policy, target_agent


def _prune_retired_fallbacks(policy: dict[str, Any]) -> dict[str, Any]:
    """Prune retired or unsupported fallback entries from a cloned retry policy."""
    pruned_policy, _ = _prune_and_migrate_retired_agent(policy, current_agent=None)
    return pruned_policy


def _retry_task_policy(
    source: Workspace,
    coordination_warnings: Sequence[Mapping[str, Any]],
    *,
    planning_scope_context: _PlanningScopeRetryContext | None,
) -> tuple[dict[str, Any], str]:
    """Build the task policy dict and target agent for a retried workspace."""
    policy = task_policy_with_coordination_warnings(
        scheduler_retry_policy_context(
            deepcopy(source.task_policy),
            source_workspace_id=source.id,
            parent_failure_reason=source.failure_reason,
        ),
        coordination_warnings,
    )
    policy, target_agent = _prune_and_migrate_retired_agent(policy, current_agent=source.agent)
    effective_agent = target_agent or source.agent
    if (
        planning_scope_context is not None
        and planning_scope_context.fallback_model is not None
        and effective_agent == source.agent
    ):
        # Same mutual exclusion as provider recovery: a fixed fallback must
        # clear retained Cursor Auto mode or executor helpers keep preferring
        # auto-smart[...] and silently ignore the approved pin.
        from awf.service.provider_recovery import _install_fixed_recovery_model

        policy = _install_fixed_recovery_model(
            policy,
            planning_scope_context.fallback_model["model"],
        )
    return policy, effective_agent


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
