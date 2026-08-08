"""Attention WorkspaceEvent payload helpers must redact persisted reasons."""

from __future__ import annotations

import pytest

from awf.common.attention_events import (
    ATTENTION_SOURCE_BLOCKED,
    ATTENTION_SOURCE_MONITORING_PR,
    blocked_attention_payload,
    monitoring_pr_attention_payload,
)
from awf.common.audit import REDACTION_MARKER


@pytest.mark.unit
def test_monitoring_pr_attention_payload_redacts_secret_like_reason() -> None:
    """Persisted monitoring attention reasons must not retain token substrings."""
    secret = "ghp_should_not_persist"
    payload = monitoring_pr_attention_payload(
        reason=f"merge blocked; see Authorization: Bearer {secret}",
        pr_url="https://github.com/example/app/pull/1",
    )

    assert payload["source"] == ATTENTION_SOURCE_MONITORING_PR
    assert payload["pr_url"] == "https://github.com/example/app/pull/1"
    assert secret not in repr(payload)
    assert "Bearer" in str(payload["reason"])
    assert REDACTION_MARKER in str(payload["reason"])


@pytest.mark.unit
def test_monitoring_pr_attention_payload_preserves_none_reason() -> None:
    """None reasons stay None after redaction (no invented operator prose)."""
    payload = monitoring_pr_attention_payload(reason=None)

    assert payload == {
        "reason": None,
        "source": ATTENTION_SOURCE_MONITORING_PR,
    }


@pytest.mark.unit
def test_blocked_attention_payload_redacts_explicit_secret_like_reason() -> None:
    """Blocked attention payloads redact an explicit reason before persistence."""
    secret = "ghp_should_not_persist"
    payload = blocked_attention_payload(
        block_reason_code="QUALITY_GATE_POLICY_CHANGED",
        block_type="policy",
        reason=f"GITHUB_TOKEN={secret} rejected",
    )

    assert payload["source"] == ATTENTION_SOURCE_BLOCKED
    assert payload["block_reason_code"] == "QUALITY_GATE_POLICY_CHANGED"
    assert payload["block_type"] == "policy"
    assert secret not in repr(payload)
    assert payload["reason"] == f"GITHUB_TOKEN={REDACTION_MARKER} rejected"


@pytest.mark.unit
def test_blocked_attention_payload_redacts_fallback_block_reason_code() -> None:
    """When reason is omitted, the fallback block_reason_code is still redacted."""
    secret = "ghp_should_not_persist"
    payload = blocked_attention_payload(
        block_reason_code=f"token leak {secret}",
        block_type="policy",
    )

    assert payload["block_reason_code"] == f"token leak {secret}"
    assert secret not in str(payload["reason"])
    assert payload["reason"] == f"token leak {REDACTION_MARKER}"
