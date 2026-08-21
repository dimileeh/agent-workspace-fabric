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
    # Last known PR head OID (GitHub ``headRefOid`` / Bitbucket source commit).
    # Used for post-push tip-containment checks: a concurrently merged or still-
    # open reused PR contains the retry tip when this equals the pushed
    # ``head_sha`` or is a fast-forward descendant of it.
    head_sha: str | None = None
