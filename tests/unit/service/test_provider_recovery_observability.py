"""Unit tests for provider recovery observability: extraction, event payloads, merge queue, metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from awf.service.metrics import (
    FailedWorkspaceExample,
    ProviderRecoveryStateSummary,
)
from awf.service.provider_recovery import (
    PROVIDER_AUTH_FAILED,
    PROVIDER_FALLBACK_SELECTED_REASON,
    PROVIDER_RECOVERY_COOLDOWN_EVENT,
    PROVIDER_RECOVERY_REASON_CODES,
    PROVIDER_RECOVERY_REQUESTED_EVENT,
    PROVIDER_RECOVERY_STATE_KEY,
    PROVIDER_RETRY_DELAYED_REASON,
    ProviderRecoveryStateView,
    _merge_recovery_views,
    _parse_not_before,
    _recommended_action_for_action,
    _validate_recovery_action,
    provider_recovery_decision_from_workspace,
    provider_recovery_state_for_workspace,
)


def _workspace_with_provider_recovery_state(
    *,
    action: str = "retry",
    reason_code: str = PROVIDER_RETRY_DELAYED_REASON,
    source_reason_code: str = "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
    retry_attempt_number: int = 1,
    fallback_attempt_number: int = 0,
    target_agent: str = "codex",
    target_provider: str | None = "openai",
    target_model: str = "gpt-5",
    source_provider: str | None = None,
    source_model: str | None = None,
    source_workspace_id: str = "ws-source-001",
    source_attempt_id: str = "att-001",
    not_before: str | None = None,
) -> SimpleNamespace:
    state: dict[str, Any] = {
        "action": action,
        "decision_reason_code": reason_code,
        "source_reason_code": source_reason_code,
        "source_provider": source_provider,
        "source_model": source_model,
        "retry_attempt_number": retry_attempt_number,
        "fallback_attempt_number": fallback_attempt_number,
        "target_agent": target_agent,
        "target_provider": target_provider,
        "target_model": target_model,
        "source_workspace_id": source_workspace_id,
        "source_attempt_id": source_attempt_id,
    }
    if not_before is not None:
        state["not_before"] = not_before
    return SimpleNamespace(
        id="ws-001",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[],
    )


def _workspace_without_recovery() -> SimpleNamespace:
    return SimpleNamespace(
        id="ws-002",
        status="failed",
        task_policy={},
        failure_reason="agent_failure",
        failure_message="Agent error",
        agent="codex",
        events=[],
    )


def _workspace_with_cooldown_event(
    *,
    reason_code: str = PROVIDER_RETRY_DELAYED_REASON,
    not_before: datetime | None = None,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    effective_not_before = not_before or (now + timedelta(seconds=300))
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-001",
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": reason_code,
            "not_before": effective_not_before.isoformat(),
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
        reason_code=reason_code,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-001",
    )
    return SimpleNamespace(
        id="ws-003",
        status="failed",
        task_policy={},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[event],
    )


def test_provider_recovery_state_for_workspace_extracts_from_task_policy() -> None:
    not_before_iso = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    workspace = _workspace_with_provider_recovery_state(
        source_provider="openai",
        source_model="gpt-5",
        not_before=not_before_iso,
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.action == "retry"
    assert view.reason_code == PROVIDER_RETRY_DELAYED_REASON
    assert view.source_provider == "openai"
    assert view.source_model == "gpt-5"
    assert view.retry_attempt_number == 1
    assert view.fallback_attempt_number == 0
    assert view.next_eligible_at is not None
    assert view.source_workspace_id == "ws-source-001"
    assert view.source_attempt_id == "att-001"
    assert view.fallback_target is None


def test_provider_recovery_state_for_workspace_fallback_preserves_source_lineage() -> None:
    not_before_iso = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    workspace = _workspace_with_provider_recovery_state(
        action="fallback",
        reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5.3-codex",
        source_provider="google",
        source_model="gemini-2.5-pro",
        not_before=not_before_iso,
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.action == "fallback"
    assert view.source_provider == "google"
    assert view.source_model == "gemini-2.5-pro"
    assert view.fallback_target is not None
    assert view.fallback_target.provider == "openai"
    assert view.fallback_target.model == "gpt-5.3-codex"


def test_provider_recovery_state_for_workspace_extracts_from_events() -> None:
    workspace = _workspace_with_cooldown_event()
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.action == "retry"
    assert view.reason_code == PROVIDER_RETRY_DELAYED_REASON
    assert view.cooldown_until is not None


def test_provider_recovery_state_from_events_parses_lineage_fields() -> None:
    now = datetime.now(UTC)
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-001",
        "source_attempt_id": "att-042",
        "source_task_id": "task-100",
        "source_canonical_attempt_id": "att-canonical-001",
        "provider_recovery": {
            "action": "fallback",
            "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5.3-codex",
            "source_provider": "google",
            "source_model": "gemini-2.5-pro",
            "retry_attempt_number": 0,
            "fallback_attempt_number": 2,
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-002",
    )
    workspace = SimpleNamespace(
        id="ws-004",
        status="failed",
        task_policy={},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.action == "fallback"
    assert view.retry_attempt_number == 0
    assert view.fallback_attempt_number == 2
    assert view.source_attempt_id == "att-042"
    assert view.source_workspace_id == "ws-source-001"


def test_provider_recovery_state_from_events_lineage_defaults_to_none_when_missing() -> None:
    now = datetime.now(UTC)
    not_before = now + timedelta(seconds=300)
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-001",
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
            "not_before": not_before.isoformat(),
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-003",
    )
    workspace = SimpleNamespace(
        id="ws-005",
        status="failed",
        task_policy={},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.retry_attempt_number is None
    assert view.fallback_attempt_number is None
    assert view.source_attempt_id is None


def test_provider_recovery_state_for_workspace_returns_none_when_no_recovery() -> None:
    workspace = _workspace_without_recovery()
    view = provider_recovery_state_for_workspace(workspace)
    assert view is None


def test_provider_recovery_state_task_policy_source_fields_none_when_missing() -> None:
    state_data: dict[str, Any] = {
        "action": "retry",
        "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
        "source_reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "retry_attempt_number": 1,
        "fallback_attempt_number": 0,
        "target_agent": "codex",
        "target_provider": "openai",
        "target_model": "gpt-5",
        "source_workspace_id": "ws-source-001",
        "source_attempt_id": "att-001",
    }
    workspace = SimpleNamespace(
        id="ws-001",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state_data},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.source_provider is None
    assert view.source_model is None


def test_provider_recovery_decision_from_workspace_derives_cooldown() -> None:
    not_before_dt = datetime.now(UTC) + timedelta(seconds=300)
    workspace = _workspace_with_provider_recovery_state(
        not_before=not_before_dt.isoformat(),
    )
    decision = provider_recovery_decision_from_workspace(workspace)
    assert decision is not None
    assert decision.next_eligible_at is not None
    assert decision.action == "retry"


def test_provider_recovery_state_for_workspace_prioritizes_task_policy_over_events() -> None:
    not_before_iso = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    workspace = _workspace_with_provider_recovery_state(
        action="fallback",
        reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
        not_before=not_before_iso,
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.action == "fallback"
    assert view.reason_code == PROVIDER_FALLBACK_SELECTED_REASON


def test_metrics_saturation_includes_recovery_state_summary() -> None:
    summary = ProviderRecoveryStateSummary(
        pending_retry=2,
        pending_fallback=1,
        in_cooldown=3,
        terminal_no_loop=0,
        terminal_exhausted=1,
        circuit_breakers_open=1,
    )
    assert summary.pending_retry == 2
    assert summary.pending_fallback == 1
    assert summary.in_cooldown == 3
    assert summary.terminal_no_loop == 0
    assert summary.terminal_exhausted == 1
    assert summary.circuit_breakers_open == 1


def test_metrics_failure_analysis_includes_provider_recovery_breakdown() -> None:
    now = datetime.now(UTC)
    example = FailedWorkspaceExample(
        workspace_id="ws-001",
        title="Test workspace",
        repo_url="git@github.com:example/test.git",
        branch_base="main",
        agent="codex",
        status="failed",
        failure_reason="agent_failure",
        failure_message="Provider capacity exhausted",
        pr_url=None,
        created_at=now,
        updated_at=now,
        reason_code="PROVIDER_RETRY_DELAYED",
        details={"provider_recovery": {"action": "retry", "retryable": True}},
    )
    assert example.details.get("provider_recovery") is not None
    assert example.details["provider_recovery"]["action"] == "retry"


def test_provider_recovery_reason_codes_includes_all_contract_values() -> None:
    expected = frozenset(
        {
            PROVIDER_AUTH_FAILED,
            "PROVIDER_MODEL_CIRCUIT_OPEN",
            "PROVIDER_RETRY_DELAYED",
            "PROVIDER_FALLBACK_SELECTED",
            "REPEATED_PROVIDER_FAILURE_FINGERPRINT",
            "NON_RETRYABLE_PROVIDER_FAILURE",
            "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED",
        }
    )
    assert expected == PROVIDER_RECOVERY_REASON_CODES


def test_provider_recovery_event_payload_includes_required_keys() -> None:
    now = datetime.now(UTC)
    not_before = now + timedelta(seconds=300)
    requested_payload = {
        "source_workspace_id": "ws-001",
        "new_workspace_id": "ws-002",
        "provider_recovery": {
            "action": "fallback",
            "decision_reason_code": "PROVIDER_FALLBACK_SELECTED",
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5.3-codex",
            "fallback_attempt_number": 1,
            "retry_attempt_number": 0,
        },
    }
    for required_key in (
        "source_workspace_id",
        "provider_recovery",
    ):
        assert required_key in requested_payload
    recovery = requested_payload["provider_recovery"]
    for required_key in (
        "action",
        "decision_reason_code",
        "target_agent",
        "target_provider",
        "target_model",
        "fallback_attempt_number",
        "retry_attempt_number",
    ):
        assert required_key in recovery

    cooldown_payload = {
        "source_workspace_id": "ws-001",
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": "PROVIDER_RETRY_DELAYED",
            "not_before": not_before.isoformat(),
        },
    }
    for required_key in ("source_workspace_id", "provider_recovery"):
        assert required_key in cooldown_payload
    cooldown_recovery = cooldown_payload["provider_recovery"]
    for required_key in ("action", "decision_reason_code", "not_before"):
        assert required_key in cooldown_recovery


def test_recovery_payload_includes_provider_recovery_state() -> None:
    from awf.service.workspace_observability import (
        WorkspaceRecoverySummary,
    )

    now = datetime.now(UTC)
    workspace = _workspace_with_provider_recovery_state(
        action="retry",
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
    )
    summary = WorkspaceRecoverySummary(
        from_state="running",
        to_state="failed",
        reason_code="agent_failure",
        action="retry",
        recovery_mode=None,
        started_at=now,
        current_operation=None,
        summary="Reverted running -> failed for agent_failure.",
        payload=None,
        provider_recovery=provider_recovery_state_for_workspace(workspace),
    )
    assert summary.provider_recovery is not None
    assert summary.provider_recovery.action == "retry"
    assert summary.provider_recovery.reason_code == PROVIDER_RETRY_DELAYED_REASON


def test_recovery_payload_provider_recovery_includes_all_state_view_fields() -> None:
    from awf.service.workspace_observability import (
        WorkspaceRecoverySummary,
    )

    now = datetime.now(UTC)
    not_before = now + timedelta(minutes=5)
    workspace = _workspace_with_provider_recovery_state(
        action="retry",
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        source_provider="openai",
        source_model="gpt-4",
        retry_attempt_number=2,
        fallback_attempt_number=0,
        source_workspace_id="ws-source-042",
        source_attempt_id="att-042",
        not_before=not_before.isoformat(),
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    summary = WorkspaceRecoverySummary(
        from_state="running",
        to_state="failed",
        reason_code="agent_failure",
        action="retry",
        recovery_mode=None,
        started_at=now,
        current_operation=None,
        summary="Reverted running -> failed for agent_failure.",
        payload=None,
        provider_recovery=view,
    )

    pr = summary.provider_recovery
    assert pr is not None
    serialized = {
        "action": pr.action,
        "reason_code": pr.reason_code,
        "source_provider": pr.source_provider,
        "source_model": pr.source_model,
        "retry_attempt_number": pr.retry_attempt_number,
        "fallback_attempt_number": pr.fallback_attempt_number,
        "fallback_target": (
            {
                "agent": pr.fallback_target.agent,
                "provider": pr.fallback_target.provider,
                "model": pr.fallback_target.model,
            }
            if pr.fallback_target is not None
            else None
        ),
        "cooldown_until": (
            pr.cooldown_until.isoformat() if pr.cooldown_until is not None else None
        ),
        "next_eligible_at": (
            pr.next_eligible_at.isoformat() if pr.next_eligible_at is not None else None
        ),
        "source_workspace_id": pr.source_workspace_id,
        "source_attempt_id": pr.source_attempt_id,
        "recommended_action": pr.recommended_action,
        "terminal": pr.terminal,
    }
    assert serialized["source_provider"] == "openai"
    assert serialized["source_model"] == "gpt-4"
    assert serialized["retry_attempt_number"] == 2
    assert serialized["fallback_attempt_number"] == 0
    assert serialized["source_workspace_id"] == "ws-source-042"
    assert serialized["source_attempt_id"] == "att-042"
    assert serialized["terminal"] is False


def test_validate_recovery_action_accepts_known_actions() -> None:
    assert _validate_recovery_action("retry") == "retry"
    assert _validate_recovery_action("fallback") == "fallback"
    assert _validate_recovery_action("terminal") == "terminal"


def test_validate_recovery_action_rejects_unknown() -> None:
    assert _validate_recovery_action("unknown") is None
    assert _validate_recovery_action(None) is None
    assert _validate_recovery_action("") is None


def test_recommended_action_for_action_maps_known_actions() -> None:
    assert _recommended_action_for_action("retry") == "Retry after provider cooldown."
    assert _recommended_action_for_action("fallback") == "Dispatch an approved fallback model."
    assert (
        _recommended_action_for_action("terminal")
        == "No further recovery possible; inspect failure details."
    )


def test_recommended_action_for_action_returns_none_for_none() -> None:
    assert _recommended_action_for_action(None) is None


def test_provider_recovery_state_from_events_uses_payload_recommended_action() -> None:
    now = datetime.now(UTC)
    event_payload: dict[str, Any] = {
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
            "recommended_action": "Refresh credentials and retry.",
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-rec-001",
    )
    workspace = SimpleNamespace(
        id="ws-rec-001",
        status="failed",
        task_policy={},
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.recommended_action == "Refresh credentials and retry."


def test_provider_recovery_state_from_events_falls_back_to_action_default() -> None:
    now = datetime.now(UTC)
    event_payload: dict[str, Any] = {
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-rec-002",
    )
    workspace = SimpleNamespace(
        id="ws-rec-002",
        status="failed",
        task_policy={},
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.recommended_action == "Retry after provider cooldown."


def test_provider_recovery_state_from_task_policy_uses_payload_recommended_action() -> None:
    state_data: dict[str, Any] = {
        "action": "fallback",
        "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
        "recommended_action": "Switch to backup provider immediately.",
    }
    workspace = SimpleNamespace(
        id="ws-rec-003",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state_data},
        events=[],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.recommended_action == "Switch to backup provider immediately."


def test_provider_recovery_state_from_task_policy_falls_back_to_action_default() -> None:
    state_data: dict[str, Any] = {
        "action": "fallback",
        "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
    }
    workspace = SimpleNamespace(
        id="ws-rec-004",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state_data},
        events=[],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.recommended_action == "Dispatch an approved fallback model."


def test_parse_not_before_parses_iso_with_tz() -> None:
    dt = datetime(2025, 3, 15, 12, 0, 0, tzinfo=UTC)
    iso = dt.isoformat()
    cooldown, eligible = _parse_not_before(iso)
    assert cooldown is not None
    assert eligible is not None
    assert cooldown == dt
    assert eligible == dt


def test_parse_not_before_parses_iso_without_tz() -> None:
    iso = "2025-03-15T12:00:00"
    cooldown, eligible = _parse_not_before(iso)
    assert cooldown is not None
    assert eligible is not None
    assert cooldown.tzinfo is not None
    assert cooldown == datetime(2025, 3, 15, 12, 0, 0, tzinfo=UTC)


def test_parse_not_before_returns_none_for_none() -> None:
    assert _parse_not_before(None) == (None, None)


def test_parse_not_before_returns_none_for_invalid() -> None:
    assert _parse_not_before("not-a-date") == (None, None)


def test_provider_recovery_state_reason_code_prefers_decision_reason_code() -> None:
    workspace = _workspace_with_provider_recovery_state(
        action="retry",
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        source_reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.reason_code == PROVIDER_RETRY_DELAYED_REASON


def test_merge_recovery_views_fills_missing_fields_from_event_view() -> None:
    partial_state: dict[str, Any] = {
        "action": "retry",
        "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
        "retry_attempt_number": 1,
        "fallback_attempt_number": 0,
        "target_agent": "codex",
        "target_provider": "openai",
        "target_model": "gpt-5",
        "source_workspace_id": "ws-source-001",
        "source_attempt_id": "att-001",
    }
    now = datetime.now(UTC)
    not_before = now + timedelta(seconds=300)
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-002",
        "source_attempt_id": "att-002",
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
            "source_provider": "google",
            "source_model": "gemini-2.5-pro",
            "retry_attempt_number": 0,
            "fallback_attempt_number": 2,
            "not_before": not_before.isoformat(),
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-merge",
    )
    workspace = SimpleNamespace(
        id="ws-merge",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: partial_state},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.source_provider == "google"
    assert view.source_model == "gemini-2.5-pro"
    assert view.action == "retry"
    assert view.reason_code == PROVIDER_RETRY_DELAYED_REASON
    assert view.retry_attempt_number == 1
    assert view.fallback_attempt_number == 0
    assert view.cooldown_until is not None


def test_merge_recovery_views_prefers_policy_values_when_present() -> None:
    now = datetime.now(UTC)
    not_before_policy = now + timedelta(seconds=500)
    not_before_event = now + timedelta(seconds=200)
    state: dict[str, Any] = {
        "action": "retry",
        "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
        "source_provider": "anthropic",
        "source_model": "claude-4",
        "retry_attempt_number": 2,
        "fallback_attempt_number": 1,
        "target_agent": "codex",
        "target_provider": "openai",
        "target_model": "gpt-5",
        "source_workspace_id": "ws-src-policy",
        "source_attempt_id": "att-policy",
        "not_before": not_before_policy.isoformat(),
    }
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-src-event",
        "source_attempt_id": "att-event",
        "provider_recovery": {
            "action": "fallback",
            "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
            "source_provider": "google",
            "source_model": "gemini-2.5-pro",
            "retry_attempt_number": 0,
            "fallback_attempt_number": 3,
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5.3-codex",
            "not_before": not_before_event.isoformat(),
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-priority",
    )
    workspace = SimpleNamespace(
        id="ws-priority",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.source_provider == "anthropic"
    assert view.source_model == "claude-4"
    assert view.action == "retry"
    assert view.retry_attempt_number == 2
    assert view.fallback_attempt_number == 1


def test_provider_recovery_state_reason_code_falls_back_to_source_reason_code() -> None:
    state_data: dict[str, Any] = {
        "action": "retry",
        "source_reason_code": PROVIDER_RETRY_DELAYED_REASON,
        "retry_attempt_number": 1,
        "fallback_attempt_number": 0,
        "target_agent": "codex",
        "target_provider": "openai",
        "target_model": "gpt-5",
        "source_workspace_id": "ws-source-001",
        "source_attempt_id": "att-001",
    }
    workspace = SimpleNamespace(
        id="ws-001",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state_data},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.reason_code == PROVIDER_RETRY_DELAYED_REASON


def test_merge_view_prefers_event_recommended_action_when_policy_lacks_one() -> None:
    now = datetime.now(UTC)
    not_before = now + timedelta(seconds=300)
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-001",
        "provider_recovery": {
            "action": "retry",
            "decision_reason_code": PROVIDER_RETRY_DELAYED_REASON,
            "recommended_action": "Refresh credentials and retry.",
            "source_provider": "openai",
            "source_model": "gpt-5",
            "retry_attempt_number": 1,
            "fallback_attempt_number": 0,
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5",
            "not_before": not_before.isoformat(),
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-merge-001",
    )
    state_data: dict[str, Any] = {
        "action": "terminal",
        "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
        "source_provider": "openai",
        "source_model": "gpt-5",
        "retry_attempt_number": 3,
        "fallback_attempt_number": 2,
        "target_agent": "codex",
        "target_provider": "openai",
        "target_model": "gpt-5",
        "source_workspace_id": "ws-source-001",
        "source_attempt_id": "att-001",
    }
    workspace = SimpleNamespace(
        id="ws-merge-001",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state_data},
        failure_reason="agent_failure",
        failure_message="Provider credentials expired",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.recommended_action == "No further recovery possible; inspect failure details."


def test_merge_view_prefers_policy_recommended_action_over_event() -> None:
    now = datetime.now(UTC)
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-002",
        "provider_recovery": {
            "action": "fallback",
            "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
            "recommended_action": "Switch to backup provider immediately.",
            "source_provider": "google",
            "source_model": "gemini-2.5-pro",
            "retry_attempt_number": 0,
            "fallback_attempt_number": 1,
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5.3-codex",
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-merge-002",
    )
    state_data: dict[str, Any] = {
        "action": "fallback",
        "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
        "recommended_action": "Retry with exponential backoff.",
        "source_provider": "google",
        "source_model": "gemini-2.5-pro",
        "retry_attempt_number": 0,
        "fallback_attempt_number": 1,
        "target_agent": "codex",
        "target_provider": "openai",
        "target_model": "gpt-5.3-codex",
        "source_workspace_id": "ws-source-002",
        "source_attempt_id": "att-002",
    }
    workspace = SimpleNamespace(
        id="ws-merge-002",
        status="failed",
        task_policy={PROVIDER_RECOVERY_STATE_KEY: state_data},
        failure_reason="agent_failure",
        failure_message="Provider rate limited",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.recommended_action == "Retry with exponential backoff."


def test_provider_recovery_state_from_events_falls_back_to_event_reason_code() -> None:
    now = datetime.now(UTC)
    event_payload: dict[str, Any] = {
        "source_workspace_id": "ws-source-001",
        "provider_recovery": {
            "action": "retry",
        },
    }
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload=event_payload,
        occurred_at=now,
        old_state="running",
        new_state="failed",
        id="evt-fallback-rc",
    )
    workspace = SimpleNamespace(
        id="ws-fallback-rc",
        status="failed",
        task_policy={},
        failure_reason="agent_failure",
        failure_message="Provider exhausted",
        agent="codex",
        events=[event],
    )
    view = provider_recovery_state_for_workspace(workspace)
    assert view is not None
    assert view.action == "retry"
    assert view.reason_code == PROVIDER_RETRY_DELAYED_REASON


def test_provider_recovery_state_ignores_event_with_non_mapping_payload() -> None:
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        payload="not-a-mapping",
        occurred_at=datetime.now(UTC),
    )
    workspace = SimpleNamespace(
        id="ws-non-mapping-event",
        status="failed",
        task_policy={},
        events=[event],
    )

    assert provider_recovery_state_for_workspace(workspace) is None


def test_provider_recovery_state_from_event_uses_flat_payload_when_nested_missing() -> None:
    event = SimpleNamespace(
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
        payload={
            "source_workspace_id": "ws-source-flat",
            "source_attempt_id": "att-flat",
            "action": "fallback",
            "decision_reason_code": PROVIDER_FALLBACK_SELECTED_REASON,
            "provider": "google",
            "model": "gemini-2.5-pro",
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5.3-codex",
            "fallback_attempt_number": 1,
        },
        occurred_at=datetime.now(UTC),
    )
    workspace = SimpleNamespace(
        id="ws-flat-event",
        status="failed",
        task_policy={},
        events=[event],
    )

    view = provider_recovery_state_for_workspace(workspace)

    assert view is not None
    assert view.action == "fallback"
    assert view.source_provider == "google"
    assert view.source_model == "gemini-2.5-pro"
    assert view.source_workspace_id == "ws-source-flat"
    assert view.source_attempt_id == "att-flat"
    assert view.fallback_target is not None
    assert view.fallback_target.agent == "codex"


def test_merge_recovery_views_returns_event_view_when_policy_absent() -> None:
    event_view = ProviderRecoveryStateView(
        action="retry",
        reason_code=PROVIDER_RETRY_DELAYED_REASON,
        source_provider="google",
        source_model="gemini-2.5-pro",
        retry_attempt_number=1,
        fallback_attempt_number=0,
        cooldown_until=None,
        next_eligible_at=None,
        fallback_target=None,
        source_workspace_id="ws-source",
        source_attempt_id="att-source",
        recommended_action="Retry after provider cooldown.",
        terminal=False,
    )

    assert _merge_recovery_views(None, event_view) is event_view
