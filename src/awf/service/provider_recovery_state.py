"""Provider recovery state views derived from task policy and events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from awf.db.models import Workspace
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_COOLDOWN_EVENT,
    PROVIDER_RECOVERY_REQUESTED_EVENT,
    PROVIDER_RECOVERY_STATE_KEY,
    PROVIDER_RECOVERY_TERMINAL_EVENT,
    UNSUPPORTED_AGENT_RUNTIME,
    FallbackTarget,
    _mapping_str,
    _nonnegative_int,
)


@dataclass(frozen=True)
class ProviderRecoveryStateView:
    action: Literal["retry", "fallback", "terminal"] | None
    reason_code: str | None
    source_provider: str | None
    source_model: str | None
    retry_attempt_number: int | None
    fallback_attempt_number: int | None
    cooldown_until: datetime | None
    next_eligible_at: datetime | None
    fallback_target: FallbackTarget | None
    source_workspace_id: str | None
    source_attempt_id: str | None
    recommended_action: str | None
    terminal: bool | None
    launched_fallback_attempts: int | None = None


def provider_recovery_state_for_workspace(
    workspace: Workspace,
    *,
    now: datetime | None = None,  # noqa: ARG001
) -> ProviderRecoveryStateView | None:
    task_policy = (
        workspace.task_policy
        if isinstance(getattr(workspace, "task_policy", None), Mapping)
        else {}
    )
    recovery_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
    event_view = _provider_recovery_state_from_events(workspace)
    if isinstance(recovery_state, Mapping):
        return _merge_recovery_views(
            _provider_recovery_state_from_task_policy(recovery_state),
            event_view,
        )
    return event_view


def provider_recovery_decision_from_workspace(
    workspace: Workspace,
) -> ProviderRecoveryStateView | None:
    return provider_recovery_state_for_workspace(workspace)


def _validate_recovery_action(raw: str | None) -> Literal["retry", "fallback", "terminal"] | None:
    if raw not in {"retry", "fallback", "terminal"}:
        return None
    return raw  # type: ignore[return-value]


def _recommended_action_for_action(
    action: Literal["retry", "fallback", "terminal"] | None,
) -> str | None:
    if action == "retry":
        return "Retry after provider cooldown."
    if action == "fallback":
        return "Dispatch an approved fallback model."
    if action == "terminal":
        return "No further recovery possible; inspect failure details."
    return None


def _parse_not_before(not_before_str: str | None) -> tuple[datetime | None, datetime | None]:
    if not_before_str is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(not_before_str)
        cooldown_until = (
            parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        )
        return cooldown_until, cooldown_until
    except ValueError:
        return None, None


def _build_provider_recovery_state_view(
    recovery: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
    default_reason_code: str | None = None,
) -> ProviderRecoveryStateView:
    payload_map = payload or {}
    action = _validate_recovery_action(_mapping_str(recovery, "action"))
    reason_code = (
        _mapping_str(recovery, "decision_reason_code")
        or _mapping_str(recovery, "source_reason_code")
        or _mapping_str(recovery, "reason_code")
        or default_reason_code
    )
    source_provider = _mapping_str(recovery, "source_provider") or _mapping_str(
        recovery, "provider"
    )
    source_model = _mapping_str(recovery, "source_model") or _mapping_str(recovery, "model")
    retry_attempt_number = (
        _nonnegative_int(recovery["retry_attempt_number"], default=0)
        if recovery.get("retry_attempt_number") is not None
        else None
    )
    fallback_attempt_number = (
        _nonnegative_int(recovery["fallback_attempt_number"], default=0)
        if recovery.get("fallback_attempt_number") is not None
        else None
    )
    launched_fallback_attempts = (
        _nonnegative_int(recovery["launched_fallback_attempts"], default=0)
        if recovery.get("launched_fallback_attempts") is not None
        else None
    )
    cooldown_until, next_eligible_at = _parse_not_before(_mapping_str(recovery, "not_before"))
    target_agent = _mapping_str(recovery, "target_agent")
    target_provider = _mapping_str(recovery, "target_provider")
    target_model = _mapping_str(recovery, "target_model")
    fallback_target = (
        FallbackTarget(agent=target_agent, provider=target_provider, model=target_model)
        if action == "fallback" and target_agent and target_model
        else None
    )
    source_workspace_id = _mapping_str(recovery, "source_workspace_id") or _mapping_str(
        payload_map, "source_workspace_id"
    )
    source_attempt_id = _mapping_str(recovery, "source_attempt_id") or _mapping_str(
        payload_map, "source_attempt_id"
    )
    recommended_action = (
        _mapping_str(recovery, "recommended_action") or _recommended_action_for_action(action)
        if reason_code != UNSUPPORTED_AGENT_RUNTIME
        else None
    )
    terminal = action == "terminal" if action is not None else None
    return ProviderRecoveryStateView(
        action=action,
        reason_code=reason_code,
        source_provider=source_provider,
        source_model=source_model,
        retry_attempt_number=retry_attempt_number,
        fallback_attempt_number=fallback_attempt_number,
        cooldown_until=cooldown_until,
        next_eligible_at=next_eligible_at,
        fallback_target=fallback_target,
        source_workspace_id=source_workspace_id,
        source_attempt_id=source_attempt_id,
        recommended_action=recommended_action,
        terminal=terminal,
        launched_fallback_attempts=launched_fallback_attempts,
    )


def _provider_recovery_state_from_task_policy(
    recovery_state: Mapping[str, Any],
) -> ProviderRecoveryStateView | None:
    return _build_provider_recovery_state_view(recovery_state)


def _provider_recovery_state_from_events(
    workspace: Workspace,
) -> ProviderRecoveryStateView | None:
    recovery_event_types = frozenset(
        {
            PROVIDER_RECOVERY_REQUESTED_EVENT,
            PROVIDER_RECOVERY_COOLDOWN_EVENT,
            PROVIDER_RECOVERY_TERMINAL_EVENT,
            "workspace.provider_recovery_created",
        }
    )
    events = getattr(workspace, "events", []) or []
    latest_event: Any | None = None
    for event in reversed(events):
        event_type = getattr(event, "event_type", None)
        if event_type in recovery_event_types:
            latest_event = event
            break
    if latest_event is None:
        return None
    payload = getattr(latest_event, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    recovery = payload.get("provider_recovery")
    if not isinstance(recovery, Mapping):
        recovery = payload
    default_reason = getattr(latest_event, "reason_code", None) or None
    return _build_provider_recovery_state_view(
        recovery, payload=payload, default_reason_code=default_reason
    )


def _merge_recovery_views(
    policy_view: ProviderRecoveryStateView | None,
    event_view: ProviderRecoveryStateView | None,
) -> ProviderRecoveryStateView | None:
    if policy_view is None:
        return event_view
    if event_view is None:
        return policy_view
    merged = {
        name: (
            getattr(policy_view, name)
            if getattr(policy_view, name) is not None
            else getattr(event_view, name)
        )
        for name in ProviderRecoveryStateView.__dataclass_fields__
    }
    return ProviderRecoveryStateView(**merged)
