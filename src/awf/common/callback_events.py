"""Public callback event type policy."""

from __future__ import annotations

from typing import Final

CALLBACK_EVENT_WILDCARDS: Final[frozenset[str]] = frozenset(
    {"workspace.*", "merge.*", "operation.*"}
)
PUBLIC_CALLBACK_EVENT_TYPES: Final[tuple[str, ...]] = (
    "workspace.created",
    "workspace.state_changed",
    "workspace.secondary_failure_recorded",
    "operation.state_changed",
    "merge.candidate_updated",
)

_PUBLIC_CALLBACK_EVENT_TYPE_SET: Final[frozenset[str]] = frozenset(PUBLIC_CALLBACK_EVENT_TYPES)


def is_valid_callback_subscription_event_type(event_type: str) -> bool:
    """Return whether an event type is part of the public subscription contract."""
    return event_type in CALLBACK_EVENT_WILDCARDS or event_type in _PUBLIC_CALLBACK_EVENT_TYPE_SET


def callback_subscription_matches_event_type(
    subscription_event_type: str,
    event_type: str,
) -> bool:
    """Match a subscription pattern against a public source event type."""
    if event_type not in _PUBLIC_CALLBACK_EVENT_TYPE_SET:
        return False
    if subscription_event_type in CALLBACK_EVENT_WILDCARDS:
        return event_type.startswith(subscription_event_type[:-1])
    return subscription_event_type == event_type
