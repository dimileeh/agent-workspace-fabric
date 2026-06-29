"""Monitor-action vocabulary returned by the PR-monitor decision core.

``decide`` in :mod:`awf.runtime.pr_monitor` returns exactly one of these
frozen dataclasses per call. They are pure value types describing *what the
runner should do next* and depend only on the wire-shape value types in
:mod:`awf.runtime.pr_monitor_models` (one-directional import), so the pure
decision core imports and re-exports them to keep the historical
``from awf.runtime.pr_monitor import Merge`` call sites working while the core
file stays under the maintainability line budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from awf.runtime.pr_monitor_models import CheckFailure, ReviewComment, ReviewThread

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import OperatorHint

# ── Actions — the vocabulary decide() returns to the runner ────────────────


class AbortReason(StrEnum):
    """Reason codes for why the monitor gave up. Propagate into
    ``Workspace.failure_reason``-style fields so operators can triage.

    No ``iter_cap_reached`` / ``wall_clock_cap_reached`` — volume is
    not a terminal condition; bots can leave thousands of review
    cycles and the monitor must keep servicing the PR."""

    pr_closed_externally = "pr_closed_externally"
    no_progress_on_comments = "no_progress_on_comments"
    merge_conflict_unresolvable = "merge_conflict_unresolvable"
    merge_conflict_not_reproduced = "merge_conflict_not_reproduced"
    base_sync_no_progress = "base_sync_no_progress"
    stale = "stale"
    """GitHub reports mergeStateStatus == DIRTY after every other gate is
    clean — git can't auto-resolve and the CLI already had its chance."""


@dataclass(frozen=True)
class AddressComments:
    """Re-invoke the coding CLI to fix a batch of unresolved threads.

    ``threads`` are inline comments; ``review_comments`` are outside-diff.
    The runner addresses every item in the batch before re-polling for new
    activity (the ``fix_cycle`` inner loop).
    """

    threads: tuple[ReviewThread, ...]
    review_comments: tuple[ReviewComment, ...]


@dataclass(frozen=True)
class AddressOperatorHint:
    """Run one repair pass for a pending operator remonitor hint."""

    hint: OperatorHint


@dataclass(frozen=True)
class ReportCiFailure:
    """Re-invoke the CLI with logs of the failing checks."""

    failures: tuple[CheckFailure, ...]


@dataclass(frozen=True)
class RerunTransientCI:
    """Ask GitHub to rerun failed jobs for infra-like CI failures."""

    failures: tuple[CheckFailure, ...]


@dataclass(frozen=True)
class WaitForTransientCI:
    """Known infra-like CI failure; wait/back off before human escalation."""

    failures: tuple[CheckFailure, ...]
    wait_seconds: float
    wait_count: int


@dataclass(frozen=True)
class SyncBase:
    """Merge base into head. No payload — the runner knows base + head."""


@dataclass(frozen=True)
class WaitForCI:
    """CI still running; sleep poll_interval then re-decide. Does NOT bump iter_count."""

    reason: Literal[
        "pending_checks",
        "unknown_mergeable_state",
        "awaiting_required_checks",
        "ci_run_in_progress",
    ] = "pending_checks"


@dataclass(frozen=True)
class Merge:
    """All 5 gates green — squash-merge + delete branch."""


@dataclass(frozen=True)
class NotifyHuman:
    """Post a human-attention comment and keep monitoring.

    This is deliberately not terminal. A monitor owns the PR until it is
    merged, closed, or fails; human-attention comments are just status
    notifications while the workspace remains alive.
    """

    message: str | None = None


@dataclass(frozen=True)
class ShortCircuitCompleted:
    """PR already merged upstream — workspace can transition to completed."""


@dataclass(frozen=True)
class Abort:
    """Terminal failure; the runner transitions the workspace to ``failed``."""

    reason: AbortReason


MonitorAction = (
    AddressComments
    | AddressOperatorHint
    | ReportCiFailure
    | RerunTransientCI
    | WaitForTransientCI
    | SyncBase
    | WaitForCI
    | Merge
    | NotifyHuman
    | ShortCircuitCompleted
    | Abort
)


# Known bot reviewer logins whose "defer" verdicts should not block the
# merge — they only post advisory feedback and cannot themselves mark
# threads resolved. Any GitHub App handle ending in "[bot]" (e.g.
# dependabot[bot], renovate[bot]) is also treated as a bot. A
# non-member in this set whose login we don't recognise is treated as
# human — safer default: block the merge, let the operator triage.
BOT_REVIEWER_LOGINS = frozenset(
    {
        "greptile-apps",
        "coderabbitai",
        "gemini-code-assist",
        "chatgpt-codex-connector",
        "cursor",
        "codex",
        "github-actions",
    }
)

__all__ = [
    "AbortReason",
    "AddressComments",
    "AddressOperatorHint",
    "ReportCiFailure",
    "RerunTransientCI",
    "WaitForTransientCI",
    "SyncBase",
    "WaitForCI",
    "Merge",
    "NotifyHuman",
    "ShortCircuitCompleted",
    "Abort",
    "MonitorAction",
    "BOT_REVIEWER_LOGINS",
]
