"""Structured control-plane audit payload tests."""

from __future__ import annotations

import pytest

from awf.common.audit import AUDIT_SCHEMA, build_audit_payload, redact_audit_value


@pytest.mark.unit
def test_build_audit_payload_keeps_common_keys_and_safe_evidence_refs() -> None:
    payload = build_audit_payload(
        actor="executor",
        action="git_push",
        outcome="succeeded",
        reason_code="VALIDATION_OK",
        pr_number=123,
        pr_url="https://github.com/example/repo/pull/123",
        target_branch="main",
        remote_branch="awf/ws_123",
        source_head_sha="b" * 40,
        evidence={
            "log_stream_refs": {"push": "executor.push"},
            "cleanup": [
                {"name": "containers", "status": "succeeded", "error": None},
            ],
            "empty": None,
        },
        extra={"branch_name": "awf/ws_123", "none": None},
    )

    assert payload == {
        "schema": AUDIT_SCHEMA,
        "actor": "executor",
        "source": "executor",
        "action": "git_push",
        "outcome": "succeeded",
        "reason_code": "VALIDATION_OK",
        "pr_number": 123,
        "pr_url": "https://github.com/example/repo/pull/123",
        "source_head_sha": "b" * 40,
        "target_branch": "main",
        "remote_branch": "awf/ws_123",
        "branch_name": "awf/ws_123",
        "evidence": {
            "log_stream_refs": {"push": "executor.push"},
            "cleanup": [{"name": "containers", "status": "succeeded"}],
        },
    }


@pytest.mark.unit
def test_redact_audit_value_recursively_redacts_secrets_without_losing_token_usage() -> None:
    value = {
        "github_token": "ghp_should_not_persist",
        "authorization": "Bearer secret-secret-secret",
        "remote": "https://user:ghp_should_not_persist@github.com/org/repo",
        "message": "GITHUB_TOKEN=ghp_should_not_persist " + ("x" * 1200),
        "usage": {
            "input_tokens": 100,
            "output_tokens": 25,
            "total_tokens": 125,
            "token_count": 3,
        },
        "nested": [
            {
                "prompt_tokens": 70,
                "token": "raw-token-secret",
                "secret_total_tokens": 5,
            }
        ],
    }

    redacted = redact_audit_value(value)

    assert redacted["github_token"] == "[redacted]"
    assert redacted["authorization"] == "[redacted]"
    assert redacted["remote"] == "https://[redacted]@github.com/org/repo"
    assert redacted["message"].startswith("GITHUB_TOKEN=[redacted] ")
    assert redacted["message"].endswith("...[truncated]")
    assert redacted["usage"] == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "token_count": 3,
    }
    assert redacted["nested"] == [
        {
            "prompt_tokens": 70,
            "token": "[redacted]",
            "secret_total_tokens": "[redacted]",
        }
    ]
    assert "ghp_should_not_persist" not in repr(redacted)
    assert "secret-secret-secret" not in repr(redacted)
