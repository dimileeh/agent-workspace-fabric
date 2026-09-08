"""Antigravity agy print-mode provider-failure regressions."""

from __future__ import annotations

import pytest

from awf.adapters.provider_failures import AGENT_AUTH_FAILED, classify_provider_failure


@pytest.mark.unit
def test_classifies_agy_print_mode_oauth_fallback_as_auth_failure() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr="Authentication required. Please visit the URL to log in:",
        provider="antigravity",
        model="gemini-3.1-pro",
    )

    assert classification is not None
    assert classification.reason_code == AGENT_AUTH_FAILED
    assert classification.failure_type == "auth"
    assert classification.provider == "antigravity"
    assert classification.model == "gemini-3.1-pro"
    assert classification.retryable is True
