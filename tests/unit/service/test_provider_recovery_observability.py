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
    PROVIDER_FALLBACK_SELECTED_REASON,
    PROVIDER_RECOVERY_COOLDOWN_EVENT,
    PROVIDER_RECOVERY_REASON_CODES,
    PROVIDER_RECOVERY_STATE_KEY,
    PROVIDER_RETRY_DELAYED_REASON,
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
        "source_provider": source_provider or target_provider,
        "source_model": source_model or target_model,
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
    workspace = _workspace_with_provider_recovery_state(not_before=not_before_iso)
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


def test_provider_recovery_state_for_workspace_returns_none_when_no_recovery() -> None:
    workspace = _workspace_without_recovery()
    view = provider_recovery_state_for_workspace(workspace)
    assert view is None


def test_provider_recovery_state_task_policy_falls_back_to_target_when_source_missing() -> None:
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
    assert view.source_provider == "openai"
    assert view.source_model == "gpt-5"


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
