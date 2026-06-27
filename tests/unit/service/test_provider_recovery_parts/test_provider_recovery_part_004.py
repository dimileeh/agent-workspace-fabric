"""Provider recovery service-unhealthy timeout regression tests."""

from __future__ import annotations

import pytest

from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_IDLE_TIMEOUT,
    AGENT_SERVICE_UNHEALTHY,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.service.provider_recovery import provider_recovery_metadata_from_failure


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
