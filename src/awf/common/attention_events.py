"""Typed attention WorkspaceEvent constants and payload helpers (AIRA-T490)."""

from __future__ import annotations

from typing import Any, Final

from awf.common.audit import redact_audit_value

ATTENTION_REQUIRED_EVENT_TYPE: Final[str] = "workspace.attention_required"
ATTENTION_CLEARED_EVENT_TYPE: Final[str] = "workspace.attention_cleared"
ATTENTION_SOURCE_MONITORING_PR: Final[str] = "monitoring_pr"
ATTENTION_SOURCE_BLOCKED: Final[str] = "blocked"


def monitoring_pr_attention_payload(
    *,
    reason: str | None,
    pr_url: str | None = None,
) -> dict[str, Any]:
    """Payload for monitoring_pr HUMAN_WAIT attention enter/clear events."""
    payload: dict[str, Any] = {
        "reason": redact_audit_value(reason),
        "source": ATTENTION_SOURCE_MONITORING_PR,
    }
    if pr_url:
        payload["pr_url"] = pr_url
    return payload


def blocked_attention_payload(
    *,
    block_reason_code: str | None,
    block_type: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Payload for protected-gate blocked attention enter/clear events."""
    payload: dict[str, Any] = {
        "source": ATTENTION_SOURCE_BLOCKED,
        "block_reason_code": block_reason_code,
        "block_type": block_type,
    }
    # Keep parity with monitoring_pr shape: prefer an explicit reason, else the
    # durable block reason code (no invented operator prose). Redact before
    # persistence — operator/monitor text can embed tokens.
    raw_reason = reason if reason is not None else block_reason_code
    payload["reason"] = redact_audit_value(raw_reason)
    return payload
