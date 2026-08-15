"""Unit tests for awf.common.profiles helper module."""

from __future__ import annotations

import pytest

from awf.common.profiles import (
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
    assert is_allowlisted_validation_message("invalid toolchain version for 'python'") is True
    assert (
        is_allowlisted_validation_message("runtime.toolchains declares unsupported versions")
        is True
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
def test_format_safe_validation_message_allowlisted_custom() -> None:
    err = {
        "type": "value_error",
        "msg": "Value error, invalid toolchain version for 'go'",
    }
    assert format_safe_validation_message(err) == "invalid toolchain version for 'go'"


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
