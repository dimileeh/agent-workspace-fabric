"""Structured control-plane audit payload tests."""

from __future__ import annotations

import pytest

from awf.common.audit import AUDIT_SCHEMA, build_audit_payload, redact_audit_value


@pytest.mark.unit
def test_build_audit_payload_keeps_common_keys_and_safe_evidence_refs() -> None:
    """Verify audit payload construction preserves safe common evidence."""
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
    """Verify audit redaction keeps usage metrics while removing secrets."""
    github_app_jwt = (
        "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJnaXRodWItYXBwIn0.c2lnbmF0dXJlX3Nob3VsZF9ub3RfcGVyc2lzdA"
    )
    value = {
        "github_token": "ghp_should_not_persist",
        "authorization": "Bearer secret-secret-secret",
        "remote": "https://user:ghp_should_not_persist@github.com/org/repo",
        "database_url": "postgresql+asyncpg://awf:db_password@db.internal/awf",
        "github_error": f"GitHub API rejected JWT {github_app_jwt}",
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
    assert redacted["database_url"] == "postgresql+asyncpg://[redacted]@db.internal/awf"
    assert redacted["github_error"] == "GitHub API rejected JWT [redacted]"
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
    assert github_app_jwt not in repr(redacted)
    assert "secret-secret-secret" not in repr(redacted)
    assert "db_password" not in repr(redacted)


@pytest.mark.unit
def test_redact_audit_value_converts_tuples_to_lists_by_default() -> None:
    """Verify audit redaction normalizes tuples to JSON-style lists by default."""
    redacted = redact_audit_value(
        (
            "safe",
            ("Authorization: Bearer ghp_nestedSecretToken",),
        )
    )

    assert redacted == [
        "safe",
        ["Authorization: Bearer [redacted]"],
    ]
