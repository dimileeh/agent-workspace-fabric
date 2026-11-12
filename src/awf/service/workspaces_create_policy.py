"""Create-time task policy and frozen primary/fallback execution snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.auto_merge import AUTO_MERGE_INTENT_POLICY_KEY
from awf.common.companions import COMPANION_POLICY_KEY
from awf.common.config import DEFAULT_OPENROUTER_WORKSPACE_LIMIT_USD
from awf.common.workspace_policy import (
    CURSOR_AUTO_MODE_POLICY_KEY,
    DEFAULT_RELEASE_SYNC_SOURCE_BRANCH,
    EXPECTED_BASE_SHA_POLICY_KEY,
    canonical_agent_model_for_cursor_auto,
)
from awf.db.enums import TaskKind
from awf.hosted_node.runtime_schemas import POLICY_KEY
from awf.service.scheduler import scheduler_policy_snapshot


def workspace_create_task_policy_snapshot(payload: WorkspaceCreateRequest) -> dict[str, Any]:
    """Build a task policy dictionary from a rich create request."""
    from awf.service.workspaces import (  # noqa: E402
        RELEASE_SYNC_POLICY_KEY,
        RESOURCE_RESERVATION_REQUEST_POLICY_KEY,
        VALIDATION_POLICY_KEY,
        VALIDATION_REQUESTED_TIER_POLICY_KEY,
    )
    from awf.service.workspaces_create import (
        _requested_companions,
        _requested_resource_reservation_values,
    )

    policy: dict[str, Any] = {}
    if payload.hosted is not None:
        policy[POLICY_KEY] = payload.hosted.model_dump(mode="json")
    resource_request = _requested_resource_reservation_values(payload)
    # Persist empty requests so idempotent replays do not re-plan against
    # mutable default resource settings.
    policy[RESOURCE_RESERVATION_REQUEST_POLICY_KEY] = resource_request
    policy[VALIDATION_POLICY_KEY] = {
        VALIDATION_REQUESTED_TIER_POLICY_KEY: payload.validation.requested_tier
    }
    # Persist the raw tri-state auto-merge intent (True/False/None) durably so the
    # provisioner resolves the final flag from it and idempotent replays compare a
    # stable snapshot. Always write the key (even for None) to keep the snapshot
    # stable across replays.
    policy[AUTO_MERGE_INTENT_POLICY_KEY] = payload.task.auto_merge
    if payload.repo.expected_base_sha is not None:
        policy[EXPECTED_BASE_SHA_POLICY_KEY] = payload.repo.expected_base_sha
    if payload.task.kind == TaskKind.sync_release_pr.value:
        policy[RELEASE_SYNC_POLICY_KEY] = {
            "source_branch": payload.repo.source_branch or DEFAULT_RELEASE_SYNC_SOURCE_BRANCH,
            "target_branch": payload.repo.base_branch,
        }
    if payload.task.priority != 0 or payload.task.human_boost != 0:
        policy["scheduler"] = scheduler_policy_snapshot(
            base_priority=payload.task.priority,
            human_boost=payload.task.human_boost,
        )
    if payload.task.model is not None:
        effective_model = canonical_agent_model_for_cursor_auto(
            model=payload.task.model,
            cursor_auto_mode=payload.task.cursor_auto_mode,
        )
        if effective_model is not None:
            policy["agent_model"] = effective_model
    if payload.task.effort is not None:
        policy["agent_effort"] = payload.task.effort
    if payload.task.cursor_auto_mode is not None:
        policy[CURSOR_AUTO_MODE_POLICY_KEY] = payload.task.cursor_auto_mode.value
    if payload.task.out_of_scope_changes is not None:
        policy["out_of_scope_changes"] = payload.task.out_of_scope_changes.model_dump(mode="json")
    if payload.task.provider_recovery is not None:
        policy["provider_recovery"] = payload.task.provider_recovery.model_dump(
            mode="json",
            exclude_none=True,
            exclude_unset=True,
        )
    policy[COMPANION_POLICY_KEY] = _requested_companions(payload)
    return policy


def freeze_execution_policy(
    payload: WorkspaceCreateRequest,
    *,
    profile: Mapping[str, Any] | None,
    enabled: bool,
    environ: Mapping[str, str],
    settings: object = None,
    hosted: bool = False,
) -> dict[str, Any]:
    """Freeze the primary and every declared fallback using one resolved profile.

    With managed OpenRouter keys configured, every OpenRouter target uses the
    fixed managed credential reference and the workspace budget is frozen once,
    beside the selections, in ``provider_budget``.
    """
    from awf.common.agent_execution import (
        PROVIDER_BUDGET_POLICY_KEY,
        provider_budget_policy,
        resolve_execution,
    )

    managed_openrouter = getattr(settings, "openrouter_management_key", None) is not None
    default_budget_usd = getattr(
        settings, "openrouter_workspace_limit_usd", DEFAULT_OPENROUTER_WORKSPACE_LIMIT_USD
    )

    policy = workspace_create_task_policy_snapshot(payload)
    from awf.common.workspace_policy import cursor_auto_model_selector

    selected_model = (
        cursor_auto_model_selector(payload.task.cursor_auto_mode)
        if payload.task.cursor_auto_mode is not None
        else payload.task.model
    )
    targets = [
        (payload.task.agent.value, payload.task.provider, selected_model, payload.task.effort)
    ]
    if payload.task.provider_recovery:
        targets.extend(
            (str(t.agent), t.provider, t.model, None)
            for t in payload.task.provider_recovery.fallbacks
        )
    snapshots = []
    for agent, provider, model, effort in targets:
        native = {"codex": "openai", "claude_code": "anthropic"}.get(agent)
        if native and provider is not None and provider != native and not enabled:
            raise ValueError("THIRD_PARTY_AGENT_PROVIDER_ADMISSION_DISABLED")
        snapshots.append(
            resolve_execution(
                agent=agent,
                provider=provider,
                model=model,
                effort=effort,
                allow_unverified=True,
                profile=profile,
                environ=environ if profile is not None else {},
                managed_openrouter=managed_openrouter,
            )
        )
    managed = any(s.managed_key for s in snapshots)
    if managed and hosted:
        raise ValueError("PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED")
    requested_budget = payload.task.provider_budget_usd
    if requested_budget is not None and not managed:
        raise ValueError("PROVIDER_BUDGET_UNSUPPORTED")
    if managed:
        policy[PROVIDER_BUDGET_POLICY_KEY] = provider_budget_policy(
            limit_usd=requested_budget if requested_budget is not None else default_budget_usd,
            source="task" if requested_budget is not None else "default",
            requested_usd=requested_budget,
        )
    if not payload.preflight.allow_unverified_model and any(s.third_party for s in snapshots):
        policy["require_compatibility_evidence"] = True
    if any(not s.third_party for s in snapshots):
        policy["agent_execution_defaults_pending"] = True
    if profile is None and any(s.third_party for s in snapshots):
        policy["agent_execution_profile_pending"] = True
    if profile is not None or not snapshots[0].third_party:
        policy["agent_execution"] = snapshots[0].model_dump(mode="json")
    if payload.task.provider is not None:
        policy["agent_provider"] = payload.task.provider
    if payload.preflight.allow_unverified_model:
        policy["allow_unverified_model"] = True
    for target, snapshot in zip(
        policy.get("provider_recovery", {}).get("fallbacks", []), snapshots[1:], strict=True
    ):
        if profile is not None or not snapshot.third_party:
            target["agent_execution"] = snapshot.model_dump(mode="json")
    return policy


def freeze_diagnostic_policy(policy: dict[str, Any], diagnostic_spec: Mapping[str, Any]) -> None:
    """Freeze a diagnostic's selection; a managed one runs under a small fixed budget."""
    from awf.common.agent_execution import (
        DIAGNOSTIC_PROVIDER_BUDGET_USD,
        MANAGED_OPENROUTER_CREDENTIAL_REF,
        PROVIDER_BUDGET_POLICY_KEY,
        provider_budget_policy,
    )

    policy["agent_execution"] = diagnostic_spec["selection"]
    policy["provider_diagnostic"] = {"request": diagnostic_spec}
    policy.pop(PROVIDER_BUDGET_POLICY_KEY, None)
    if diagnostic_spec["selection"]["credential_ref"] == MANAGED_OPENROUTER_CREDENTIAL_REF:
        policy[PROVIDER_BUDGET_POLICY_KEY] = provider_budget_policy(
            limit_usd=DIAGNOSTIC_PROVIDER_BUDGET_USD, source="diagnostic"
        )


def required_worker_contract_version(policy: Mapping[str, Any]) -> int:
    """Live queue consumers must mint managed keys (3) or run expanded work (2)."""
    from awf.common.agent_execution import (
        MANAGED_PROVIDER_KEY_WORKER_VERSION,
        managed_openrouter_frozen,
    )

    return MANAGED_PROVIDER_KEY_WORKER_VERSION if managed_openrouter_frozen(policy) else 2


def provider_budget_request_matches(
    stored_policy: Mapping[str, Any], payload: WorkspaceCreateRequest
) -> bool:
    """Idempotent replays compare the requested budget, not later operator raises."""
    stored = stored_policy.get("provider_budget") or {}
    return stored.get("requested_usd") == payload.task.provider_budget_usd
