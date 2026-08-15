"""Unit tests for awf.common.profiles helper module."""

from __future__ import annotations

import pytest

from awf.common.profiles import (
    format_safe_validation_location,
    format_safe_validation_message,
    is_allowlisted_validation_message,
)


@pytest.mark.unit
def test_is_allowlisted_validation_message_static_messages() -> None:
    assert is_allowlisted_validation_message("Value error") is True
    assert is_allowlisted_validation_message("schema validation failed") is True
    assert is_allowlisted_validation_message("some unknown error message 123") is False


@pytest.mark.unit
def test_is_allowlisted_validation_message_dynamic_patterns() -> None:
    assert is_allowlisted_validation_message("invalid toolchain version for 'python'") is False
    assert (
        is_allowlisted_validation_message("runtime.toolchains declares unsupported versions")
        is False
    )
    assert (
        is_allowlisted_validation_message("profile path must be a workspace-relative path") is True
    )
    assert is_allowlisted_validation_message("path must include '{workspace_id}'") is True
    assert is_allowlisted_validation_message("endpoint must be a URL path") is True


@pytest.mark.unit
def test_format_safe_validation_message_known_type() -> None:
    err = {"type": "missing", "msg": "Field required"}
    assert format_safe_validation_message(err) == "Field required"


@pytest.mark.unit
def test_format_safe_validation_message_dynamic_toolchain_returns_static_value_error() -> None:
    err = {
        "type": "value_error",
        "msg": "Value error, invalid toolchain version for 'go'",
    }
    assert format_safe_validation_message(err) == "Value error"


@pytest.mark.unit
def test_format_safe_validation_message_unallowlisted_value_error() -> None:
    err = {
        "type": "value_error",
        "msg": "Value error, secret_key=12345 is invalid",
    }
    assert format_safe_validation_message(err) == "Value error"


@pytest.mark.unit
def test_format_safe_validation_message_unallowlisted_assertion_error() -> None:
    err = {
        "type": "assertion_error",
        "msg": "Assertion failed, secret_token=abc",
    }
    assert format_safe_validation_message(err) == "Assertion error"


@pytest.mark.unit
def test_format_safe_validation_message_unknown_error_type() -> None:
    err = {
        "type": "custom_unknown_type",
        "msg": "Some custom error message with sensitive info",
    }
    assert format_safe_validation_message(err) == "Validation error"


@pytest.mark.unit
def test_format_safe_validation_location_sanitizes_dynamic_and_unallowlisted_segments() -> None:
    # Empty location
    assert format_safe_validation_location(()) == ""
    assert format_safe_validation_location(None) == ""

    # Allowlisted field names
    assert format_safe_validation_location(("runtime", "agent_image")) == "runtime.agent_image"
    assert (
        format_safe_validation_location(("inline_profile", "runtime", "agent_image"))
        == "inline_profile.runtime.agent_image"
    )

    # List indices
    assert format_safe_validation_location(("validation_commands", 0)) == "validation_commands.0"
    assert format_safe_validation_location(("validation_commands", "1")) == "validation_commands.1"

    # Dynamic dictionary keys after mapping fields
    assert (
        format_safe_validation_location(("runtime", "environment", "plainsecret48729"))
        == "runtime.environment.<key>"
    )
    assert format_safe_validation_location(("labels", "user_secret_key")) == "labels.<key>"
    assert (
        format_safe_validation_location(("runtime", "toolchains", "python", 0))
        == "runtime.toolchains.<key>.0"
    )

    # Numeric keys under dynamic mappings (must not bypass scrubbing)
    assert (
        format_safe_validation_location(("runtime", "environment", "8080"))
        == "runtime.environment.<key>"
    )
    assert (
        format_safe_validation_location(("runtime", "environment", 8080))
        == "runtime.environment.<key>"
    )
    assert format_safe_validation_location(("labels", "12345")) == "labels.<key>"

    # Dict fields: ports, by_base_branch, details
    assert format_safe_validation_location(("ports", "8080")) == "ports.<key>"
    assert format_safe_validation_location(("ports", 8080)) == "ports.<key>"
    assert (
        format_safe_validation_location(("monitor", "auto_merge", "by_base_branch", "main"))
        == "monitor.auto_merge.by_base_branch.<key>"
    )
    assert (
        format_safe_validation_location(("monitor", "auto_merge", "by_base_branch", "123"))
        == "monitor.auto_merge.by_base_branch.<key>"
    )
    assert format_safe_validation_location(("details", "error_code")) == "details.<key>"

    # Keys under dynamic mappings that collide with allowlisted field names
    assert format_safe_validation_location(("ports", "command")) == "ports.<key>"
    assert (
        format_safe_validation_location(("monitor", "auto_merge", "by_base_branch", "command"))
        == "monitor.auto_merge.by_base_branch.<key>"
    )

    # Dynamic service names or unallowlisted field segments
    assert (
        format_safe_validation_location(("services", "my-custom-service", "image"))
        == "services.<key>.image"
    )
    assert (
        format_safe_validation_location(("services", "my-custom-service", "environment", "SECRET"))
        == "services.<key>.environment.<key>"
    )

    # Standard nested profile fields
    assert format_safe_validation_location(("docker", "compose_files")) == "docker.compose_files"
    assert format_safe_validation_location(("phases", "setup", "command")) == "phases.setup.command"
    assert (
        format_safe_validation_location(("services", 0, "healthchecks", 0, "healthcheck_cmd"))
        == "services.0.healthchecks.0.healthcheck_cmd"
    )
    assert (
        format_safe_validation_location(("database", "alembic", "config_path"))
        == "database.alembic.config_path"
    )
    assert (
        format_safe_validation_location(("monitor", "conformance_stall", "no_output_seconds"))
        == "monitor.conformance_stall.no_output_seconds"
    )

    # Unallowlisted extra field segment
    assert format_safe_validation_location(("unknown_secret_field",)) == "<key>"
