"""Public callback event policy tests."""

from __future__ import annotations

import pytest

from awf.common.callback_events import (
    callback_subscription_matches_event_type,
    is_valid_callback_subscription_event_type,
)


@pytest.mark.unit
def test_callback_subscription_event_type_policy_accepts_public_types_and_wildcards() -> None:
    assert is_valid_callback_subscription_event_type("workspace.created") is True
    assert is_valid_callback_subscription_event_type("workspace.*") is True
    assert is_valid_callback_subscription_event_type("internal.secret_rotated") is False

    assert callback_subscription_matches_event_type(
        "workspace.*",
        "workspace.state_changed",
    ) is True
    assert callback_subscription_matches_event_type(
        "workspace.created",
        "workspace.created",
    ) is True
    assert callback_subscription_matches_event_type(
        "workspace.created",
        "operation.state_changed",
    ) is False
    assert callback_subscription_matches_event_type(
        "workspace.*",
        "internal.secret_rotated",
    ) is False
