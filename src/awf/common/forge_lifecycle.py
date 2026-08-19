"""Provider-neutral pull-request lifecycle values."""

from dataclasses import dataclass
from enum import StrEnum


class PullRequestLifecycle(StrEnum):
    """Lifecycle returned by a forge's lightweight pull-request lookup."""

    open = "open"
    closed = "closed"
    merged = "merged"
    missing = "missing"


@dataclass(frozen=True)
class PullRequestSnapshot:
    """Lightweight live PR identity needed by admission and retry paths."""

    lifecycle: PullRequestLifecycle
    head_ref: str | None
    base_sha: str | None = None
