"""PR monitor runner configuration and public protocol types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class PostMergeTargetReconciler(Protocol):
    """Best-effort target-branch repair hook invoked after a PR is merged."""

    async def __call__(  # pragma: no cover - Protocol declaration only.
        self: Any, *, repo_url: str, branch: str, workspace_id: str
    ) -> object: ...


@dataclass(frozen=True)
class MonitorRunnerConfig:
    """Operational knobs for the runner (separate from MonitorConfig so
    we can tune timing without touching the decision logic)."""

    # Max number of outer loop iterations before we stop (safety net
    # against a decision-loop bug; the outer loop is uncapped for
    # ``WaitForCI`` so idle polls don't count). A legitimate monitor
    # session should always exit via a terminal action well before this.
    max_outer_iterations: int = 10_000
    # Max fix_cycle re-polls inside a single AddressComments action.
    max_fix_cycle_passes: int = 5
    # Max local validation-fix passes before a PR-monitor repair push. This is
    # intentionally small: the monitor should validate and salvage its own
    # repair commit, but not open a second unbounded execution loop.
    pre_push_validation_fix_passes: int = 1
    # Transient GitHub outages can surface through `git fetch`, not only `gh`.
    # Keep base refresh authoritative, but retry remote 5xx/network failures
    # before declaring the monitor infrastructure-failed.
    transient_base_fetch_max_retries: int = 5
    transient_base_fetch_initial_backoff_seconds: float = 5.0
    transient_base_fetch_max_backoff_seconds: float = 120.0
    # Transient forge (GitHub/Bitbucket) API faults — an ambiguous auth blip
    # (a bare ``HTTP 401`` / ``Requires authentication`` with no strong permanent
    # marker, or a Bitbucket 401/403), a 5xx, a rate-limit, or a transport fault —
    # are bounded-retryable with exponential backoff so a momentary blip recovers
    # instead of terminating a healthy auto-merge (#515). Kept separate from the
    # base-fetch knobs (own config + own per-context retry count) so the two never
    # entangle, while sharing only the pure backoff schedule.
    transient_forge_max_retries: int = 5
    transient_forge_initial_backoff_seconds: float = 5.0
    transient_forge_max_backoff_seconds: float = 120.0
