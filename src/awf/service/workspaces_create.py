"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    PUBLIC_DIRECT_CREATE_TASK_KINDS,
    OwnedPathOverlapResponse,
    WorkspaceCreateRequest,
    WorkspaceWarningResponse,
)
from awf.api.schemas_companions import WorkspaceCompanionRequest
from awf.common.companions import COMPANION_POLICY_KEY
from awf.common.config import Settings, get_settings
from awf.common.logging import get_logger
from awf.common.workspace_policy import DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
from awf.db.enums import AgentRuntime, TaskKind
from awf.db.models import (
    ResourceReservation,
    Workspace,
)
from awf.db.repositories import (
    OwnedPathOverlap,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.service.config import resolve_service_settings
from awf.service.coordination import (
    owned_path_overlap_coordination_warnings,
    task_policy_with_coordination_warnings,
)
from awf.service.disk import DiskCheck
from awf.service.provider_readiness import (
    HttpGet,
    SubprocessRun,
    provider_readiness_preflight_from_task_policy,
    selected_provider_readiness_preflight,
)
from awf.service.resource_capacity import (
    ReservedResources,
)
from awf.service.scheduler import (
    SCHEDULER_POLICY_KEY,
    scheduler_policy_snapshot,
    scheduler_score_from_workspace,
    task_class_bias,
)
from awf.service.scheduler import (
    task_class_priority as scheduler_task_class_priority,
)

if TYPE_CHECKING:
    from awf.service.workspaces import ResourceReservationPlan

_log = get_logger(__name__)


async def create_workspace_row(
    session: AsyncSession,
    payload: WorkspaceCreateRequest,
    *,
    idempotency_key: str | None = None,
    settings: Settings | None = None,
    disk_check: DiskCheck | None = None,
    provider_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> Workspace:
    """Persist one rich workspace create request without committing the session."""
    _assert_supported_direct_create_task_kind(payload.task.kind)
    resolved_settings = settings or get_settings()
    repo = WorkspaceRepository(session)
    base_task_policy = workspace_create_task_policy_snapshot(payload)
    requested_profile, resolved_profile = workspace_create_profile_snapshots(payload)
    preflight = await _selected_provider_preflight_for_task_async(
        resolved_settings,
        agent=payload.task.agent,
        task_policy=base_task_policy,
        override=payload.preflight.provider_readiness_override,
        override_reason=payload.preflight.provider_readiness_override_reason,
        provider_environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )
    _raise_if_provider_preflight_blocks(preflight)
    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        owned_paths=payload.task.owned_paths,
    )
    task_policy = task_policy_with_coordination_warnings(
        {
            **base_task_policy,
            "provider_readiness_preflight": preflight,
        },
        owned_path_overlap_coordination_warnings(overlaps),
    )

    ws = await repo.create(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        task_title=payload.task.title,
        task_prompt=payload.task.prompt,
        task_external_id=payload.task.external_id,
        task_class=(payload.task.task_class.value if payload.task.task_class is not None else None),
        owned_paths=payload.task.owned_paths,
        task_policy=task_policy,
        auto_merge=_effective_auto_merge(payload),
        initial_review_grace_period_seconds=(payload.task.initial_review_grace_period_seconds),
        agent=payload.task.agent.value,
        env_profile=None,
        profile_ref=payload.workspace.profile_ref,
        requested_profile=requested_profile,
        resolved_profile=resolved_profile,
        test_commands=payload.validation.commands,
        requires_database=payload.requires_database,
        idempotency_key=idempotency_key,
        task_kind=payload.task.kind,
    )
    task = await TaskRepository(session).create_or_get(
        repo_url=payload.repo.url,
        base_branch=payload.repo.base_branch,
        title=payload.task.title,
        prompt=payload.task.prompt,
        external_id=payload.task.external_id,
        idempotency_key=idempotency_key,
        task_class=(payload.task.task_class.value if payload.task.task_class is not None else None),
        owned_paths=payload.task.owned_paths,
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=ws)
    reservation_plan = resource_reservation_plan(payload, settings=resolved_settings)
    resource_summary = await _resource_reservation_summary(
        session,
        reservation_plan,
        settings=resolved_settings,
        disk_check=disk_check,
    )
    await ResourceReservationRepository(session).create(
        workspace_id=ws.id,
        attempt_id=attempt.id,
        node_id=reservation_plan.node_id,
        steady_cpu=reservation_plan.steady_cpu,
        steady_memory_gb=reservation_plan.steady_memory_gb,
        peak_cpu=reservation_plan.peak_cpu,
        peak_memory_gb=reservation_plan.peak_memory_gb,
        disk_mb=reservation_plan.disk_mb,
        dind_slots=reservation_plan.dind_slots,
        phase=reservation_plan.phase,
    )
    scheduler_score = scheduler_score_from_workspace(ws, now=ws.created_at)
    from awf.service.workspaces import (  # noqa: E402
        QUEUE_DECISION_ADMITTED,
        QUEUE_DECISION_ADMITTED_LOCAL_REASON,
    )

    await QueueDecisionRepository(session).create(
        workspace_id=ws.id,
        task_id=task.id,
        attempt_id=attempt.id,
        decision=QUEUE_DECISION_ADMITTED,
        reason_code=QUEUE_DECISION_ADMITTED_LOCAL_REASON,
        class_priority=scheduler_score.class_priority,
        computed_priority=scheduler_score.effective_score,
        age_boost=scheduler_score.age_boost,
        retry_bonus=scheduler_score.retry_bonus,
        resource_summary=resource_summary,
        overlap_risk_summary=overlap_risk_summary(overlaps),
        score_summary=scheduler_score.score_summary,
    )
    await _record_provider_readiness_preflight(repo, ws, preflight)
    await _record_owned_path_overlap_risk(repo, ws, overlaps)
    await session.flush()
    return ws


def workspace_create_payload_matches(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
    *,
    settings: Settings | None = None,
) -> bool:
    """Compare user-authored create fields for idempotent replay."""
    # Lifecycle status/subphase are intentionally excluded: create idempotency
    # replays the existing workspace in whatever state it has reached.
    requested_profile = (
        payload.workspace.profile.model_dump(mode="json", by_alias=True)
        if payload.workspace.profile is not None
        else None
    )
    task_class = payload.task.task_class.value if payload.task.task_class is not None else None
    stored_task_kind = getattr(existing, "task_kind", None)
    if stored_task_kind is None:
        stored_task_kind = "feature_branch_pr"
    return (
        existing.repo_url == payload.repo.url
        and existing.branch_base == payload.repo.base_branch
        and existing.task_title == payload.task.title
        and existing.task_prompt == payload.task.prompt
        and existing.task_external_id == payload.task.external_id
        and getattr(existing, "task_class", None) == task_class
        and _owned_path_hints_match(
            cast(Sequence[str], getattr(existing, "owned_paths", None) or []),
            payload.task.owned_paths,
        )
        and _stored_task_agent_model(existing) == payload.task.model
        and _stored_task_out_of_scope_policy(existing)
        == _requested_task_out_of_scope_policy(payload)
        and _stored_task_provider_recovery_policy(existing)
        == _requested_task_provider_recovery_policy(payload)
        and _stored_companions(existing) == _requested_companions(payload)
        and _stored_task_agent_effort(existing) == payload.task.effort
        and _stored_task_scheduler_policy(existing) == _requested_task_scheduler_policy(payload)
        and _stored_auto_merge_matches(existing, payload)
        and _release_sync_source_branch_matches(existing, payload)
        and (
            getattr(existing, "initial_review_grace_period_seconds", None)
            == payload.task.initial_review_grace_period_seconds
        )
        and existing.agent == payload.task.agent.value
        and stored_task_kind == payload.task.kind
        and _profile_ref_matches(existing, payload)
        and getattr(existing, "requested_profile", None) == requested_profile
        and _stored_validation_requested_tier_matches(existing, payload)
        and list(existing.test_commands) == list(payload.validation.commands)
        and _stored_resource_reservation_matches(existing, payload, settings=settings)
        and _task_provider_readiness_override_matches(existing, payload)
    )


def _stored_auto_merge_matches(existing: Workspace, payload: WorkspaceCreateRequest) -> bool:
    """Check stored auto-merge intent, treating legacy NULL as the old default."""
    if payload.task.kind == TaskKind.sync_release_pr.value:
        # Release-PR syncs force auto_merge off at persistence and execution, so
        # the stored value carries no idempotency signal. Rows written before
        # that canonicalization snapshotted the raw request (default True), and
        # comparing them against the effective False would spuriously conflict on
        # an otherwise identical replay. The kind itself is matched separately.
        return True
    stored = getattr(existing, "auto_merge", None)
    return (stored is not False) == _effective_auto_merge(payload)


def _release_sync_source_branch_matches(
    existing: Workspace, payload: WorkspaceCreateRequest
) -> bool:
    """Check the stored release-sync source branch for sync_release_pr replays.

    ``repo.source_branch`` selects which branch the release PR syncs, so a replay
    that changes it must conflict instead of silently reusing the original
    workspace and syncing the wrong branch. Other task kinds ignore the field.
    """
    if payload.task.kind != TaskKind.sync_release_pr.value:
        return True
    requested = payload.repo.source_branch or DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
    from awf.service.workspaces import RELEASE_SYNC_POLICY_KEY  # noqa: E402

    release_sync = _stored_task_policy(existing).get(RELEASE_SYNC_POLICY_KEY)
    stored = release_sync.get("source_branch") if isinstance(release_sync, Mapping) else None
    if stored is None:
        # Legacy rows predate the source_branch snapshot (added with the input
        # itself), so they could only have synced the default; treat a missing
        # field as the historical default to keep identical replays idempotent.
        stored = DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
    return stored == requested


def _profile_ref_matches(existing: Workspace, payload: WorkspaceCreateRequest) -> bool:
    """Check whether a stored workspace's profile reference matches a rich create request."""
    existing_profile_ref = getattr(existing, "profile_ref", None)
    if existing_profile_ref is not None:
        return bool(existing_profile_ref == payload.workspace.profile_ref)
    if _legacy_database_profile_ref_matches(existing, payload):
        return True
    legacy_env_profile = getattr(existing, "env_profile", None)
    if legacy_env_profile is not None:
        return _legacy_env_profile_ref_matches(existing, payload)
    if payload.workspace.profile_ref not in (None, "auto"):
        return False
    return not (
        payload.workspace.profile_ref == "auto"
        and (
            getattr(existing, "requested_profile", None) is not None
            or payload.workspace.profile is not None
        )
    )


def _legacy_env_profile_ref_matches(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
) -> bool:
    """Check whether a legacy env_profile row matches the requested profile ref."""
    legacy_env_profile = getattr(existing, "env_profile", None)
    return bool(
        legacy_env_profile is not None and legacy_env_profile == payload.workspace.profile_ref
    )


def _legacy_database_profile_ref_matches(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
) -> bool:
    """Check whether a legacy requires_database row matches the database profile."""
    from awf.service.workspaces import LEGACY_DATABASE_PROFILE_REF  # noqa: E402

    return bool(
        getattr(existing, "requires_database", False) is True
        and payload.workspace.profile_ref == LEGACY_DATABASE_PROFILE_REF
    )


def _has_rich_create_artifact(existing: Workspace) -> bool:
    """Check whether the workspace was created through the rich create path."""
    try:
        state = sa_inspect(existing)
    except NoInspectionAvailable:
        return getattr(existing, "task_attempt", None) is not None
    if "task_attempt" in state.unloaded:
        return False
    return existing.task_attempt is not None


def _owned_path_hints_match(stored: Sequence[str], requested: Sequence[str]) -> bool:
    """Check whether stored and requested owned-path hints are identical."""
    return list(stored) == list(requested)


def _auto_profile_request(payload: WorkspaceCreateRequest) -> bool:
    """Check whether the payload requests an automatically resolved profile."""
    return payload.workspace.profile is None and payload.workspace.profile_ref in (None, "auto")


def _stored_validation_requested_tier_matches(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
) -> bool:
    """Check whether the stored validation requested tier matches the payload's tier."""
    stored_tier = _stored_validation_requested_tier(existing)
    if stored_tier is not None:
        return stored_tier == payload.validation.requested_tier
    # Legacy rows did not always snapshot requested_tier before profile
    # resolution. Treat the tier as unknown when the durable profile request
    # still matches, so an otherwise identical replay stays valid.
    return _legacy_validation_requested_tier_unknown(existing, payload)


def _legacy_validation_requested_tier_unknown(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
) -> bool:
    """Check whether a legacy row with an unknown requested tier still matches the payload."""
    if not _profile_ref_matches(existing, payload):
        return False
    if (
        getattr(existing, "requested_profile", None) is not None
        or payload.workspace.profile is not None
    ):
        return False
    if _auto_profile_request(payload):
        return True
    return (
        getattr(existing, "profile_ref", None) is not None
        or _legacy_database_profile_ref_matches(existing, payload)
        or _legacy_env_profile_ref_matches(existing, payload)
    ) and getattr(existing, "resolved_profile", None) is None


def _stored_resource_reservation_matches(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
    *,
    settings: Settings | None,
) -> bool:
    """Check whether the stored resource reservation matches the request payload."""
    # Kept for caller compatibility; replay matching must not re-plan mutable defaults.
    del settings
    requested_values = _requested_resource_reservation_values(payload)
    stored_values = _stored_resource_reservation_request_values(existing)
    if stored_values is not None:
        return stored_values == requested_values

    reservation = _latest_workspace_resource_reservation(existing)
    if reservation is None:
        return not requested_values

    stored_dind_slots = _stored_resource_dind_slots(existing, payload)
    return (
        _resource_reservation_matches_request_values(reservation, requested_values)
        and reservation.dind_slots == stored_dind_slots
    )


def _requested_resource_reservation_values(
    payload: WorkspaceCreateRequest,
) -> dict[str, int | float]:
    """Extract normalised resource reservation values from a rich create request."""
    resources = payload.resources
    legacy_memory_gb = _parse_memory_gb(resources.memory)
    values: dict[str, int | float] = {}
    if resources.steady_state_cpu_cores is not None:
        values["steady_cpu"] = resources.steady_state_cpu_cores
    elif resources.cpu is not None:
        values["steady_cpu"] = resources.cpu
    if resources.peak_cpu_cores is not None:
        values["peak_cpu"] = resources.peak_cpu_cores
    elif resources.cpu is not None:
        values["peak_cpu"] = resources.cpu
    if resources.steady_state_memory_gb is not None:
        values["steady_memory_gb"] = resources.steady_state_memory_gb
    elif legacy_memory_gb is not None:
        values["steady_memory_gb"] = legacy_memory_gb
    if resources.peak_memory_gb is not None:
        values["peak_memory_gb"] = resources.peak_memory_gb
    elif legacy_memory_gb is not None:
        values["peak_memory_gb"] = legacy_memory_gb
    if resources.disk_mb is not None:
        values["disk_mb"] = resources.disk_mb
    return values


def _requested_resource_dind_slots(payload: WorkspaceCreateRequest) -> int:
    """Return the number of DinD slots requested by the payload's resolved profile."""
    _, resolved_profile = workspace_create_profile_snapshots(payload)
    return 1 if _dind_mode_from_profile_snapshot(resolved_profile) == "dind" else 0


def _stored_resource_dind_slots(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
) -> int:
    """Return the DinD slot count from the stored workspace's resolved profile, falling back to the request."""
    resolved_profile = getattr(existing, "resolved_profile", None)
    if isinstance(resolved_profile, Mapping):
        return 1 if _dind_mode_from_profile_snapshot(resolved_profile) == "dind" else 0
    return _requested_resource_dind_slots(payload)


def _stored_resource_reservation_request_values(
    existing: Workspace,
) -> dict[str, int | float] | None:
    """Extract the resource reservation request values stored in a workspace's task policy."""
    from awf.service.workspaces import RESOURCE_RESERVATION_REQUEST_POLICY_KEY  # noqa: E402

    resource_request = _stored_task_policy(existing).get(RESOURCE_RESERVATION_REQUEST_POLICY_KEY)
    if not isinstance(resource_request, dict):
        return None
    values: dict[str, int | float] = {}
    for field in (
        "steady_cpu",
        "steady_memory_gb",
        "peak_cpu",
        "peak_memory_gb",
        "disk_mb",
    ):
        value = resource_request.get(field)
        if value is None:
            continue
        if isinstance(value, bool):
            return None
        if field == "disk_mb":
            if not isinstance(value, int):
                return None
            values[field] = value
            continue
        if not isinstance(value, (int, float)):
            return None
        values[field] = float(value)
    return values


def _resource_reservation_matches_request_values(
    reservation: ResourceReservation,
    requested_values: Mapping[str, int | float],
) -> bool:
    """Check whether a persisted reservation matches the requested resource values."""
    for field in ("steady_cpu", "steady_memory_gb", "peak_cpu", "peak_memory_gb"):
        value = requested_values.get(field)
        if value is not None and getattr(reservation, field) != value:
            return False
    requested_disk_mb = requested_values.get("disk_mb")
    return requested_disk_mb is None or reservation.disk_mb == requested_disk_mb


def _latest_workspace_resource_reservation(existing: Workspace) -> ResourceReservation | None:
    """Return the most recent resource reservation for a workspace, or None."""
    try:
        state = sa_inspect(existing)
    except NoInspectionAvailable:
        reservations = cast(
            Sequence[ResourceReservation],
            getattr(existing, "resource_reservations", []),
        )
    else:
        if "resource_reservations" in state.unloaded:
            return None
        reservations = cast(
            Sequence[ResourceReservation],
            getattr(existing, "resource_reservations", []),
        )
    if not reservations:
        return None
    return max(reservations, key=lambda item: (item.reserved_at, item.id))


def _stored_validation_requested_tier(existing: Workspace) -> int | None:
    """Return the requested validation tier stored in the workspace's task policy or resolved profile."""
    from awf.service.workspaces import (  # noqa: E402
        VALIDATION_POLICY_KEY,
        VALIDATION_REQUESTED_TIER_POLICY_KEY,
    )

    validation_policy = _stored_task_policy(existing).get(VALIDATION_POLICY_KEY)
    if isinstance(validation_policy, dict):
        tier = validation_policy.get(VALIDATION_REQUESTED_TIER_POLICY_KEY)
        if isinstance(tier, int) and not isinstance(tier, bool):
            return tier
    resolved_tier = _resolved_profile_requested_tier(existing)
    if resolved_tier is not None:
        return resolved_tier
    return None


def _resolved_profile_requested_tier(existing: Workspace) -> int | None:
    """Extract the requested validation tier from the workspace's resolved profile."""
    profile = getattr(existing, "resolved_profile", None)
    if not isinstance(profile, Mapping):
        return None
    validation = profile.get("validation")
    if not isinstance(validation, Mapping):
        return None
    tier = validation.get("requested_tier")
    return tier if isinstance(tier, int) and not isinstance(tier, bool) else None


def _requested_task_out_of_scope_policy(
    payload: WorkspaceCreateRequest,
) -> dict[str, object] | None:
    """Return the out-of-scope changes policy dict from the request payload, if any."""
    if payload.task.out_of_scope_changes is None:
        return None
    return payload.task.out_of_scope_changes.model_dump(mode="json")


def _stored_task_out_of_scope_policy(existing: Workspace) -> dict[str, object] | None:
    """Return the out-of-scope changes policy dict stored in the workspace's task policy."""
    out_of_scope = _stored_task_policy(existing).get("out_of_scope_changes")
    return out_of_scope if isinstance(out_of_scope, dict) else None


def _requested_task_provider_recovery_policy(
    payload: WorkspaceCreateRequest,
) -> dict[str, object] | None:
    """Return the provider recovery policy dict from the request payload, if any."""
    if payload.task.provider_recovery is None:
        return None
    return payload.task.provider_recovery.model_dump(
        mode="json",
        exclude_none=True,
        exclude_unset=True,
    )


def _stored_task_provider_recovery_policy(
    existing: Workspace,
) -> dict[str, object] | None:
    """Return the provider recovery policy dict stored in the workspace's task policy."""
    provider_recovery = _stored_task_policy(existing).get("provider_recovery")
    return provider_recovery if isinstance(provider_recovery, dict) else None


def _requested_companions(payload: WorkspaceCreateRequest) -> list[dict[str, object]]:
    """Return normalized companion requests from a rich create payload."""
    return [
        cast(dict[str, object], companion.model_dump(mode="json"))
        for companion in payload.companions
    ]


def _stored_companions(existing: Workspace) -> list[dict[str, object]]:
    """Return normalized companion requests stored in task policy."""
    companions = _stored_task_policy(existing).get(COMPANION_POLICY_KEY)
    if not isinstance(companions, list):
        return []
    return [_normalize_stored_companion(item) for item in companions if isinstance(item, Mapping)]


def _normalize_stored_companion(item: Mapping[str, object]) -> dict[str, object]:
    """Return a current companion snapshot for stored data, preserving invalid rows."""
    try:
        companion = WorkspaceCompanionRequest.model_validate(item)
    except ValidationError:
        return dict(item)
    return cast(dict[str, object], companion.model_dump(mode="json"))


def _requested_task_scheduler_policy(
    payload: WorkspaceCreateRequest,
) -> dict[str, object] | None:
    """Return the scheduler policy dict from the request payload, if priority or human_boost are set."""
    if payload.task.priority == 0 and payload.task.human_boost == 0:
        return None
    scheduler_policy = scheduler_policy_snapshot(
        base_priority=payload.task.priority,
        human_boost=payload.task.human_boost,
    )
    return cast(dict[str, object], scheduler_policy) if isinstance(scheduler_policy, dict) else None


def _stored_task_scheduler_policy(existing: Workspace) -> dict[str, object] | None:
    """Return the scheduler policy dict stored in the workspace's task policy."""
    scheduler_policy = _stored_task_policy(existing).get(SCHEDULER_POLICY_KEY)
    return scheduler_policy if isinstance(scheduler_policy, dict) else None


def _stored_task_agent_model(existing: Workspace) -> str | None:
    """Return the agent model string stored in the workspace's task policy."""
    model = _stored_task_policy(existing).get("agent_model")
    return model if isinstance(model, str) and model else None


def _stored_task_agent_effort(existing: Workspace) -> str | None:
    """Return the agent effort string stored in the workspace's task policy."""
    effort = _stored_task_policy(existing).get("agent_effort")
    return effort if isinstance(effort, str) and effort else None


def _stored_task_policy(existing: Workspace) -> Mapping[str, Any]:
    """Return a task-policy mapping, tolerating legacy rows and simple test doubles."""
    task_policy = getattr(existing, "task_policy", None)
    return task_policy if isinstance(task_policy, Mapping) else {}


def _requested_provider_readiness_override(
    payload: WorkspaceCreateRequest,
) -> tuple[bool, str | None]:
    """Extract the provider readiness override flag and normalised reason from the request."""
    return (
        payload.preflight.provider_readiness_override,
        _normalized_provider_readiness_override_reason(
            payload.preflight.provider_readiness_override_reason
        ),
    )


def _stored_task_provider_readiness_override(
    existing: Workspace,
) -> tuple[bool, str | None]:
    """Extract the stored provider readiness override flag and reason from a workspace."""
    preflight = workspace_provider_readiness_preflight(existing)
    if preflight is None:
        return (False, None)
    reason = preflight.get("override_reason")
    override_requested = preflight.get("override_requested")
    return (
        override_requested
        if isinstance(override_requested, bool)
        else preflight.get("override_used") is True,
        reason if isinstance(reason, str) else None,
    )


def _task_provider_readiness_override_matches(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
) -> bool:
    """Check whether the stored and requested provider readiness overrides match."""
    stored_override, stored_reason = _stored_task_provider_readiness_override(existing)
    stored_redaction_parts = _stored_task_provider_readiness_override_redaction_parts(existing)
    requested_override, requested_reason = _requested_provider_readiness_override(payload)
    return stored_override == requested_override and _override_reasons_match(
        stored_reason,
        requested_reason,
        stored_redaction_parts=stored_redaction_parts,
    )


def _normalized_provider_readiness_override_reason(reason: str | None) -> str | None:
    """Strip and normalise a provider readiness override reason string."""
    if reason is None:
        return None
    normalized = reason.strip()
    return normalized or None


def _override_reasons_match(
    stored_reason: str | None,
    requested_reason: str | None,
    *,
    stored_redaction_parts: list[str] | None = None,
) -> bool:
    """Check whether stored and requested override reasons match, accounting for redaction."""
    if stored_reason == requested_reason:
        return True
    if stored_reason is None or requested_reason is None:
        return False
    if stored_redaction_parts is None:
        return False

    from awf.service.workspaces import _REDACTED_TEXT  # noqa: E402

    if _REDACTED_TEXT.join(stored_redaction_parts) != stored_reason:
        return False
    pattern = ".+".join(re.escape(part) for part in stored_redaction_parts)
    return re.fullmatch(pattern, requested_reason, flags=re.DOTALL) is not None


def _stored_task_provider_readiness_override_redaction_parts(
    existing: Workspace,
) -> list[str] | None:
    """Return the redaction parts of the stored provider readiness override reason, if any."""
    preflight = workspace_provider_readiness_preflight(existing)
    if preflight is None:
        return None
    parts = preflight.get("override_reason_redaction_parts")
    if not isinstance(parts, list) or len(parts) < 2:
        return None
    return cast(list[str], parts) if all(isinstance(part, str) for part in parts) else None


def _selected_provider_preflight_for_task(
    settings: Settings,
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
    override: bool,
    override_reason: str | None,
    provider_environ: Mapping[str, str] | None,
    run_subprocess: SubprocessRun | None,
    http_get: HttpGet | None,
) -> dict[str, Any]:
    """Run the provider readiness preflight check synchronously for a given task."""
    return selected_provider_readiness_preflight(
        resolve_service_settings(settings),
        agent=agent,
        task_policy=task_policy,
        override=override,
        override_reason=override_reason,
        environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )


async def _selected_provider_preflight_for_task_async(
    settings: Settings,
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
    override: bool,
    override_reason: str | None,
    provider_environ: Mapping[str, str] | None,
    run_subprocess: SubprocessRun | None,
    http_get: HttpGet | None,
) -> dict[str, Any]:
    """Run the provider readiness preflight check asynchronously for a given task."""
    return await asyncio.to_thread(
        _selected_provider_preflight_for_task,
        settings,
        agent=agent,
        task_policy=task_policy,
        override=override,
        override_reason=override_reason,
        provider_environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )


def _raise_if_provider_preflight_blocks(preflight: Mapping[str, Any]) -> None:
    """Raise an error if the provider readiness preflight result blocks launch."""
    if preflight.get("blocks_launch") is True:
        from awf.service.workspaces import WorkspaceProviderReadinessBlockedError  # noqa: E402

        raise WorkspaceProviderReadinessBlockedError(preflight)


async def _record_provider_readiness_preflight(
    repo: WorkspaceRepository,
    workspace: Workspace,
    preflight: Mapping[str, Any],
) -> None:
    """Record a provider readiness preflight event on the workspace."""
    from awf.service.workspaces import (  # noqa: E402
        PROVIDER_READINESS_OVERRIDE_REASON,
        PROVIDER_READINESS_PREFLIGHT_EVENT_TYPE,
        PROVIDER_READINESS_READY_REASON,
    )

    await repo.add_event(
        workspace,
        event_type=PROVIDER_READINESS_PREFLIGHT_EVENT_TYPE,
        reason_code=(
            PROVIDER_READINESS_OVERRIDE_REASON
            if preflight.get("override_used") is True
            else PROVIDER_READINESS_READY_REASON
        ),
        payload={"provider_readiness_preflight": dict(preflight)},
    )


def workspace_provider_readiness_preflight(
    workspace: Workspace,
) -> dict[str, Any] | None:
    """Extract the provider readiness preflight snapshot from a workspace's task policy."""
    return provider_readiness_preflight_from_task_policy(_stored_task_policy(workspace))


async def _record_owned_path_overlap_risk(
    repo: WorkspaceRepository,
    workspace: Workspace,
    overlaps: list[OwnedPathOverlap],
) -> None:
    """Record an owned-path overlap risk event on the workspace if overlaps exist."""
    if not overlaps:
        return
    from awf.service.workspaces import (  # noqa: E402
        OWNED_PATH_OVERLAP_RISK_CODE,
        OWNED_PATH_OVERLAP_RISK_EVENT_TYPE,
    )

    await repo.add_event(
        workspace,
        event_type=OWNED_PATH_OVERLAP_RISK_EVENT_TYPE,
        reason_code=OWNED_PATH_OVERLAP_RISK_CODE,
        payload=owned_path_overlap_warning_payload(overlaps),
    )


def owned_path_overlap_warning_payload(overlaps: list[OwnedPathOverlap]) -> dict[str, Any]:
    """Build a warning payload dict from a list of owned-path overlap records."""
    from awf.service.workspaces import (  # noqa: E402
        OWNED_PATH_OVERLAP_RISK_CODE,
        OWNED_PATH_OVERLAP_RISK_MESSAGE,
    )

    workspace_ids: dict[str, None] = {}
    overlap_items: list[dict[str, str]] = []
    for overlap in overlaps:
        if overlap.workspace_id not in workspace_ids:
            workspace_ids[overlap.workspace_id] = None
        overlap_items.append(
            {
                "workspace_id": overlap.workspace_id,
                "existing_path": overlap.existing_path,
                "requested_path": overlap.requested_path,
            }
        )
    return {
        "warning_code": OWNED_PATH_OVERLAP_RISK_CODE,
        "message": OWNED_PATH_OVERLAP_RISK_MESSAGE,
        "workspace_ids": list(workspace_ids),
        "overlaps": overlap_items,
    }


def overlap_risk_summary(overlaps: list[OwnedPathOverlap]) -> dict[str, Any]:
    """Build a summary dict describing owned-path overlap risk, or a zero-risk placeholder."""
    from awf.service.workspaces import OWNED_PATH_OVERLAP_RISK_CODE  # noqa: E402

    if not overlaps:
        return {
            "warning_code": None,
            "overlap_count": 0,
            "workspace_ids": [],
            "overlaps": [],
        }
    payload = owned_path_overlap_warning_payload(overlaps)
    return {
        "warning_code": OWNED_PATH_OVERLAP_RISK_CODE,
        "overlap_count": len(overlaps),
        "workspace_ids": payload["workspace_ids"],
        "overlaps": payload["overlaps"],
    }


def task_class_priority(task_class: str | None) -> int:
    """Return the scheduler priority bias for a task class."""
    return scheduler_task_class_priority(task_class)


def computed_priority(
    *,
    base_priority: int,
    task_class: str | None,
    age_boost: int,
    retry_bonus: int,
) -> int:
    """Compute the effective scheduling priority from base, class bias, age and retry."""
    return base_priority + task_class_bias(task_class) + age_boost + retry_bonus


def resource_reservation_plan(
    payload: WorkspaceCreateRequest,
    *,
    settings: Settings,
) -> ResourceReservationPlan:
    """Build a resource reservation plan from a rich create request and settings."""
    from awf.service.workspaces import ResourceReservationPlan  # noqa: E402

    resources = payload.resources
    legacy_memory_gb = _parse_memory_gb(resources.memory)
    _, resolved_profile = workspace_create_profile_snapshots(payload)
    dind_mode = _dind_mode_from_profile_snapshot(resolved_profile)
    return ResourceReservationPlan(
        node_id=settings.worker_node_id or "local",
        steady_cpu=(
            resources.steady_state_cpu_cores
            if resources.steady_state_cpu_cores is not None
            else resources.cpu
            if resources.cpu is not None
            else settings.workspace_steady_cpu
        ),
        steady_memory_gb=(
            resources.steady_state_memory_gb
            if resources.steady_state_memory_gb is not None
            else legacy_memory_gb
            if legacy_memory_gb is not None
            else settings.workspace_steady_memory_gb
        ),
        peak_cpu=(
            resources.peak_cpu_cores
            if resources.peak_cpu_cores is not None
            else resources.cpu
            if resources.cpu is not None
            else settings.workspace_peak_cpu
        ),
        peak_memory_gb=(
            resources.peak_memory_gb
            if resources.peak_memory_gb is not None
            else legacy_memory_gb
            if legacy_memory_gb is not None
            else settings.workspace_peak_memory_gb
        ),
        disk_mb=resources.disk_mb,
        dind_slots=1 if dind_mode == "dind" else 0,
        dind_mode=dind_mode,
    )


async def _resource_reservation_summary(
    session: AsyncSession,
    reservation_plan: ResourceReservationPlan,
    *,
    settings: Settings,
    disk_check: DiskCheck | None,
) -> dict[str, Any]:
    """Build a resource reservation summary dict combining active totals with the new plan."""
    active_totals = await ResourceReservationRepository(session).active_latest_totals()
    reserved_resources = ReservedResources(
        active_workspace_count=int(active_totals["workspace_count"]) + 1,
        steady_cpu=float(active_totals["steady_cpu"]) + reservation_plan.steady_cpu,
        steady_memory_gb=(
            float(active_totals["steady_memory_gb"]) + reservation_plan.steady_memory_gb
        ),
        peak_cpu=float(active_totals["peak_cpu"]) + reservation_plan.peak_cpu,
        peak_memory_gb=(float(active_totals["peak_memory_gb"]) + reservation_plan.peak_memory_gb),
        disk_mb=int(active_totals["disk_mb"]) + (reservation_plan.disk_mb or 0),
        dind_slots=int(active_totals["dind_slots"]) + reservation_plan.dind_slots,
    )
    return reservation_plan.summary(
        settings=settings,
        disk_check=disk_check,
        reserved_resources=reserved_resources,
    )


def _dind_mode_from_profile_snapshot(profile: Mapping[str, Any] | None) -> str:
    """Return the Docker-in-Docker mode from a resolved profile snapshot."""
    if profile is None:
        return "unknown"
    docker = profile.get("docker")
    if isinstance(docker, Mapping) and docker.get("mode") == "dind":
        return "dind"
    return "none"


def _parse_memory_gb(value: str | None) -> float | None:
    """Parse a human-friendly memory string (e.g. '4gb', '512mb') into gigabytes."""
    if value is None:
        return None
    normalized = value.strip().lower().replace(" ", "")
    if normalized == "":
        return None
    multipliers = {
        "gb": 1.0,
        "g": 1.0,
        "mb": 1.0 / 1024.0,
        "m": 1.0 / 1024.0,
    }
    for suffix, multiplier in multipliers.items():
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)]
            try:
                return float(number) * multiplier
            except ValueError:
                return None
    try:
        memory_gb = float(normalized)
    except ValueError:
        return None

    _log.warning(
        "workspace.resources.memory_unit_missing",
        raw_value=normalized,
        interpreted_unit="gb",
    )
    return memory_gb


def owned_path_overlap_warnings(workspace: Workspace) -> list[WorkspaceWarningResponse]:
    """Extract owned-path overlap warning responses from workspace events."""
    from awf.service.workspaces import OWNED_PATH_OVERLAP_RISK_EVENT_TYPE  # noqa: E402

    warnings: list[WorkspaceWarningResponse] = []
    for event in getattr(workspace, "events", ()) or ():
        if event.event_type != OWNED_PATH_OVERLAP_RISK_EVENT_TYPE:
            continue
        payload = event.payload
        if payload is None:
            continue
        warnings.append(_owned_path_overlap_warning_response(payload))
    return warnings


def _owned_path_overlap_warning_response(
    payload: dict[str, Any],
) -> WorkspaceWarningResponse:
    """Build a WorkspaceWarningResponse from an owned-path overlap warning payload."""
    from awf.service.workspaces import (  # noqa: E402
        OWNED_PATH_OVERLAP_RISK_CODE,
        OWNED_PATH_OVERLAP_RISK_MESSAGE,
    )

    return WorkspaceWarningResponse(
        warning_code=str(payload.get("warning_code", OWNED_PATH_OVERLAP_RISK_CODE)),
        message=str(payload.get("message", OWNED_PATH_OVERLAP_RISK_MESSAGE)),
        workspace_ids=_string_payload_list(payload, "workspace_ids"),
        overlaps=_owned_path_overlap_payload_responses(payload),
    )


def _string_payload_list(payload: dict[str, Any], field: str) -> list[str]:
    """Extract a list of strings from a payload dict field, filtering non-string entries."""
    value = payload.get(field)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _owned_path_overlap_payload_responses(
    payload: dict[str, Any],
) -> list[OwnedPathOverlapResponse]:
    """Build a list of OwnedPathOverlapResponse from an overlap warning payload."""
    overlaps = payload.get("overlaps")
    if not isinstance(overlaps, list):
        return []
    return [
        OwnedPathOverlapResponse(
            workspace_id=str(item["workspace_id"]),
            existing_path=str(item["existing_path"]),
            requested_path=str(item["requested_path"]),
        )
        for item in overlaps
        if _has_owned_path_overlap_payload_fields(item)
    ]


def _has_owned_path_overlap_payload_fields(item: Any) -> bool:
    """Check whether a dict item contains all required owned-path overlap fields."""
    from awf.service.workspaces import OWNED_PATH_OVERLAP_PAYLOAD_FIELDS  # noqa: E402

    return isinstance(item, dict) and all(
        field in item for field in OWNED_PATH_OVERLAP_PAYLOAD_FIELDS
    )


def workspace_create_profile_snapshots(
    payload: WorkspaceCreateRequest,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve requested and resolved profile snapshots from a rich create request."""
    requested_profile = (
        payload.workspace.profile.model_dump(mode="json", by_alias=True)
        if payload.workspace.profile is not None
        else None
    )
    resolved_profile = None
    if payload.workspace.profile is not None or (
        payload.workspace.profile_ref and payload.workspace.profile_ref != "auto"
    ):
        resolved = resolve_workspace_profile(
            worktree_path=None,
            inline_profile=payload.workspace.profile,
            profile_ref=payload.workspace.profile_ref,
            validation_commands=payload.validation.commands,
        )
        profile = profile_with_requested_tier(
            resolved.profile,
            payload.validation.requested_tier,
        )
        resolved_profile = profile.model_dump(mode="json", by_alias=True)
    return requested_profile, resolved_profile


def _assert_supported_direct_create_task_kind(task_kind: str) -> None:
    """Defense-in-depth re-check of the public direct-create task-kind allow-set.

    The ``WorkspaceTask`` schema validator already rejects unsupported kinds on
    REST/MCP; this guards internal callers that build a request another way.
    Adoption bypasses this function (it calls ``WorkspaceRepository.create``).
    """
    if task_kind not in PUBLIC_DIRECT_CREATE_TASK_KINDS:
        supported = ", ".join(sorted(PUBLIC_DIRECT_CREATE_TASK_KINDS))
        raise ValueError(
            f"unsupported task kind {task_kind!r} for direct workspace creation; "
            f"supported kinds are: {supported}."
        )


def _effective_auto_merge(payload: WorkspaceCreateRequest) -> bool:
    """Release-PR syncs must never auto-merge; force it off at the boundary."""
    if payload.task.kind == TaskKind.sync_release_pr.value:
        return False
    return payload.task.auto_merge


def workspace_create_task_policy_snapshot(payload: WorkspaceCreateRequest) -> dict[str, Any]:
    """Build a task policy dictionary from a rich create request."""
    from awf.service.workspaces import (  # noqa: E402
        RELEASE_SYNC_POLICY_KEY,
        RESOURCE_RESERVATION_REQUEST_POLICY_KEY,
        VALIDATION_POLICY_KEY,
        VALIDATION_REQUESTED_TIER_POLICY_KEY,
    )

    policy: dict[str, Any] = {}
    resource_request = _requested_resource_reservation_values(payload)
    # Persist empty requests so idempotent replays do not re-plan against
    # mutable default resource settings.
    policy[RESOURCE_RESERVATION_REQUEST_POLICY_KEY] = resource_request
    policy[VALIDATION_POLICY_KEY] = {
        VALIDATION_REQUESTED_TIER_POLICY_KEY: payload.validation.requested_tier
    }
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
        policy["agent_model"] = payload.task.model
    if payload.task.effort is not None:
        policy["agent_effort"] = payload.task.effort
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


def profile_with_requested_tier(
    profile: WorkspaceProfile,
    requested_tier: int,
) -> WorkspaceProfile:
    """Return a profile copy with the validation requested_tier overridden."""
    if profile.validation.requested_tier == requested_tier:
        return profile
    return profile.model_copy(
        update={
            "validation": profile.validation.model_copy(update={"requested_tier": requested_tier})
        }
    )
