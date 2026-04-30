"""Public callback event type policy tests."""

from __future__ import annotations

import pytest

from awf.common.callback_events import (
    callback_subscription_matches_event_type,
    is_valid_callback_subscription_event_type,
)


@pytest.mark.unit
def test_callback_subscription_matching_rejects_internal_source_event_types() -> None:
    assert not callback_subscription_matches_event_type("workspace.*", "workspace.internal")
    assert not callback_subscription_matches_event_type(
        "workspace.created",
        "workspace.internal",
    )


@pytest.mark.unit
def test_callback_subscription_matching_supports_wildcard_and_exact_public_events() -> None:
    assert is_valid_callback_subscription_event_type("workspace.*")
    assert callback_subscription_matches_event_type("workspace.*", "workspace.created")
    assert callback_subscription_matches_event_type("workspace.created", "workspace.created")
    assert not callback_subscription_matches_event_type("operation.*", "workspace.created")
    assert not callback_subscription_matches_event_type("workspace.state_changed", "workspace.created")
