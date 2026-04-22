"""Pure decision core for the PR monitor.

Given a ``PRStatus`` snapshot + ``MonitorState`` (iteration counters,
which threads we've already addressed, wall-clock start) the ``decide``
function returns one ``MonitorAction``. No I/O, no GitHub, no subprocess
— this module is trivially unit-tested with hand-rolled snapshots.

The runner at ``pr_monitor_runner.py`` wraps ``decide`` with side effects:
it fetches ``PRStatus`` from GitHub, executes whichever ``MonitorAction``
comes back, persists the updated ``MonitorState``, and loops.

Design notes:

* **Exactly-one action per call.** ``decide`` never returns a list of
  things to do. The runner calls it once, acts, then calls it again. This
  keeps the loop simple and the decision table testable.
* **Iteration accounting is the runner's problem, not ours**. ``decide``
  just inspects ``state.iter_count`` against ``config.iter_cap``; bumping
  the counter happens in the runner after an action executes.
* **Thread dedup is the caller's problem**. ``decide`` returns a
  ``batch`` of threads on ``AddressComments`` consisting *only* of threads
  whose IDs are absent from ``state.threads_addressed_ids``. If every
  thread is already addressed, ``decide`` skips to the other gates.
* **Release-PR variant** (``task_kind="monitor_release_pr"``) differs in
  exactly one place: when all 5 gates are green it returns
  ``NotifyHuman`` instead of ``Merge``. The caller flips
  ``config.auto_merge`` accordingly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


# ── Wire-shape dataclasses — what the runner assembles after polling GH ────


class MergeableState(StrEnum):
    MERGEABLE = "MERGEABLE"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class CheckState(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class ReviewThread:
    """An inline (file + line) review thread.

    The GraphQL id is the node ID used with ``resolveReviewThread``.
    ``body_excerpt`` is short (~400 chars) so prompts stay small.
    """

    thread_id: str
    path: str | None
    line: int | None
    body_excerpt: str
    author: str | None = None
    is_resolved: bool = False


@dataclass(frozen=True)
class ReviewComment:
    """A review-level (outside-diff) comment — CodeRabbit summary, etc.

    No file/line anchor. Still must be resolved under gate #2.
    """

    comment_id: str
    body_excerpt: str
    author: str | None = None
    is_resolved: bool = False


@dataclass(frozen=True)
class CheckFailure:
    """One failing CI check worth surfacing to the coding CLI."""

    name: str
    conclusion: str  # FAILURE / TIMED_OUT / CANCELLED / ACTION_REQUIRED
    log_excerpt: str  # tail of the failing step's log, truncated


@dataclass(frozen=True)
class PRStatus:
    """Snapshot of a single PR's state as seen by the runner.

    Assembled from ``gh pr view --json ...`` + a GraphQL call for threads.
    The runner is responsible for populating ``unresolved_*`` with
    already-filtered-for-``isResolved==False`` items.
    """

    number: int
    head_sha: str
    mergeable: MergeableState
    check_state: CheckState
    unresolved_inline_threads: tuple[ReviewThread, ...]
    unresolved_review_comments: tuple[ReviewComment, ...]
    base_behind_count: int  # commits on base not in head
    ci_failures: tuple[CheckFailure, ...] = ()
    closed: bool = False
    merged: bool = False


# ── State — small, serialisable, lives on the workspace row ────────────────


@dataclass
class MonitorState:
    """Mutable state the runner keeps across iterations.

    Persisted to the workspace row so a mid-loop crash resumes from DB
    rather than re-addressing threads we already handled.
    """

    iter_count: int = 0
    last_push_sha: str | None = None  # SHA at the time of last push
    # thread_id → one of: "fix_committed" / "false_positive" / "defer"
    threads_addressed_ids: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)

    def mark_addressed(self, thread_id: str, verdict: str) -> None:
        self.threads_addressed_ids[thread_id] = verdict


@dataclass(frozen=True)
class MonitorConfig:
    """Caps + intervals — knobs exposed for policy without changing the logic."""

    iter_cap: int = 10
    wall_clock_cap_seconds: float = 6 * 3600  # 6 hours
    auto_merge: bool = True  # False = release-PR variant
    # Only used by the RUNNER, not decide(); listed here so the full config
    # travels in one object.
    poll_interval_seconds: float = 60.0
    settle_interval_seconds: float = 30.0


# ── Actions — the vocabulary decide() returns to the runner ────────────────


class AbortReason(StrEnum):
    """Reason codes for why the monitor gave up. Propagate into
    ``Workspace.failure_reason``-style fields so operators can triage."""

    iter_cap_reached = "iter_cap_reached"
    wall_clock_cap_reached = "wall_clock_cap_reached"
    pr_closed_externally = "pr_closed_externally"
    no_progress_on_comments = "no_progress_on_comments"


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
class ReportCiFailure:
    """Re-invoke the CLI with logs of the failing checks."""

    failures: tuple[CheckFailure, ...]


@dataclass(frozen=True)
class SyncBase:
    """Merge base into head. No payload — the runner knows base + head."""


@dataclass(frozen=True)
class WaitForCI:
    """CI still running; sleep poll_interval then re-decide. Does NOT bump iter_count."""

    reason: Literal["pending_checks", "unknown_mergeable_state"] = "pending_checks"


@dataclass(frozen=True)
class Merge:
    """All 5 gates green — squash-merge + delete branch."""


@dataclass(frozen=True)
class NotifyHuman:
    """Release-PR variant: post a 'ready to merge' comment, exit completed."""


@dataclass(frozen=True)
class ShortCircuitCompleted:
    """PR already merged upstream — workspace can transition to completed."""


@dataclass(frozen=True)
class Abort:
    """Terminal failure; the runner transitions the workspace to ``failed``."""

    reason: AbortReason


MonitorAction = (
    AddressComments
    | ReportCiFailure
    | SyncBase
    | WaitForCI
    | Merge
    | NotifyHuman
    | ShortCircuitCompleted
    | Abort
)


# ── The decision function ──────────────────────────────────────────────────


def decide(
    status: PRStatus, state: MonitorState, config: MonitorConfig
) -> MonitorAction:
    """Pure policy: which ``MonitorAction`` should the runner take next?

    Gate order matters:

    0.  Terminal states: merged → ShortCircuitCompleted, closed → Abort.
    1.  Budget: iter_cap / wall_clock_cap → Abort.
    2.  Unresolved comments (inline + review) → AddressComments.
        The batch only contains threads/comments we HAVEN'T already
        addressed (``state.threads_addressed_ids``). If every comment
        is already in that dict we fall through — the runner is
        probably waiting for the reviewer to actually mark them
        resolved on GitHub after our push, or the GraphQL query was
        stale; either way, gate forward to CI/merge checks.
    3.  CI FAILURE → ReportCiFailure.
    4.  CI PENDING (or mergeable UNKNOWN with no other blocker) →
        WaitForCI (does not consume an iteration).
    5.  Base behind → SyncBase.
    6.  Mergeable == CONFLICTING after addressing everything → Abort (the
        coding CLI can't fix a structural conflict it doesn't know about).
    7.  All green → Merge (or NotifyHuman if auto_merge=False).
    """

    # 0. Terminal upstream states short-circuit everything.
    if status.merged:
        return ShortCircuitCompleted()
    if status.closed:
        return Abort(reason=AbortReason.pr_closed_externally)

    # 1. Budget checks — consume NO iterations beyond this point, so it's
    # safe to fire every loop.
    if state.iter_count >= config.iter_cap:
        return Abort(reason=AbortReason.iter_cap_reached)
    if time.monotonic() - state.started_at >= config.wall_clock_cap_seconds:
        return Abort(reason=AbortReason.wall_clock_cap_reached)

    # 2. Unresolved comments, filtered to those we haven't handled yet.
    new_threads = tuple(
        t
        for t in status.unresolved_inline_threads
        if t.thread_id not in state.threads_addressed_ids
    )
    new_reviews = tuple(
        c
        for c in status.unresolved_review_comments
        if c.comment_id not in state.threads_addressed_ids
    )
    if new_threads or new_reviews:
        return AddressComments(threads=new_threads, review_comments=new_reviews)

    # 3. CI failures.
    if status.check_state == CheckState.FAILURE:
        if not status.ci_failures:
            # Failure reported by GraphQL but no per-check log available.
            # The runner treats an empty CheckFailure list as a signal to
            # grab ``gh run view --log-failed`` on its end; we still hand
            # off a ReportCiFailure action.
            return ReportCiFailure(failures=())
        return ReportCiFailure(failures=status.ci_failures)

    # 4. CI still running, or mergeable state unknown → passive wait.
    if status.check_state == CheckState.PENDING:
        return WaitForCI(reason="pending_checks")
    if status.mergeable == MergeableState.UNKNOWN:
        return WaitForCI(reason="unknown_mergeable_state")

    # 5. Base behind → integrate base into head before merging.
    if status.base_behind_count > 0:
        return SyncBase()

    # 6. Still conflicting with nothing else to do → can't continue.
    if status.mergeable == MergeableState.CONFLICTING:
        return Abort(reason=AbortReason.no_progress_on_comments)

    # 7. All green — terminal success action.
    if config.auto_merge:
        return Merge()
    return NotifyHuman()
