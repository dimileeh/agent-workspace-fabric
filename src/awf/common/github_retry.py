"""GitHub transport retry policy, context, and backoff helpers."""

from __future__ import annotations

import random
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

GITHUB_TRANSPORT_MAX_ATTEMPTS = 5
GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS = 45.0
GITHUB_TRANSPORT_INITIAL_BACKOFF_SECONDS = 1.0
GITHUB_TRANSPORT_MAX_BACKOFF_SECONDS = 30.0


class RetryPolicy(StrEnum):
    """Explicit per-operation retry posture for gh transport calls."""

    READ = "read"
    IDEMPOTENT_MUTATION = "idempotent_mutation"
    RECONCILABLE_MUTATION = "reconcilable_mutation"
    NEVER = "never"


class Reconciler(Protocol):
    """Post-failure reconcile hook for RECONCILABLE mutations."""

    async def __call__(self) -> Any | None:  # pragma: no cover - Protocol stub
        ...


@dataclass
class GitHubRetryContext:
    """Thread-local transport context set by monitor cycles and executors."""

    workspace_id: str | None = None
    pr_number: int | None = None
    deadline: float | None = None


@dataclass
class PullRequestCreateTransportOutcome:
    """Audit metadata captured by transport-level create_pr retry/reconcile."""

    strategy: str
    attempts: int
    failures: list[dict[str, object]] = field(default_factory=list)
    reconcile_lookups: list[dict[str, object]] = field(default_factory=list)
    matched_pr: dict[str, object] | None = None


github_retry_context: ContextVar[GitHubRetryContext | None] = ContextVar(
    "github_retry_context",
    default=None,
)


def jittered_backoff_seconds(*, attempt: int) -> float:
    """Full jitter: random(0, min(30s, 1s * 2^n))."""

    cap = min(
        GITHUB_TRANSPORT_MAX_BACKOFF_SECONDS,
        GITHUB_TRANSPORT_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)),
    )
    return random.uniform(0, cap)


def past_transport_deadline(deadline: float | None) -> bool:
    """Return whether the shared cycle/operation deadline has elapsed."""

    return deadline is not None and time.monotonic() >= deadline
