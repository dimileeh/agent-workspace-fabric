"""First-run setup/start rendering contract tests."""

from __future__ import annotations

import json

import pytest

from awf.host_setup.rendering import (
    FIRST_RUN_FAILURE_REASON_CODES,
    FirstRunPayload,
    _redact_provider_refs,
    first_run_failure_payload,
    first_run_issue_from_reason_code,
    first_run_success_payload,
    first_run_warning_payload,
    redact_first_run_value,
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
def test_first_run_json_omits_empty_optional_collections() -> None:
    warning_payload = first_run_warning_payload(
        command="awf setup",
        reason_code="PROVIDER_SETUP_AUTH_INVALID",
        summary="GitHub provider is unavailable.",
    )
    failure_payload = first_run_failure_payload(
        command="awf start",
        reason_code="START_HEALTH_TIMEOUT",
        summary="AWF services did not become healthy.",
    )

    for payload in (warning_payload, failure_payload):
        rendered_json = render_first_run_json(payload)
        remediation = rendered_json["issues"][0]["remediation"]

        assert "details" not in rendered_json
        assert "next_steps" not in rendered_json
        assert "details" not in rendered_json["issues"][0]
        assert "next_steps" not in remediation


@pytest.mark.unit
def test_first_run_json_preserves_non_empty_remediation_next_steps() -> None:
    issue = first_run_issue_from_reason_code(
        "PROVIDER_SETUP_AUTH_INVALID",
        severity="warning",
        next_steps=("Refresh the GitHub token.",),
    )
    payload = FirstRunPayload(
        status="warning",
        command="awf setup",
        summary="GitHub provider is unavailable.",
        reason_code="PROVIDER_SETUP_AUTH_INVALID",
        issues=(issue,),
        next_steps=("Continue without GitHub.",),
    )

    rendered_json = render_first_run_json(payload)

    assert rendered_json["next_steps"] == ["Continue without GitHub."]
    assert rendered_json["issues"][0]["remediation"]["next_steps"] == ["Refresh the GitHub token."]


@pytest.mark.unit
def test_first_run_json_omits_empty_remediation_related_command() -> None:
    issue = first_run_issue_from_reason_code(
        "PROVIDER_SETUP_AUTH_INVALID",
        severity="warning",
        related_command="",
    )
    payload = FirstRunPayload(
        status="warning",
        command="awf setup",
        summary="GitHub provider is unavailable.",
        reason_code="PROVIDER_SETUP_AUTH_INVALID",
        issues=(issue,),
    )

    rendered_json = render_first_run_json(payload)
    rendered_pretty = render_first_run_pretty(payload)

    assert "related_command" not in rendered_json["issues"][0]["remediation"]
    assert "Related Command:" not in rendered_pretty


@pytest.mark.unit
def test_first_run_warning_and_failure_payloads_omit_empty_issue_details() -> None:
    warning_payload = first_run_warning_payload(
        command="awf setup",
        reason_code="PROVIDER_SETUP_AUTH_INVALID",
        summary="GitHub provider is unavailable.",
    )
    failure_payload = first_run_failure_payload(
        command="awf start",
        reason_code="START_HEALTH_TIMEOUT",
        summary="AWF services did not become healthy.",
    )

    for payload in (warning_payload, failure_payload):
        rendered_json = render_first_run_json(payload)

        assert "details" not in rendered_json
        assert "details" not in rendered_json["issues"][0]


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
def test_first_run_pretty_renders_sequence_details_as_nested_lines() -> None:
    payload = first_run_failure_payload(
        command="awf start",
        reason_code="START_HEALTH_TIMEOUT",
        summary="AWF service ports are unavailable.",
        details={
            "paths": ("plain-file:///tmp/awf-secret",),
            "port_conflicts": [
                {"container": 8000, "host": 8000},
                {"container": 5432, "provider_ref": "env://POSTGRES_PASSWORD"},
            ],
        },
    )

    rendered_pretty = render_first_run_pretty(payload)
    lines = rendered_pretty.splitlines()

    assert 'paths: ["[redacted]"]' not in rendered_pretty
    assert "port_conflicts: [" not in rendered_pretty
    assert "  paths:" in lines
    paths_index = lines.index("  paths:")
    assert lines[paths_index + 1] == "    - [redacted]"
    assert (
        "\n".join(
            [
                "  port_conflicts:",
                "    -",
                "      container: 8000",
                "      host: 8000",
                "    -",
                "      container: 5432",
                "      provider_ref: [redacted]",
            ]
        )
        in rendered_pretty
    )


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


@pytest.mark.unit
def test_first_run_redaction_preserves_tuple_container_type() -> None:
    redacted = redact_first_run_value(
        (
            "env://OPENAI_API_KEY",
            ("Authorization: Bearer ghp_nestedSecretToken",),
            {
                "provider_ref": "keyring://awf/github/token",
                "token": "sk-proj-dictKeySecretToken12345678901234567890",
            },
        )
    )

    assert redacted == (
        "[redacted]",
        ("Authorization: Bearer [redacted]",),
        {
            "provider_ref": "[redacted]",
            "token": "[redacted]",
        },
    )


@pytest.mark.unit
def test_first_run_redaction_does_not_double_redact_provider_ref_assignments() -> None:
    redacted = redact_first_run_value(
        {
            "message": "TOKEN=env://OPENAI_API_KEY",
            "nested": ("API_KEY=plain-file:///tmp/awf-secret",),
        }
    )

    assert redacted == {
        "message": "TOKEN=[redacted]",
        "nested": ("API_KEY=[redacted]",),
    }
