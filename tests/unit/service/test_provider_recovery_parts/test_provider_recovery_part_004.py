"""Provider recovery service-unhealthy timeout regression tests."""

from datetime import UTC, datetime

import pytest

from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_IDLE_TIMEOUT,
    AGENT_SERVICE_UNHEALTHY,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.service.provider_recovery import (
    decide_provider_recovery,
    provider_recovery_metadata_from_failure,
)
from awf.service.workspaces import workspace_create_task_policy_snapshot
from tests.unit.service.test_provider_recovery_parts.test_provider_recovery_part_002 import (
    _request,
)

pytestmark = pytest.mark.unit


def test_provider_recovery_metadata_from_failure_rejects_infra_service_unhealthy():
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_SERVICE_UNHEALTHY,
        message="agent service is not running",
        details={
            "provider_recovery": {
                "reason_code": AGENT_SERVICE_UNHEALTHY,
                "failure_type": "runtime_unhealthy",
                "failure_scope": "infra",
                "retryable": True,
                "failure_fingerprint": "",
            }
        },
        task_policy={"provider_recovery": {"max_same_provider_retries": 1}},
    )

    assert metadata is None


@pytest.mark.parametrize(
    ("reason_code", "provider_failure_type"),
    [
        (AGENT_IDLE_TIMEOUT, "idle_timeout"),
        (AGENT_TIMEOUT, "timeout"),
    ],
)
@pytest.mark.parametrize("service_healthy", [True, None])
def test_timeout_service_healthy_or_indeterminate_keeps_provider_classification(
    reason_code: str,
    provider_failure_type: str,
    service_healthy: bool | None,
) -> None:
    result = classify_provider_failure(
        reason_code=reason_code,
        stdout="",
        stderr="agent command timed out",
        provider="openai",
        model="gpt-5.3-codex",
        service_healthy=service_healthy,
    )

    assert result is not None
    assert result.reason_code == reason_code
    assert result.failure_type == provider_failure_type
    assert result.failure_scope == "provider"
    assert result.failure_fingerprint
    assert result.fallback_allowed is True


@pytest.mark.parametrize("reason_code", [AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT])
def test_timeout_service_down_returns_infra_service_unhealthy(
    reason_code: str,
) -> None:
    result = classify_provider_failure(
        reason_code=reason_code,
        stdout="",
        stderr='service "agent" is not running',
        provider="openai",
        model="gpt-5.3-codex",
        service_healthy=False,
    )

    assert result is not None
    assert result.reason_code == AGENT_SERVICE_UNHEALTHY
    assert result.failure_type == "runtime_unhealthy"
    assert result.failure_scope == "infra"
    assert result.failure_fingerprint == ""
    assert result.fallback_allowed is False


def test_service_down_does_not_reclassify_non_timeout_failures() -> None:
    result = classify_provider_failure(
        reason_code=AGENT_AUTH_FAILED,
        stdout="",
        stderr="Manual authorization is required. Please run /login.",
        provider="openai",
        model="gpt-5.3-codex",
        service_healthy=False,
    )

    assert result is not None
    assert result.reason_code == AGENT_AUTH_FAILED
    assert result.failure_type == "auth"
    assert result.failure_scope == "provider"


class TestTerminalState:
    """Prove finite termination for repeated fingerprints and exhausted fallbacks."""

    def test_repeated_fingerprint_three_times_is_terminal(self) -> None:
        policy = workspace_create_task_policy_snapshot(_request())
        metadata = provider_recovery_metadata_from_failure(
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            message="RESOURCE_EXHAUSTED RetryableQuotaError",
            details={"provider": "google", "model": "gemini-2.5-pro"},
            task_policy=policy,
        )
        assert metadata is not None
        policy["provider_recovery_state"] = {
            "failure_fingerprints": [
                "other-fingerprint",
                metadata["failure_fingerprint"],
                metadata["failure_fingerprint"],
            ],
            "fallback_attempt_number": 0,
            "retry_attempt_number": 3,
        }

        decision = decide_provider_recovery(
            metadata,
            task_policy=policy,
            current_agent="codex",
            current_model="gpt-5.5",
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )

        assert decision.action == "terminal"
        assert decision.retryable is False
        assert decision.terminal_reason == "REPEATED_PROVIDER_FAILURE_FINGERPRINT"

    def test_exhausted_fallbacks_is_terminal(self) -> None:
        policy = workspace_create_task_policy_snapshot(_request())
        metadata = provider_recovery_metadata_from_failure(
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            message="RESOURCE_EXHAUSTED RetryableQuotaError",
            details={"provider": "google", "model": "gemini-2.5-pro"},
            task_policy=policy,
        )
        assert metadata is not None
        policy["provider_recovery_state"] = {
            "fallback_attempt_number": 1,
            "retry_attempt_number": 1,
        }

        decision = decide_provider_recovery(
            metadata,
            task_policy=policy,
            current_agent="codex",
            current_model="gpt-5.3-codex",
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )

        assert decision.action == "terminal"
        assert decision.retryable is False
        assert decision.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


def test_unsupported_agent_runtime_discards_inherited_recommended_action() -> None:
    from awf.service.provider_recovery import (
        ProviderRecoveryDecision,
        _build_provider_recovery_state_view,
        _decision_payload,
        _recovery_task_policy,
    )

    metadata = {
        "reason_code": "AGENT_AUTH_FAILED",
        "provider": "google",
        "model": "gemini-2.5-pro",
        "recommended_action": "Refresh provider credentials before retrying this workspace.",
    }
    decision = ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="UNSUPPORTED_AGENT_RUNTIME",
        terminal_reason="UNSUPPORTED_AGENT_RUNTIME",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )

    payload = _decision_payload(decision, metadata)
    assert "recommended_action" not in payload

    policy = _recovery_task_policy(
        {},
        source_workspace_id="ws-123",
        source_attempt=None,
        source_canonical_attempt=None,
        metadata=metadata,
        decision=decision,
    )
    assert "recommended_action" not in policy["provider_recovery_state"]

    view = _build_provider_recovery_state_view(
        {
            **payload,
            "recommended_action": "Refresh provider credentials before retrying this workspace.",
        }
    )
    assert view.action == "terminal"
    assert view.reason_code == "UNSUPPORTED_AGENT_RUNTIME"
    assert view.recommended_action is None


def test_supported_terminal_decision_preserves_recommended_action() -> None:
    from awf.service.provider_recovery import (
        ProviderRecoveryDecision,
        _build_provider_recovery_state_view,
        _decision_payload,
        _recovery_task_policy,
    )

    metadata = {
        "reason_code": "PROVIDER_AUTH_FAILED",
        "provider": "openai",
        "model": "gpt-5",
        "recommended_action": "Refresh provider credentials before retrying this workspace.",
    }
    decision = ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="PROVIDER_AUTH_FAILED",
        terminal_reason="PROVIDER_AUTH_FAILED",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )

    payload = _decision_payload(decision, metadata)
    assert (
        payload["recommended_action"]
        == "Refresh provider credentials before retrying this workspace."
    )

    policy = _recovery_task_policy(
        {},
        source_workspace_id="ws-123",
        source_attempt=None,
        source_canonical_attempt=None,
        metadata=metadata,
        decision=decision,
    )
    assert (
        policy["provider_recovery_state"]["recommended_action"]
        == "Refresh provider credentials before retrying this workspace."
    )

    view = _build_provider_recovery_state_view(payload)
    assert view.action == "terminal"
    assert view.reason_code == "PROVIDER_AUTH_FAILED"
    assert view.recommended_action == "Refresh provider credentials before retrying this workspace."


def test_skipped_placeholder_slot_remains_free_across_decisions() -> None:
    from datetime import UTC, datetime

    from awf.service.provider_recovery import decide_provider_recovery

    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    task_policy = {
        "provider_recovery": {
            "fallbacks": [
                {"agent": "invalid_retired_agent", "model": "m1"},
                {"agent": "codex", "model": "gpt-5.5"},
                {"agent": "claude_code", "model": "claude-3-7-sonnet"},
            ],
            "max_fallback_attempts": 2,
            "max_same_provider_retries": 1,
        },
        "provider_recovery_state": {
            "retry_attempt_number": 1,
            "fallback_attempt_number": 0,
            "launched_fallback_attempts": 0,
        },
    }

    decision1 = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    assert decision1.action == "fallback"
    assert decision1.target_agent == "codex"
    assert decision1.fallback_attempt_number == 2
    assert decision1.launched_fallback_attempts == 1

    task_policy_step2 = dict(task_policy)
    task_policy_step2["provider_recovery_state"] = {
        "retry_attempt_number": 1,
        "fallback_attempt_number": 2,
        "launched_fallback_attempts": 1,
    }
    decision2 = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy_step2,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    assert decision2.action == "fallback"
    assert decision2.target_agent == "claude_code"
    assert decision2.fallback_attempt_number == 3
    assert decision2.launched_fallback_attempts == 2

    task_policy_step3 = dict(task_policy)
    task_policy_step3["provider_recovery_state"] = {
        "retry_attempt_number": 1,
        "fallback_attempt_number": 3,
        "launched_fallback_attempts": 2,
    }
    decision3 = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy_step3,
        current_agent="claude_code",
        current_model="claude-3-7-sonnet",
        now=now,
    )
    assert decision3.action == "terminal"
    assert decision3.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"
    assert decision3.launched_fallback_attempts == 2
