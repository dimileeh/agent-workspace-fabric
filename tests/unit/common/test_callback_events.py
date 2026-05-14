"""Public callback event policy tests."""

from __future__ import annotations

import pytest

from awf.common.callback_events import (
    callback_subscription_matches_event_type,
    is_valid_callback_subscription_event_type,
)


@pytest.mark.unit
def test_subscription_event_type_policy_accepts_public_exact_and_wildcards() -> None:
    assert is_valid_callback_subscription_event_type("workspace.created") is True
    assert is_valid_callback_subscription_event_type("workspace.*") is True
    assert is_valid_callback_subscription_event_type("workspace.created") is True
    assert is_valid_callback_subscription_event_type("workspace.state_changed") is True
    assert is_valid_callback_subscription_event_type("workspace.secondary_failure_recorded") is True
    assert is_valid_callback_subscription_event_type("workspace.internal") is False
    assert is_valid_callback_subscription_event_type("internal.secret_rotated") is False
    assert is_valid_callback_subscription_event_type("workspace.internal_secret") is False


@pytest.mark.unit
def test_subscription_event_matching_requires_public_source_event() -> None:
    assert callback_subscription_matches_event_type(
        "workspace.*",
        "workspace.state_changed",
    )
    assert callback_subscription_matches_event_type(
        "workspace.created",
        "workspace.created",
    )
    assert callback_subscription_matches_event_type(
        "workspace.state_changed",
        "workspace.state_changed",
    )
    assert callback_subscription_matches_event_type(
        "workspace.*",
        "workspace.secondary_failure_recorded",
    )
    assert callback_subscription_matches_event_type(
        "workspace.secondary_failure_recorded",
        "workspace.secondary_failure_recorded",
    )
    assert callback_subscription_matches_event_type(
        "workspace.created",
        "workspace.created",
    )
    assert not callback_subscription_matches_event_type(
        "workspace.*",
        "workspace.internal",
    )
    assert not callback_subscription_matches_event_type(
        "workspace.created",
        "workspace.internal",
    )
    assert not callback_subscription_matches_event_type(
        "workspace.*",
        "workspace.internal_secret",
    )
    assert not callback_subscription_matches_event_type(
        "workspace.*",
        "internal.secret_rotated",
    )
    assert not callback_subscription_matches_event_type(
        "operation.*",
        "workspace.state_changed",
    )
    assert not callback_subscription_matches_event_type(
        "workspace.state_changed",
        "workspace.created",
    )
    assert not callback_subscription_matches_event_type(
        "workspace.created",
        "operation.state_changed",
    )
