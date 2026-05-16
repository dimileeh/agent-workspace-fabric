"""Contract tests for provider recovery API schemas and reason-code stability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from awf.api.schemas import (
    FallbackTargetResponse,
    MergeQueueItemResponse,
    ProviderRecoveryStateResponse,
    WorkspaceFailureDetailsResponse,
    WorkspaceRecoverySummaryResponse,
    WorkspaceResponse,
)
from awf.service.provider_recovery import (
    PROVIDER_AUTH_FAILED,
    PROVIDER_FALLBACK_SELECTED_REASON,
    PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
    PROVIDER_RECOVERY_NO_LOOP_REASON,
    PROVIDER_RECOVERY_REASON_CODES,
    PROVIDER_RETRY_DELAYED_REASON,
)


def test_provider_recovery_reason_codes_are_stable() -> None:
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


def test_provider_recovery_reason_code_constant_values() -> None:
    assert PROVIDER_AUTH_FAILED == "PROVIDER_AUTH_FAILED"
    assert PROVIDER_MODEL_CIRCUIT_OPEN_REASON == "PROVIDER_MODEL_CIRCUIT_OPEN"
    assert PROVIDER_RETRY_DELAYED_REASON == "PROVIDER_RETRY_DELAYED"
    assert PROVIDER_FALLBACK_SELECTED_REASON == "PROVIDER_FALLBACK_SELECTED"
    assert PROVIDER_RECOVERY_NO_LOOP_REASON == "REPEATED_PROVIDER_FAILURE_FINGERPRINT"


def test_provider_recovery_state_response_all_fields_serializes() -> None:
    now = datetime.now(UTC)
    response = ProviderRecoveryStateResponse(
        action="retry",
        reason_code="PROVIDER_RETRY_DELAYED",
        source_provider="openai",
        source_model="gpt-5",
        retry_attempt_number=1,
        fallback_attempt_number=0,
        cooldown_until=now + timedelta(seconds=300),
        next_eligible_at=now + timedelta(seconds=300),
        fallback_target=FallbackTargetResponse(
            agent="codex",
            provider="openai",
            model="gpt-5.3-codex",
        ),
        source_workspace_id="ws-001",
        source_attempt_id="att-001",
        recommended_action="Retry after provider cooldown.",
        terminal=False,
    )
    dumped = response.model_dump()
    roundtripped = ProviderRecoveryStateResponse.model_validate(dumped)
    assert roundtripped.action == "retry"
    assert roundtripped.reason_code == "PROVIDER_RETRY_DELAYED"
    assert roundtripped.source_provider == "openai"
    assert roundtripped.source_model == "gpt-5"
    assert roundtripped.retry_attempt_number == 1
    assert roundtripped.fallback_attempt_number == 0
    assert roundtripped.cooldown_until is not None
    assert roundtripped.next_eligible_at is not None
    assert roundtripped.fallback_target is not None
    assert roundtripped.fallback_target.agent == "codex"
    assert roundtripped.source_workspace_id == "ws-001"
    assert roundtripped.source_attempt_id == "att-001"
    assert roundtripped.recommended_action == "Retry after provider cooldown."
    assert roundtripped.terminal is False


def test_provider_recovery_state_response_missing_fields_use_placeholders() -> None:
    response = ProviderRecoveryStateResponse(
        action="fallback",
        reason_code="PROVIDER_FALLBACK_SELECTED",
        source_provider="anthropic",
        source_model="claude-4-opus",
        retry_attempt_number=0,
        fallback_attempt_number=1,
        cooldown_until=None,
        next_eligible_at=None,
        fallback_target=None,
        source_workspace_id=None,
        source_attempt_id=None,
        recommended_action=None,
        terminal=None,
    )
    assert response.cooldown_until is None
    assert response.next_eligible_at is None
    assert response.fallback_target is None
    assert response.source_workspace_id is None
    assert response.source_attempt_id is None
    assert response.recommended_action is None
    assert response.terminal is None
    dumped = response.model_dump()
    reloaded = ProviderRecoveryStateResponse.model_validate(dumped)
    assert reloaded.cooldown_until is None
    assert reloaded.fallback_target is None


def test_workspace_failure_details_response_provider_recovery_state_compat() -> None:
    legacy = WorkspaceFailureDetailsResponse(
        provider_recovery={"retryable": True, "action": "retry"},
    )
    assert legacy.provider_recovery_state is None
    assert legacy.provider_recovery == {"retryable": True, "action": "retry"}

    enriched = WorkspaceFailureDetailsResponse(
        provider_recovery={"retryable": True},
        provider_recovery_state=ProviderRecoveryStateResponse(
            action="retry",
            reason_code="PROVIDER_RETRY_DELAYED",
        ),
    )
    assert enriched.provider_recovery_state is not None
    assert enriched.provider_recovery_state.action == "retry"
    assert enriched.provider_recovery == {"retryable": True}


def test_merge_queue_item_response_accepts_provider_recovery_state() -> None:
    now = datetime.now(UTC)
    item_none = MergeQueueItemResponse(
        task_id="t1",
        workspace_id="w1",
        title="Test",
        repo_url="git@github.com:example/test.git",
        base_branch="main",
        branch_name=None,
        pr_url="https://github.com/example/test/pull/1",
        status="monitoring_pr",
        auto_merge=True,
        task_class=None,
        owned_paths=[],
        created_at=now,
        updated_at=now,
        last_event=None,
        merge_blocker_reason="ready_to_merge_or_waiting_for_github",
        canonical=True,
        provider_recovery_state=None,
    )
    assert item_none.provider_recovery_state is None

    item_with_recovery = MergeQueueItemResponse(
        task_id="t1",
        workspace_id="w1",
        title="Test",
        repo_url="git@github.com:example/test.git",
        base_branch="main",
        branch_name=None,
        pr_url="https://github.com/example/test/pull/1",
        status="monitoring_pr",
        auto_merge=True,
        task_class=None,
        owned_paths=[],
        created_at=now,
        updated_at=now,
        last_event=None,
        merge_blocker_reason="ready_to_merge_or_waiting_for_github",
        canonical=True,
        provider_recovery_state=ProviderRecoveryStateResponse(
            action="fallback",
            reason_code="PROVIDER_FALLBACK_SELECTED",
            source_provider="openai",
            source_model="gpt-5",
        ),
    )
    assert item_with_recovery.provider_recovery_state is not None
    assert item_with_recovery.provider_recovery_state.action == "fallback"


def test_provider_circuit_breaker_summary_response_tolerates_missing_fields() -> None:
    from awf.api.routes.metrics import ProviderCircuitBreakerSummaryResponse

    response = ProviderCircuitBreakerSummaryResponse(
        provider="openai",
        model="gpt-5",
        state="open",
        failure_count=3,
        cooldown_until=None,
        last_reason_code=None,
        last_workspace_id=None,
    )
    dumped = response.model_dump()
    reloaded = ProviderCircuitBreakerSummaryResponse.model_validate(dumped)
    assert reloaded.last_reason_code is None
    assert reloaded.last_workspace_id is None
    assert reloaded.cooldown_until is None


def test_provider_recovery_state_action_enum_values() -> None:
    for valid_action in ("retry", "fallback", "terminal"):
        response = ProviderRecoveryStateResponse(action=valid_action)
        assert response.action == valid_action

    none_response = ProviderRecoveryStateResponse(action=None)
    assert none_response.action is None


def test_workspace_response_accepts_provider_recovery_state() -> None:
    now = datetime.now(UTC)
    ws = WorkspaceResponse(
        id="w1",
        status="running",
        version=1,
        repo_url="git@github.com:example/test.git",
        branch_base="main",
        branch_name="feature-1",
        base_commit="abc123",
        task_title="Test",
        task_prompt="Do something",
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy={},
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        agent="codex",
        agent_model="gpt-5",
        agent_effort=None,
        agent_model_source="task_policy",
        agent_effort_source="unavailable",
        env_profile=None,
        profile_ref=None,
        requested_profile=None,
        resolved_profile=None,
        test_commands=[],
        requires_database=False,
        node_id=None,
        compose_project_name=None,
        compose_file_path=None,
        pr_url=None,
        failure_reason=None,
        failure_message=None,
        created_at=now,
        updated_at=now,
        provider_recovery_state=ProviderRecoveryStateResponse(
            action="retry",
            reason_code="PROVIDER_RETRY_DELAYED",
        ),
    )
    assert ws.provider_recovery_state is not None
    assert ws.provider_recovery_state.action == "retry"

    ws_none = ws.model_copy(update={"provider_recovery_state": None})
    assert ws_none.provider_recovery_state is None


def test_recovery_summary_response_preserves_provider_recovery() -> None:
    now = datetime.now(UTC)
    provider_recovery_data = {
        "action": "fallback",
        "reason_code": "PROVIDER_FALLBACK_SELECTED",
        "fallback_target": {
            "agent": "codex",
            "provider": "openai",
            "model": "gpt-5.3-codex",
        },
        "cooldown_until": None,
        "next_eligible_at": (now + timedelta(seconds=300)).isoformat(),
        "recommended_action": "Dispatch an approved fallback model.",
    }
    response = WorkspaceRecoverySummaryResponse(
        from_state="running",
        to_state="failed",
        reason_code="agent_failure",
        action="retry",
        recovery_mode=None,
        started_at=now,
        current_operation=None,
        summary="Reverted running -> failed.",
        payload=None,
        provider_recovery=provider_recovery_data,
    )
    assert response.provider_recovery is not None
    assert response.provider_recovery["action"] == "fallback"
    assert response.provider_recovery["reason_code"] == "PROVIDER_FALLBACK_SELECTED"

    response_none = WorkspaceRecoverySummaryResponse(
        from_state="running",
        to_state="failed",
        reason_code="agent_failure",
        action="retry",
        recovery_mode=None,
        started_at=now,
        current_operation=None,
        summary="Reverted running -> failed.",
        payload=None,
        provider_recovery=None,
    )
    assert response_none.provider_recovery is None
