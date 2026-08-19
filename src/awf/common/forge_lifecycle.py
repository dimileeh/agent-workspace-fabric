"""Provider-neutral pull-request lifecycle values."""

from enum import StrEnum


class PullRequestLifecycle(StrEnum):
    """Lifecycle returned by a forge's lightweight pull-request lookup."""

    open = "open"
    closed = "closed"
    merged = "merged"
    missing = "missing"
