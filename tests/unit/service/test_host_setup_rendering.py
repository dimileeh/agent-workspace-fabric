"""First-run setup/start rendering contract tests."""

from __future__ import annotations

import json

import pytest

from awf.host_setup.rendering import (
    FIRST_RUN_FAILURE_REASON_CODES,
    FirstRunPayload,
    _redact_provider_refs,
    first_run_failure_payload,
    first_run_success_payload,
    first_run_warning_payload,
    render_first_run_json,
    render_first_run_pretty,
)


@pytest.mark.unit
def test_first_run_success_payload_renders_pretty_and_json() -> None:
    payload = first_run_success_payload(
        command="awf setup",
        summary="AWF first-run checks passed.",
        details={"api_url": "http://127.0.0.1:8000"},
        next_steps=("awf start",),
    )

    rendered_json = render_first_run_json(payload)
    rendered_pretty = render_first_run_pretty(payload)

    assert rendered_json == {
        "status": "success",
        "command": "awf setup",
        "summary": "AWF first-run checks passed.",
        "issues": [],
        "details": {"api_url": "http://127.0.0.1:8000"},
        "next_steps": ["awf start"],
    }
    assert "Status: success" in rendered_pretty
    assert "Command: awf setup" in rendered_pretty
    assert "Summary: AWF first-run checks passed." in rendered_pretty
    assert "Next:" in rendered_pretty
    assert "  - awf start" in rendered_pretty


@pytest.mark.unit
def test_first_run_pretty_renders_top_level_reason_without_issue_details() -> None:
    payload = FirstRunPayload(
        status="failed",
        command="awf setup",
        summary="External first-run preflight failed.",
        reason_code="EXTERNAL_PREFLIGHT_FAILED",
    )

    rendered_pretty = render_first_run_pretty(payload)

    assert "Reason: EXTERNAL_PREFLIGHT_FAILED" in rendered_pretty
    assert "Problem:" not in rendered_pretty


@pytest.mark.unit
def test_first_run_warning_payload_includes_structured_remediation() -> None:
    payload = first_run_warning_payload(
        command="awf setup",
        reason_code="PROVIDER_SETUP_AUTH_INVALID",
        summary="GitHub provider is unavailable.",
        details={"provider": "github"},
        next_steps=("Continue setup without GitHub.",),
    )

    rendered_json = render_first_run_json(payload)
    rendered_pretty = render_first_run_pretty(payload)

    assert rendered_json["status"] == "warning"
    assert rendered_json["reason_code"] == "PROVIDER_SETUP_AUTH_INVALID"
    assert rendered_json["issues"][0]["severity"] == "warning"
    remediation = rendered_json["issues"][0]["remediation"]
    assert remediation["problem"]
    assert remediation["cause"]
    assert remediation["fix"]
    assert remediation["docs_link"]
    assert "details" not in rendered_json
    assert rendered_json["issues"][0]["details"] == {"provider": "github"}
    assert "Reason: PROVIDER_SETUP_AUTH_INVALID" in rendered_pretty
    assert "Problem:" in rendered_pretty
    assert "Cause:" in rendered_pretty
    assert "Fix:" in rendered_pretty
    assert "Docs:" in rendered_pretty
    assert "Continue setup without GitHub." in rendered_pretty


@pytest.mark.unit
def test_first_run_failure_payload_includes_reason_and_safe_details() -> None:
    payload = first_run_failure_payload(
        command="awf setup",
        reason_code="SETUP_PROVIDER_UNKNOWN",
        summary="Provider selection failed.",
        details={
            "provider": "unknown",
            "credential_ref": "keyring://awf/github/token",
        },
        next_steps=("Run awf setup --provider github.",),
    )

    rendered_json = render_first_run_json(payload)
    rendered_pretty = render_first_run_pretty(payload)

    assert rendered_json["status"] == "failed"
    assert rendered_json["reason_code"] == "SETUP_PROVIDER_UNKNOWN"
    issue = rendered_json["issues"][0]
    assert issue["reason_code"] == "SETUP_PROVIDER_UNKNOWN"
    assert issue["severity"] == "failed"
    assert "details" not in rendered_json
    assert issue["details"] == {
        "provider": "unknown",
        "credential_ref": "[redacted]",
    }
    assert issue["remediation"]["related_command"]
    assert "Reason: SETUP_PROVIDER_UNKNOWN" in rendered_pretty
    assert "Problem:" in rendered_pretty
    assert "Cause:" in rendered_pretty
    assert "Fix:" in rendered_pretty
    assert "Docs:" in rendered_pretty
    assert "keyring://awf/github/token" not in rendered_pretty
    assert "[redacted]" in rendered_pretty


@pytest.mark.unit
def test_every_first_run_failure_reason_can_render_a_pretty_panel() -> None:
    for reason_code in FIRST_RUN_FAILURE_REASON_CODES:
        payload = first_run_failure_payload(
            command="awf setup",
            reason_code=reason_code,
            summary=f"Blocked by {reason_code}.",
            details={"reason_code_under_test": reason_code},
        )

        rendered_json = render_first_run_json(payload)
        rendered_pretty = render_first_run_pretty(payload)

        assert rendered_json["reason_code"] == reason_code
        issue = rendered_json["issues"][0]
        assert issue["remediation"]["problem"]
        assert issue["remediation"]["cause"]
        assert issue["remediation"]["fix"]
        assert issue["remediation"]["docs_link"]
        assert f"Reason: {reason_code}" in rendered_pretty
        assert "Problem:" in rendered_pretty
        assert "Cause:" in rendered_pretty
        assert "Fix:" in rendered_pretty
        assert "Docs:" in rendered_pretty


@pytest.mark.unit
def test_first_run_rendering_redacts_tokens_provider_refs_and_sensitive_keys() -> None:
    raw_values = (
        "ghp_firstRunSecretToken",
        "github_pat_firstRunSecretToken",
        "glpat-firstRunSecretToken",
        "sk-proj-firstRunSecretToken12345678901234567890",
        "sk-firstRunSecretToken12345678901234567890",
        "sk-ant-firstRunSecretToken",
        "AIzaFirstRunSecretToken123",
        "xoxb-firstRunSecretToken",
        "keyring://awf/github/token",
        "env://OPENAI_API_KEY",
        "plain-file:///home/user/.awf/secrets/openai",
        "https://user:ghp_urlSecretToken@github.com/org/repo",
        "Authorization: Bearer ghp_bearerSecretToken",
    )
    payload = first_run_failure_payload(
        command="awf setup",
        reason_code="CREDENTIAL_REF_INVALID",
        summary="Credential reference is invalid.",
        details={
            "message": " ".join(raw_values),
            "provider_ref": "env://OPENAI_API_KEY",
            "token": "sk-proj-dictKeySecretToken12345678901234567890",
            "nested": [
                {"api_key": "AIzaNestedSecretToken123"},
                ("plain-file:///tmp/awf-secret",),
            ],
        },
        next_steps=("Run awf setup --provider openai.",),
    )

    rendered_json_text = json.dumps(render_first_run_json(payload), sort_keys=True)
    rendered_pretty = render_first_run_pretty(payload)

    for raw_value in raw_values:
        assert raw_value not in rendered_json_text
        assert raw_value not in rendered_pretty
    assert "sk-proj-dictKeySecretToken12345678901234567890" not in rendered_json_text
    assert "AIzaNestedSecretToken123" not in rendered_json_text
    assert "plain-file:///tmp/awf-secret" not in rendered_json_text
    assert "[redacted]" in rendered_json_text
    assert "[redacted]" in rendered_pretty


@pytest.mark.unit
def test_provider_ref_redaction_preserves_tuple_container_type() -> None:
    redacted = _redact_provider_refs(
        (
            "env://OPENAI_API_KEY",
            ["plain-file:///tmp/awf-secret"],
            {"provider_ref": "keyring://awf/github/token"},
        )
    )

    assert redacted == (
        "[redacted]",
        ["[redacted]"],
        {"provider_ref": "[redacted]"},
    )
